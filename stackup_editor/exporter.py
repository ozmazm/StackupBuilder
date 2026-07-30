from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields

from stackup_editor.catalog import MaterialCatalog
from stackup_editor.impedance_models import (
    ImpedanceWorkspaceState,
    impedance_workspace_from_dict,
    impedance_workspace_to_dict,
)
from stackup_editor.models import (
    CoverlaySettings,
    CopperLayer,
    DielectricLayer,
    FlexCoreLayer,
    SolderMaskSettings,
    Stackup,
    catalog_material_type_for_dielectric,
    dielectric_type_display_name,
    infer_copper_type,
    is_dummy_core_type,
    is_etched_core_type,
    is_no_flow_prepreg_type,
    is_prepreg_dielectric_type,
)
from stackup_editor.units import (
    SUPPORTED_UNITS,
    format_frequency_ghz,
    format_roughness_um,
    format_stackup_thickness,
    format_total_thickness,
    from_display,
)

_THICKNESS_PATTERN = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*(um|mm|mil|inch|oz)\b")
_FREQUENCY_PATTERN = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*(MHz|GHz)\b", re.IGNORECASE)
_PERCENT_PATTERN = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*%")
_XPEDITION_FIELD_PATTERN = re.compile(r'([A-Z_]+)=(".*?"|[^\s)]+)')
_IMPEDANCE_WORKSPACE_START = "<<<STACKUP_EDITOR_IMPEDANCE_WORKSPACE>>>"
_IMPEDANCE_WORKSPACE_END = "<<<END_STACKUP_EDITOR_IMPEDANCE_WORKSPACE>>>"
_RIGID_FLEX_PROJECT_START = "<<<STACKUP_EDITOR_RIGID_FLEX_PROJECT>>>"
_RIGID_FLEX_PROJECT_END = "<<<END_STACKUP_EDITOR_RIGID_FLEX_PROJECT>>>"
_RIGID_FLEX_FORMAT_VERSION = 5


def stackup_import_mode_warning(text: str, *, rigid_flex_mode: bool) -> str | None:
    """Return a user-facing warning when file content does not match the editor mode."""

    contains_flex = "flex" in text.casefold()
    if rigid_flex_mode and not contains_flex:
        return "Selected stackup is Rigid, not suitable for Rigid Flex stackup."
    if not rigid_flex_mode and contains_flex:
        return "Selected stackup is Rigid Flex, not suitable for Rigid stackup."
    return None


@dataclass
class RigidFlexZoneState:
    kind: str
    label: str
    display_unit: str
    stackup: Stackup
    impedance_workspace: ImpedanceWorkspaceState | None = None
    flex_slot_coverage: list[int] | None = None
    flex_slot_map: dict[int, int] | None = None
    global_copper_numbers: list[int] | None = None
    parent_zone_index: int | None = None
    parent_zone_indices: list[int] | None = None
    definition_zone_index: int | None = None


def _validate_branching_rigid_flex_zones(zones: list[RigidFlexZoneState]) -> None:
    if len(zones) < 2:
        raise ValueError("A rigid-flex project must contain at least one rigid part and one Flex Part.")
    root_parents = zones[0].parent_zone_indices or (
        [zones[0].parent_zone_index] if zones[0].parent_zone_index is not None else []
    )
    if zones[0].kind.strip().lower() != "rigid" or root_parents:
        raise ValueError("A connected rigid-flex project must begin with an unparented master rigid part.")
    if not any(zone.kind.strip().lower() == "flex" for zone in zones):
        raise ValueError("A connected rigid-flex project must contain at least one Flex Part.")
    for zone_index, zone in enumerate(zones):
        kind = zone.kind.strip().lower()
        if kind not in {"rigid", "flex"}:
            raise ValueError(f"Unsupported rigid-flex zone kind: {kind!r}")
        if zone.definition_zone_index is not None:
            definition_index = zone.definition_zone_index
            if not 0 <= definition_index < len(zones) or definition_index == zone_index:
                raise ValueError(f"Part {zone_index + 1} has an invalid shared-definition reference.")
            if zones[definition_index].kind.strip().lower() != kind:
                raise ValueError(
                    f"Part {zone_index + 1} cannot share a definition with a different part kind."
                )
        if zone_index > 0:
            parent_indices = zone.parent_zone_indices or (
                [zone.parent_zone_index] if zone.parent_zone_index is not None else []
            )
            if any(not 0 <= parent_index < zone_index for parent_index in parent_indices):
                raise ValueError(f"Part {zone_index + 1} has an invalid parent connection.")
            expected_parent_kind = "flex" if kind == "rigid" else "rigid"
            if kind == "flex" and len(parent_indices) != 1:
                raise ValueError(f"Flex Part {zone_index + 1} must have exactly one parent rigid part.")
            if any(zones[parent_index].kind.strip().lower() != expected_parent_kind for parent_index in parent_indices):
                raise ValueError(
                    f"Part {zone_index + 1} must be connected after a {expected_parent_kind.title()} Part."
                )
        if kind == "flex":
            continue

        coverage = None if zone.flex_slot_coverage is None else set(zone.flex_slot_coverage)
        if coverage is not None:
            parent_indices = zone.parent_zone_indices or (
                [zone.parent_zone_index] if zone.parent_zone_index is not None else []
            )
            if not coverage and parent_indices:
                raise ValueError(f"Rigid part {zone_index + 1} must connect to at least one flex sandwich.")
            active_slots = {
                slot_id
                for parent_index in parent_indices
                for slot_id in zones[parent_index].stackup.flex_sandwich_slot_ids()
            }
            if parent_indices and not coverage <= active_slots:
                raise ValueError(f"Rigid part {zone_index + 1} references a flex sandwich that does not exist.")
            slot_map = zone.flex_slot_map or {}
            if not coverage <= set(slot_map):
                raise ValueError(f"Rigid part {zone_index + 1} has incomplete flex-slot mapping data.")
            if any(int(local_slot) < 0 for local_slot in slot_map.values()):
                raise ValueError(f"Rigid part {zone_index + 1} has a negative local flex-slot index.")

        numbers = zone.global_copper_numbers
        if numbers is not None:
            if len(numbers) != zone.stackup.copper_count():
                raise ValueError(f"Rigid part {zone_index + 1} has invalid global copper numbering.")
            if any(number <= 0 for number in numbers) or numbers != sorted(set(numbers)):
                raise ValueError(f"Rigid part {zone_index + 1} global copper numbers must be positive and increasing.")


def _dataclass_kwargs(cls: type, payload: dict[str, object]) -> dict[str, object]:
    allowed = {item.name for item in fields(cls)}
    return {key: value for key, value in payload.items() if key in allowed}


def _frequency_map(payload: object, *, field_name: str) -> dict[float, float]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"Rigid-flex project field {field_name!r} must be an object.")
    converted: dict[float, float] = {}
    try:
        for frequency, value in payload.items():
            converted[float(frequency)] = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Rigid-flex project field {field_name!r} contains invalid numeric data.") from exc
    return converted


def _stackup_to_project_dict(stackup: Stackup) -> dict[str, object]:
    return {
        "mode": stackup.mode,
        "layers": [asdict(layer) for layer in stackup.layers],
        "soldermask": asdict(stackup.soldermask),
        "coverlay": asdict(stackup.coverlay) if stackup.coverlay is not None else None,
        "flex_sandwich_slots": list(stackup.flex_sandwich_slots),
        "flex_slot_capacity": stackup.flex_slot_capacity,
    }


def _layer_from_project_dict(payload: object, *, layer_number: int):
    if not isinstance(payload, dict):
        raise ValueError(f"Rigid-flex layer {layer_number} must be an object.")
    kind = str(payload.get("kind") or "").strip().lower()
    if kind == "copper":
        kwargs = _dataclass_kwargs(CopperLayer, payload)
        kwargs["kind"] = "copper"
        return CopperLayer(**kwargs)
    if kind == "dielectric":
        kwargs = _dataclass_kwargs(DielectricLayer, payload)
        kwargs["kind"] = "dielectric"
        return DielectricLayer(**kwargs)
    if kind == "flex_core":
        kwargs = _dataclass_kwargs(FlexCoreLayer, payload)
        kwargs["kind"] = "flex_core"
        for field_name in ("dk_by_freq_ghz", "df_by_freq_ghz"):
            kwargs[field_name] = _frequency_map(payload.get(field_name), field_name=field_name)
        return FlexCoreLayer(**kwargs)
    raise ValueError(f"Rigid-flex layer {layer_number} has unsupported kind {kind!r}.")


def _stackup_from_project_dict(payload: object) -> Stackup:
    if not isinstance(payload, dict):
        raise ValueError("Each rigid-flex zone must contain a stackup object.")

    layer_payloads = payload.get("layers")
    if not isinstance(layer_payloads, list) or not layer_payloads:
        raise ValueError("Each rigid-flex zone must contain at least one stackup layer.")
    layers = [
        _layer_from_project_dict(layer_payload, layer_number=index)
        for index, layer_payload in enumerate(layer_payloads, start=1)
    ]

    soldermask_payload = payload.get("soldermask")
    if not isinstance(soldermask_payload, dict):
        raise ValueError("Each rigid-flex zone must contain solder-mask settings.")
    soldermask = SolderMaskSettings(**_dataclass_kwargs(SolderMaskSettings, soldermask_payload))

    coverlay_payload = payload.get("coverlay")
    coverlay = None
    if coverlay_payload is not None:
        if not isinstance(coverlay_payload, dict):
            raise ValueError("Rigid-flex coverlay settings must be an object.")
        coverlay_kwargs = _dataclass_kwargs(CoverlaySettings, coverlay_payload)
        for field_name in (
            "pi_dk_by_freq_ghz",
            "pi_df_by_freq_ghz",
            "adhesive_dk_by_freq_ghz",
            "adhesive_df_by_freq_ghz",
        ):
            coverlay_kwargs[field_name] = _frequency_map(
                coverlay_payload.get(field_name),
                field_name=field_name,
            )
        coverlay = CoverlaySettings(**coverlay_kwargs)

    mode = str(payload.get("mode") or "rigid").strip().lower()
    if mode not in {"rigid", "flex"}:
        raise ValueError(f"Unsupported stackup mode in rigid-flex project: {mode!r}")

    slots_payload = payload.get("flex_sandwich_slots", [])
    if not isinstance(slots_payload, list):
        raise ValueError("Flex sandwich slots must be an array.")
    try:
        slots = [int(slot) for slot in slots_payload]
        capacity = int(payload.get("flex_slot_capacity", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Flex sandwich slot data is invalid.") from exc
    if any(slot < 0 for slot in slots) or capacity < 0:
        raise ValueError("Flex sandwich slot values cannot be negative.")

    return Stackup(
        layers=layers,
        soldermask=soldermask,
        mode=mode,
        coverlay=coverlay,
        flex_sandwich_slots=slots,
        flex_slot_capacity=capacity,
    )


def export_rigid_flex_text(zones: list[RigidFlexZoneState]) -> str:
    _validate_branching_rigid_flex_zones(zones)

    zone_payloads: list[dict[str, object]] = []
    summary_lines: list[str] = []
    for index, zone in enumerate(zones):
        kind = zone.kind.strip().lower()
        if kind not in {"rigid", "flex"}:
            raise ValueError(f"Unsupported rigid-flex zone kind: {kind!r}")
        if zone.display_unit not in SUPPORTED_UNITS:
            raise ValueError(f"Unsupported display unit in zone {index + 1}: {zone.display_unit!r}")
        label = zone.label.strip() or f"{kind.title()} zone {(index // 2) + 1}"
        workspace_payload = (
            impedance_workspace_to_dict(zone.impedance_workspace, zone.stackup)
            if zone.impedance_workspace is not None
            else None
        )
        zone_payloads.append(
            {
                "kind": kind,
                "label": label,
                "display_unit": zone.display_unit,
                "stackup": _stackup_to_project_dict(zone.stackup),
                "impedance_workspace": workspace_payload,
                "flex_slot_coverage": zone.flex_slot_coverage,
                "flex_slot_map": (
                    {str(key): value for key, value in (zone.flex_slot_map or {}).items()}
                    if zone.flex_slot_map is not None
                    else None
                ),
                "global_copper_numbers": zone.global_copper_numbers,
                "parent_zone_index": zone.parent_zone_index,
                "parent_zone_indices": zone.parent_zone_indices,
                "definition_zone_index": zone.definition_zone_index,
            }
        )
        summary_lines.append(
            f"Zone {index + 1}: {label} | {kind.title()} | {zone.stackup.copper_count()} copper layers"
        )

    project_payload = {
        "format": "stackup-editor-rigid-flex",
        "version": _RIGID_FLEX_FORMAT_VERSION,
        "zones": zone_payloads,
    }
    json_payload = json.dumps(project_payload, indent=2, ensure_ascii=True)
    lines = [
        "PCB Rigid-Flex Stackup Export",
        "==============================",
        f"Format version: {_RIGID_FLEX_FORMAT_VERSION}",
        f"Zone count: {len(zones)}",
        *summary_lines,
        "",
        "Machine-readable project data",
        "-----------------------------",
        _RIGID_FLEX_PROJECT_START,
        *json_payload.splitlines(),
        _RIGID_FLEX_PROJECT_END,
        "",
    ]
    return "\n".join(lines)


def import_rigid_flex_text(text: str) -> list[RigidFlexZoneState]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "PCB Rigid-Flex Stackup Export":
        raise ValueError("This file is not a rigid-flex StackUp Editor text export.")
    try:
        start_index = lines.index(_RIGID_FLEX_PROJECT_START)
        end_index = lines.index(_RIGID_FLEX_PROJECT_END, start_index + 1)
    except ValueError as exc:
        raise ValueError("The rigid-flex project data block is missing or incomplete.") from exc

    payload_text = "\n".join(lines[start_index + 1 : end_index]).strip()
    try:
        project_payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse rigid-flex project data: {exc}") from exc
    if not isinstance(project_payload, dict):
        raise ValueError("Rigid-flex project data is malformed.")
    if project_payload.get("format") != "stackup-editor-rigid-flex":
        raise ValueError("The text file contains an unsupported project format.")
    version = project_payload.get("version")
    if version not in {1, 2, 3, 4, _RIGID_FLEX_FORMAT_VERSION}:
        raise ValueError(
            f"Unsupported rigid-flex project version: {version!r}."
        )

    zones_payload = project_payload.get("zones")
    if not isinstance(zones_payload, list) or len(zones_payload) < 2:
        raise ValueError("A rigid-flex project must contain at least two zones.")

    zones: list[RigidFlexZoneState] = []
    for index, zone_payload in enumerate(zones_payload):
        if not isinstance(zone_payload, dict):
            raise ValueError(f"Rigid-flex zone {index + 1} is malformed.")
        kind = str(zone_payload.get("kind") or "").strip().lower()
        if version == 1:
            expected_kind = "rigid" if index % 2 == 0 else "flex"
            if kind != expected_kind:
                raise ValueError("Legacy rigid-flex zones must alternate and begin with a rigid zone.")
        elif kind not in {"rigid", "flex"}:
            raise ValueError(f"Unsupported rigid-flex zone kind: {kind!r}")
        display_unit = str(zone_payload.get("display_unit") or "mm")
        if display_unit not in SUPPORTED_UNITS:
            raise ValueError(f"Unsupported display unit in zone {index + 1}: {display_unit!r}")
        label = str(zone_payload.get("label") or "").strip() or f"{kind.title()} zone {(index // 2) + 1}"
        stackup = _stackup_from_project_dict(zone_payload.get("stackup"))
        if stackup.mode != kind:
            raise ValueError(
                f"Zone {index + 1} is marked {kind!r}, but its stackup mode is {stackup.mode!r}."
            )
        workspace_payload = zone_payload.get("impedance_workspace")
        workspace = (
            impedance_workspace_from_dict(workspace_payload, stackup)
            if workspace_payload is not None
            else None
        )
        coverage_payload = zone_payload.get("flex_slot_coverage")
        coverage = None
        if coverage_payload is not None and not isinstance(coverage_payload, list):
            raise ValueError(f"Rigid-flex zone {index + 1} has invalid flex-slot coverage data.")
        if isinstance(coverage_payload, list):
            coverage = [int(slot_id) for slot_id in coverage_payload]
        slot_map_payload = zone_payload.get("flex_slot_map")
        slot_map = None
        if slot_map_payload is not None and not isinstance(slot_map_payload, dict):
            raise ValueError(f"Rigid-flex zone {index + 1} has invalid flex-slot mapping data.")
        if isinstance(slot_map_payload, dict):
            slot_map = {int(key): int(value) for key, value in slot_map_payload.items()}
        numbers_payload = zone_payload.get("global_copper_numbers")
        global_numbers = None
        if numbers_payload is not None and not isinstance(numbers_payload, list):
            raise ValueError(f"Rigid-flex zone {index + 1} has invalid global copper numbering.")
        if isinstance(numbers_payload, list):
            global_numbers = [int(number) for number in numbers_payload]
        parent_payload = zone_payload.get("parent_zone_index")
        parent_zone_index = int(parent_payload) if parent_payload is not None else None
        parents_payload = zone_payload.get("parent_zone_indices")
        if parents_payload is not None and not isinstance(parents_payload, list):
            raise ValueError(f"Rigid-flex zone {index + 1} has invalid parent connection data.")
        parent_zone_indices = (
            [int(parent_index) for parent_index in parents_payload]
            if isinstance(parents_payload, list)
            else None
        )
        definition_payload = zone_payload.get("definition_zone_index")
        definition_zone_index = (
            int(definition_payload) if definition_payload is not None else None
        )
        zones.append(
            RigidFlexZoneState(
                kind=kind,
                label=label,
                display_unit=display_unit,
                stackup=stackup,
                impedance_workspace=workspace,
                flex_slot_coverage=coverage,
                flex_slot_map=slot_map,
                global_copper_numbers=global_numbers,
                parent_zone_index=parent_zone_index,
                parent_zone_indices=parent_zone_indices,
                definition_zone_index=definition_zone_index,
            )
        )
    if version == 1:
        for index, zone in enumerate(zones):
            zone.parent_zone_index = None if index == 0 else index - 1
    elif version == 2:
        flex_index = next((index for index, zone in enumerate(zones) if zone.kind == "flex"), None)
        for index, zone in enumerate(zones):
            if index == 0:
                zone.parent_zone_index = None
            elif zone.kind == "flex":
                zone.parent_zone_index = 0
            else:
                zone.parent_zone_index = flex_index
    if version in {1, 2, 3}:
        for zone in zones:
            zone.parent_zone_indices = (
                [zone.parent_zone_index] if zone.parent_zone_index is not None else []
            )
    _validate_branching_rigid_flex_zones(zones)
    return zones


def export_stackup_text(
    stackup: Stackup,
    catalog: MaterialCatalog,
    unit: str,
    impedance_workspace: ImpedanceWorkspaceState | None = None,
) -> str:
    is_symmetric, symmetry_issues = stackup.symmetry_report(catalog)
    soldermask = stackup.soldermask
    lines = [
        "PCB Stackup Export",
        "=================",
        f"Display unit: {unit}",
        f"Total thickness: {format_total_thickness(stackup.total_thickness_mm(catalog), unit)}",
        f"Copper layers: {stackup.copper_count()}",
        f"Symmetry: {'Symmetric' if is_symmetric else 'Not symmetric'}",
        "",
    ]

    if symmetry_issues:
        lines.append("Symmetry warnings:")
        for issue in symmetry_issues:
            lines.append(f" - {issue}")
        lines.append("")

    lines.extend(
        [
            "1. Top solder mask",
            f"   Thickness: {format_stackup_thickness(soldermask.thickness_mm, unit, is_copper=False)}",
            f"   Manufacturer: {soldermask.manufacturer}",
            f"   Frequency: {format_frequency_ghz(soldermask.freq_ghz)}",
            f"   Dk: {soldermask.dk:.3f}",
            f"   Df: {soldermask.df:.4f}",
            "",
        ]
    )

    for index, layer in enumerate(stackup.layers, start=1):
        if isinstance(layer, CopperLayer):
            copper_number = stackup.copper_layer_number(index - 1)
            lines.extend(
                [
                    f"{index + 1}. L{copper_number}",
                    f"   Copper type: {layer.copper_type}",
                    f"   Thickness: {format_stackup_thickness(layer.thickness_mm, unit, is_copper=True)}",
                    f"   Surface roughness: {format_roughness_um(layer.roughness_um)}",
                    "",
                ]
            )
            continue

        manufacturer = stackup.dielectric_manufacturer(layer, catalog) or ""
        family = stackup.dielectric_family(layer, catalog) or ""
        construction = stackup.dielectric_construction(layer, catalog) or ""
        thickness_mm = stackup.dielectric_thickness_mm(layer, catalog)
        resin_pct = stackup.dielectric_resin_content_pct(layer, catalog)
        freq = stackup.dielectric_frequency_ghz_or_none(layer, catalog)
        dk, df = stackup.dielectric_dk_df_or_none(layer, catalog)
        lines.extend(
            [
                f"{index + 1}. {dielectric_type_display_name(layer.dielectric_type)} dielectric",
                f"   Material ID: {layer.material_id}",
                f"   Manufacturer: {manufacturer}",
                f"   Family: {family}",
                f"   Construction: {construction}",
                f"   Thickness: {format_stackup_thickness(thickness_mm, unit, is_copper=False) if thickness_mm is not None else ''}",
                f"   Resin content: {f'{resin_pct:.1f}%' if resin_pct is not None else ''}",
                f"   Frequency: {format_frequency_ghz(freq) if freq is not None else ''}",
                f"   Dk: {f'{dk:.3f}' if dk is not None else ''}",
                f"   Df: {f'{df:.4f}' if df is not None else ''}",
                f"   Max freq in datasheet: {format_frequency_ghz(catalog.find(layer.material_id).max_freq_ghz) if layer.material_id and catalog.get(layer.material_id) else ''}",
                f"   Freq sweep: {catalog.find(layer.material_id).frequency_summary if layer.material_id and catalog.get(layer.material_id) else ''}",
                "",
            ]
        )

    lines.extend(
        [
            f"{len(stackup.layers) + 2}. Bottom solder mask",
            f"   Thickness: {format_stackup_thickness(soldermask.thickness_mm, unit, is_copper=False)}",
            f"   Manufacturer: {soldermask.manufacturer}",
            f"   Frequency: {format_frequency_ghz(soldermask.freq_ghz)}",
            f"   Dk: {soldermask.dk:.3f}",
            f"   Df: {soldermask.df:.4f}",
            "",
        ]
    )

    output = "\n".join(lines).rstrip() + "\n"
    if impedance_workspace is None:
        return output

    workspace_payload = json.dumps(
        impedance_workspace_to_dict(impedance_workspace, stackup),
        indent=2,
        ensure_ascii=True,
    )
    workspace_lines = [
        "",
        "Impedance workspace data",
        "------------------------",
        _IMPEDANCE_WORKSPACE_START,
        *workspace_payload.splitlines(),
        _IMPEDANCE_WORKSPACE_END,
        "",
    ]
    return output + "\n".join(workspace_lines)


def _parse_frequency_value(value: str) -> float:
    match = _FREQUENCY_PATTERN.search(value.strip())
    if not match:
        raise ValueError(f"Could not parse frequency value: {value!r}")
    numeric = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "mhz":
        return numeric / 1000.0
    return numeric


def _parse_primary_thickness_mm(value: str) -> float:
    match = _THICKNESS_PATTERN.search(value.strip())
    if not match:
        raise ValueError(f"Could not parse thickness value: {value!r}")
    numeric = float(match.group(1))
    unit = match.group(2)
    return from_display(numeric, unit)


def _parse_percent_value(value: str) -> float:
    match = _PERCENT_PATTERN.search(value.strip())
    if not match:
        raise ValueError(f"Could not parse percent value: {value!r}")
    return float(match.group(1))


def _parse_optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _parse_xpedition_field_line(line: str) -> dict[str, str]:
    payload = line.strip()
    if payload.startswith("(") and payload.endswith(")"):
        payload = payload[1:-1].strip()
    if payload.startswith("LAYER "):
        payload = payload[6:].strip()
    fields: dict[str, str] = {}
    for key, raw_value in _XPEDITION_FIELD_PATTERN.findall(payload):
        value = raw_value[1:-1] if raw_value.startswith('"') and raw_value.endswith('"') else raw_value
        fields[key] = value
    return fields


def _parse_xpedition_thickness_mm(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value) * 1000.0


def _parse_xpedition_frequency_ghz(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value) / 1_000_000_000.0


def _parse_xpedition_roughness_um(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value) * 1_000_000.0


def _parse_xpedition_dielectric_description(
    description: str,
    *,
    dielectric_type: str,
) -> tuple[str | None, str | None]:
    text = description.strip()
    if not text:
        return None, None

    family = text
    construction = None
    if "," in text:
        family, construction = [part.strip() for part in text.split(",", 1)]

    family = re.sub(r"-(?:PP|Core)\s*$", "", family).strip()
    if is_prepreg_dielectric_type(dielectric_type):
        family = re.sub(r"\s*-\s*PP\s*$", "", family).strip()
    else:
        family = re.sub(r"\s*-\s*Core\s*$", "", family).strip()

    return family or None, construction or None


def _match_xpedition_dielectric_entry(
    catalog: MaterialCatalog,
    *,
    dielectric_type: str,
    family: str | None,
    construction: str | None,
    thickness_mm: float | None,
    freq_ghz: float | None,
    dk: float | None,
    df: float | None,
) -> str:
    if not family:
        return ""

    candidates = catalog.filter_entries(
        material_type=catalog_material_type_for_dielectric(dielectric_type),
        family=family,
    )
    if construction:
        exact = [entry for entry in candidates if entry.construction == construction]
        if exact:
            candidates = exact
    if not candidates:
        return ""

    scored: list[tuple[tuple[float, float, float, float], str]] = []
    for entry in candidates:
        entry_freq = entry.closest_frequency(freq_ghz) if freq_ghz is not None else entry.max_freq_ghz
        score = (
            abs(entry.thickness_mm - thickness_mm) if thickness_mm is not None else 0.0,
            abs(entry.dk_at(entry_freq) - dk) if dk is not None else 0.0,
            abs(entry.df_at(entry_freq) - df) if df is not None else 0.0,
            abs(entry_freq - freq_ghz) if freq_ghz is not None else 0.0,
        )
        scored.append((score, entry.id))
    scored.sort(key=lambda item: item[0])
    return scored[0][1]


def _parse_indexed_blocks(lines: list[str]) -> list[tuple[str, dict[str, str]]]:
    blocks: list[tuple[str, dict[str, str]]] = []
    current_title: str | None = None
    current_fields: dict[str, str] = {}

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line:
            continue
        header_match = re.match(r"^\d+\.\s+(.+)$", line)
        if header_match:
            if current_title is not None:
                blocks.append((current_title, current_fields))
            current_title = header_match.group(1).strip()
            current_fields = {}
            continue
        if current_title is None:
            continue
        if ":" in line:
            key, value = line.strip().split(":", 1)
            current_fields[key.strip()] = value.strip()

    if current_title is not None:
        blocks.append((current_title, current_fields))
    return blocks


def _resolve_dielectric_entry(
    catalog: MaterialCatalog,
    *,
    material_type: str,
    fields: dict[str, str],
) -> tuple[str, float]:
    material_id = fields.get("Material ID")
    selected_freq_ghz = _parse_frequency_value(fields["Frequency"])
    if material_id:
        entry = catalog.find(material_id)
        return entry.id, entry.closest_frequency(selected_freq_ghz)

    manufacturer = fields["Manufacturer"]
    family = fields["Family"]
    construction = fields.get("Construction", "")
    thickness_mm = _parse_primary_thickness_mm(fields["Thickness"])
    resin_content_pct = _parse_percent_value(fields["Resin content"])
    dk = float(fields["Dk"])
    df = float(fields["Df"])

    candidates = catalog.filter_entries(
        material_type=catalog_material_type_for_dielectric(material_type),
        manufacturer=manufacturer,
        family=family,
    )
    if construction:
        exact_construction = [entry for entry in candidates if entry.construction == construction]
        if exact_construction:
            candidates = exact_construction

    if not candidates:
        raise ValueError(
            f"No catalog entry matches {material_type} {manufacturer} / {family} / {construction!r}."
        )

    scored: list[tuple[tuple[float, float, float, float, float], str, float]] = []
    for entry in candidates:
        freq = entry.closest_frequency(selected_freq_ghz)
        score = (
            abs(entry.thickness_mm - thickness_mm),
            abs(entry.resin_content_pct - resin_content_pct),
            abs(freq - selected_freq_ghz),
            abs(entry.dk_at(freq) - dk),
            abs(entry.df_at(freq) - df),
        )
        scored.append((score, entry.id, freq))

    scored.sort(key=lambda item: item[0])
    best_score, best_id, best_freq = scored[0]
    if best_score[0] > 0.02:
        raise ValueError(
            f"Could not safely match dielectric entry for {manufacturer} / {family} / {construction!r}."
        )
    return best_id, best_freq


def _parse_soldermask_settings(fields: dict[str, str]) -> SolderMaskSettings:
    return SolderMaskSettings(
        thickness_mm=_parse_primary_thickness_mm(fields["Thickness"]),
        dk=float(fields["Dk"]),
        df=float(fields["Df"]),
        freq_ghz=_parse_frequency_value(fields["Frequency"]),
        manufacturer=fields.get("Manufacturer", "TAIYO AMERICA"),
    )


def _extract_impedance_workspace_payload(lines: list[str]) -> dict[str, object] | None:
    try:
        start_index = lines.index(_IMPEDANCE_WORKSPACE_START)
        end_index = lines.index(_IMPEDANCE_WORKSPACE_END, start_index + 1)
    except ValueError:
        return None

    payload_text = "\n".join(lines[start_index + 1 : end_index]).strip()
    if not payload_text:
        return None
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse saved impedance workspace data: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Saved impedance workspace data is malformed.")
    return payload


def import_stackup_text(
    text: str,
    catalog: MaterialCatalog,
) -> tuple[Stackup, str, ImpedanceWorkspaceState | None]:
    lines = text.splitlines()
    if len(lines) < 2 or lines[0].strip() != "PCB Stackup Export":
        raise ValueError("This file is not a StackUp Editor text export.")

    display_unit = "mm"
    for line in lines:
        if line.startswith("Display unit:"):
            display_unit = line.split(":", 1)[1].strip()
            break
    if display_unit not in SUPPORTED_UNITS:
        raise ValueError(f"Unsupported display unit in import file: {display_unit!r}")

    blocks = _parse_indexed_blocks(lines)
    if len(blocks) < 3:
        raise ValueError("The import file does not contain enough stackup sections.")

    top_title, top_fields = blocks[0]
    bottom_title, bottom_fields = blocks[-1]
    if top_title != "Top solder mask" or bottom_title != "Bottom solder mask":
        raise ValueError("The import file must start and end with solder mask sections.")

    soldermask = _parse_soldermask_settings(top_fields)
    bottom_soldermask = _parse_soldermask_settings(bottom_fields)
    if (
        abs(soldermask.thickness_mm - bottom_soldermask.thickness_mm) > 1e-9
        or abs(soldermask.dk - bottom_soldermask.dk) > 1e-9
        or abs(soldermask.df - bottom_soldermask.df) > 1e-9
        or abs(soldermask.freq_ghz - bottom_soldermask.freq_ghz) > 1e-9
        or soldermask.manufacturer != bottom_soldermask.manufacturer
    ):
        raise ValueError("Top and bottom solder mask settings differ; this stackup model supports one shared solder mask profile.")

    layers = []
    for title, fields in blocks[1:-1]:
        if re.fullmatch(r"L\d+", title):
            layers.append(
                CopperLayer(
                    thickness_mm=_parse_primary_thickness_mm(fields["Thickness"]),
                    copper_type=fields["Copper type"],
                )
            )
            continue

        if title == "Prepreg dielectric":
            material_type = "prepreg"
        elif title == "Core dielectric":
            material_type = "core"
        elif title == "Dummy Core dielectric":
            material_type = "dummy_core"
        elif title == "Etched Core dielectric":
            material_type = "etched_core"
        elif title == "No-Flow Prepreg dielectric":
            material_type = "no_flow_prepreg"
        else:
            raise ValueError(f"Unsupported section title in import file: {title!r}")

        material_id, selected_freq_ghz = _resolve_dielectric_entry(
            catalog,
            material_type=material_type,
            fields=fields,
        )
        layers.append(
            DielectricLayer(
                dielectric_type=material_type,
                material_id=material_id,
                selected_freq_ghz=selected_freq_ghz,
            )
        )

    stackup = Stackup(layers=layers, soldermask=soldermask)
    workspace_payload = _extract_impedance_workspace_payload(lines)
    impedance_workspace = (
        impedance_workspace_from_dict(workspace_payload, stackup)
        if workspace_payload is not None
        else None
    )
    return stackup, display_unit, impedance_workspace


XPEDITION_SIGNAL_COLORS = (
    16711680,
    65280,
    16776960,
    128,
    255,
    32896,
    8388736,
    8421376,
    16711935,
    65535,
    8388608,
    32768,
)


def _xpedition_number(value: float) -> str:
    return format(value, ".6g")


def _xpedition_thickness_from_mm(thickness_mm: float) -> str:
    return _xpedition_number(thickness_mm / 1000.0)


def _xpedition_frequency_hz(freq_ghz: float) -> str:
    return _xpedition_number(freq_ghz * 1_000_000_000.0)


def _xpedition_roughness_m(roughness_um: float | None) -> str | None:
    if roughness_um is None:
        return None
    return _xpedition_number(roughness_um / 1_000_000.0)


def _xpedition_signal_description(stackup: Stackup, index: int) -> str:
    copper_number = stackup.copper_layer_number(index)
    if copper_number in (1, stackup.copper_count()):
        return "Microstrip"
    return "Stripline"


def _xpedition_dielectric_class(dielectric_type: str) -> str:
    """Map editor-only dielectric types to Xpedition's core/prepreg classes."""
    if is_no_flow_prepreg_type(dielectric_type):
        return "prepreg"
    if is_dummy_core_type(dielectric_type):
        return "core"
    if is_etched_core_type(dielectric_type):
        return "core"
    return "prepreg" if is_prepreg_dielectric_type(dielectric_type) else "core"


def _xpedition_prepreg_flag(dielectric_type: str) -> int:
    return 1 if _xpedition_dielectric_class(dielectric_type) == "prepreg" else 0


def _xpedition_description_text(value: str) -> str:
    return value.replace('"', "'").strip()


def _xpedition_special_dielectric_description(
    stackup: Stackup,
    layer: DielectricLayer,
    catalog: MaterialCatalog,
) -> str | None:
    if not (
        is_dummy_core_type(layer.dielectric_type)
        or is_etched_core_type(layer.dielectric_type)
        or is_no_flow_prepreg_type(layer.dielectric_type)
    ):
        return None

    manufacturer = stackup.dielectric_manufacturer(layer, catalog) or ""
    family = stackup.dielectric_family(layer, catalog) or ""
    construction = stackup.dielectric_construction(layer, catalog) or ""
    resin = stackup.dielectric_resin_content_pct(layer, catalog)

    if is_dummy_core_type(layer.dielectric_type):
        material_name = f"{family}-DummyCore" if family else "DummyCore"
    elif is_etched_core_type(layer.dielectric_type):
        material_name = f"{family}-EtchedCore" if family else "EtchedCore"
    else:
        catalog_name = " ".join(
            value for value in (manufacturer.upper(), family) if value
        )
        material_name = f"{catalog_name}-No Flow PP" if catalog_name else "No Flow PP"

    parts = [material_name]
    if construction:
        parts.append(construction)
    if resin is not None:
        parts.append(f"RC %{_xpedition_number(resin)}")
    return _xpedition_description_text(" ".join(parts))


def _xpedition_dielectric_description(stackup: Stackup, layer: DielectricLayer, catalog: MaterialCatalog) -> str:
    special_description = _xpedition_special_dielectric_description(stackup, layer, catalog)
    if special_description is not None:
        return special_description
    if layer.description_override:
        return layer.description_override
    family = stackup.dielectric_family(layer, catalog) or ""
    construction = stackup.dielectric_construction(layer, catalog) or ""
    suffix = "PP" if _xpedition_dielectric_class(layer.dielectric_type) == "prepreg" else "Core"
    if family and construction:
        return f"{family}-{suffix}, {construction}"
    if family:
        return f"{family}-{suffix}"
    return ""


def _xpedition_dielectric_visible(stackup: Stackup, index: int) -> int:
    if index > 0 and isinstance(stackup.layers[index - 1], DielectricLayer):
        return 1
    return 0


@dataclass(frozen=True)
class _RigidFlexXpeditionDielectric:
    name: str
    description: str
    layer_type: str
    thickness_mm: float
    dk: float
    df: float
    freq_ghz: float
    prepreg: int
    conformal: int = 0


def _xpedition_rigid_flex_dielectric_description(
    stackup: Stackup,
    layer: DielectricLayer,
    catalog: MaterialCatalog,
) -> str:
    special_description = _xpedition_special_dielectric_description(stackup, layer, catalog)
    if special_description is not None:
        return special_description
    if layer.description_override:
        return _xpedition_description_text(layer.description_override)
    family = stackup.dielectric_family(layer, catalog) or ""
    construction = stackup.dielectric_construction(layer, catalog) or ""
    resin = stackup.dielectric_resin_content_pct(layer, catalog)
    suffix = "PP" if _xpedition_dielectric_class(layer.dielectric_type) == "prepreg" else "Core"
    parts = [f"{family}-{suffix}" if family else suffix]
    if construction:
        parts.append(construction)
    if resin is not None:
        parts.append(f"RC %{_xpedition_number(resin)}")
    return _xpedition_description_text(" ".join(parts))


def _xpedition_flex_core_description(layer: FlexCoreLayer) -> str:
    return _xpedition_description_text(
        " ".join(
            value
            for value in (layer.family, layer.variant_code, layer.construction)
            if value
        )
    )


def _xpedition_coverlay_description(coverlay: CoverlaySettings, component: str) -> str:
    suffix = "PI" if component == "pi" else "ADH"
    return _xpedition_description_text(
        " ".join(
            value
            for value in (coverlay.manufacturer.upper(), coverlay.family, "CVL", suffix)
            if value
        )
    )


def _xpedition_flex_core_equivalent(left: FlexCoreLayer, right: FlexCoreLayer) -> bool:
    left_dk, left_df = left.dk_at(left.closest_frequency(left.selected_freq_ghz)), left.df_at(
        left.closest_frequency(left.selected_freq_ghz)
    )
    right_dk, right_df = right.dk_at(right.closest_frequency(right.selected_freq_ghz)), right.df_at(
        right.closest_frequency(right.selected_freq_ghz)
    )
    return (
        left.copper_type == right.copper_type
        and abs(left.copper_thickness_top_mm - right.copper_thickness_top_mm) <= 1e-9
        and abs(left.copper_thickness_bottom_mm - right.copper_thickness_bottom_mm) <= 1e-9
        and abs(left.dielectric_thickness_mm - right.dielectric_thickness_mm) <= 1e-9
        and abs(left_dk - right_dk) <= 1e-9
        and abs(left_df - right_df) <= 1e-9
    )


def _rigid_flex_xpedition_slot_definitions(
    zones: list[RigidFlexZoneState],
) -> dict[int, tuple[CoverlaySettings, FlexCoreLayer, int]]:
    canonical_flex_indices: list[int] = []
    for zone_index, zone in enumerate(zones):
        if zone.kind.strip().lower() != "flex":
            continue
        definition_index = (
            zone.definition_zone_index
            if zone.definition_zone_index is not None
            else zone_index
        )
        if definition_index not in canonical_flex_indices:
            canonical_flex_indices.append(definition_index)
    flex_number_by_definition = {
        definition_index: number
        for number, definition_index in enumerate(canonical_flex_indices, start=1)
    }

    slot_definitions: dict[int, tuple[CoverlaySettings, FlexCoreLayer, int]] = {}
    for zone_index, zone in enumerate(zones[1:], start=1):
        if zone.kind.strip().lower() != "flex":
            continue
        definition_index = (
            zone.definition_zone_index
            if zone.definition_zone_index is not None
            else zone_index
        )
        flex_number = flex_number_by_definition[definition_index]
        parent_indices = zone.parent_zone_indices or (
            [zone.parent_zone_index] if zone.parent_zone_index is not None else []
        )
        if parent_indices != [0]:
            raise ValueError(
                "A single Xpedition .stk file can only flatten Flex Parts connected directly to the Master Rigid Part."
            )
        coverlay = zone.stackup.coverlay
        if coverlay is None:
            raise ValueError(f"{zone.label or f'Flex Part {zone_index + 1}'} has no coverlay definition.")
        slot_ids = zone.stackup.flex_sandwich_slot_ids()
        flex_cores = [layer for layer in zone.stackup.layers if isinstance(layer, FlexCoreLayer)]
        if len(slot_ids) != len(flex_cores):
            raise ValueError(f"{zone.label or f'Flex Part {zone_index + 1}'} has inconsistent flex-slot data.")
        for slot_id, flex_core in zip(slot_ids, flex_cores):
            existing = slot_definitions.get(slot_id)
            if existing is not None:
                existing_coverlay, existing_core, existing_flex_number = existing
                if (
                    existing_coverlay != coverlay
                    or not _xpedition_flex_core_equivalent(existing_core, flex_core)
                    or existing_flex_number != flex_number
                ):
                    raise ValueError(
                        f"Flex slot {slot_id} has conflicting material definitions and cannot be flattened into one STK layer."
                    )
                continue
            slot_definitions[slot_id] = (coverlay, flex_core, flex_number)
    return slot_definitions


def _rigid_flex_xpedition_flat_layers(
    zones: list[RigidFlexZoneState],
    catalog: MaterialCatalog,
) -> list[CopperLayer | _RigidFlexXpeditionDielectric]:
    master = zones[0]
    stackup = master.stackup
    slot_definitions = _rigid_flex_xpedition_slot_definitions(zones)
    master_flex_indices = [
        index for index, layer in enumerate(stackup.layers) if isinstance(layer, FlexCoreLayer)
    ]
    sorted_slots = sorted(slot_definitions)
    if len(master_flex_indices) != len(sorted_slots):
        raise ValueError(
            "The Master Rigid Part flex cores do not match its connected Flex Part definitions."
        )
    if master.flex_slot_coverage is not None and set(master.flex_slot_coverage) != set(sorted_slots):
        raise ValueError("The Master Rigid Part flex-slot coverage is incomplete or inconsistent.")

    coverlay_by_layer_index: dict[int, CoverlaySettings] = {}
    flex_name_by_layer_index: dict[int, str] = {}
    for layer_index, slot_id in zip(master_flex_indices, sorted_slots):
        coverlay, flex_core, flex_number = slot_definitions[slot_id]
        master_core = stackup.layers[layer_index]
        if not isinstance(master_core, FlexCoreLayer) or not _xpedition_flex_core_equivalent(master_core, flex_core):
            raise ValueError(
                f"Master flex core {slot_id} does not match its connected Flex Part material definition."
            )
        coverlay_by_layer_index[layer_index] = coverlay
        flex_name_by_layer_index[layer_index] = f"FlexPart{flex_number}FlexCore"

    flat_layers: list[CopperLayer | _RigidFlexXpeditionDielectric] = []
    rigid_dielectric_number = 0
    flex_core_number = 0

    def add_coverlay_component(
        coverlay: CoverlaySettings,
        component: str,
        *,
        flex_core_name: str,
        side: str,
    ) -> None:
        dk, df = coverlay.component_dk_df(component)
        freq = coverlay.component_frequency_ghz(component)
        if dk is None or df is None or freq is None:
            raise ValueError(f"The {component} coverlay component has incomplete Dk/Df data.")
        flex_part_name = flex_core_name.removesuffix("FlexCore")
        side_name = "Top" if side == "top" else "Bot"
        if component == "pi":
            name = f"{flex_part_name}{side_name}CoverlayPI"
            layer_type = "COVER"
        else:
            name = f"{flex_part_name}{side_name}CoverlayAdhessive"
            layer_type = "ADHESIVE"
        flat_layers.append(
            _RigidFlexXpeditionDielectric(
                name=name,
                description=_xpedition_coverlay_description(coverlay, component),
                layer_type=layer_type,
                thickness_mm=coverlay.component_thickness_mm(component),
                dk=dk,
                df=df,
                freq_ghz=freq,
                prepreg=0,
            )
        )

    def dielectric_record(
        owner_stackup: Stackup,
        layer: DielectricLayer,
        *,
        name: str,
    ) -> _RigidFlexXpeditionDielectric:
        freq = owner_stackup.dielectric_frequency_ghz_or_none(layer, catalog)
        dk, df = owner_stackup.dielectric_dk_df_or_none(layer, catalog)
        thickness_mm = owner_stackup.dielectric_thickness_mm(layer, catalog)
        if freq is None or dk is None or df is None or thickness_mm is None:
            raise ValueError(f"{name} has incomplete material data.")
        return _RigidFlexXpeditionDielectric(
            name=name,
            description=_xpedition_rigid_flex_dielectric_description(
                owner_stackup,
                layer,
                catalog,
            ),
            layer_type="DIELECTRIC",
            thickness_mm=thickness_mm,
            dk=dk,
            df=df,
            freq_ghz=freq,
            prepreg=_xpedition_prepreg_flag(layer.dielectric_type),
        )

    def soldermask_record(
        *,
        name: str,
        soldermask: SolderMaskSettings,
    ) -> _RigidFlexXpeditionDielectric:
        description = _xpedition_description_text(
            f"{soldermask.manufacturer} SolderMask "
            f"{_xpedition_number(soldermask.thickness_mm * 1000.0)}um"
        )
        return _RigidFlexXpeditionDielectric(
            name=name,
            description=description,
            layer_type="DIELECTRIC",
            thickness_mm=soldermask.thickness_mm,
            dk=soldermask.dk,
            df=soldermask.df,
            freq_ghz=soldermask.freq_ghz,
            prepreg=0,
            conformal=1,
        )

    copper_positions = [
        index for index, layer in enumerate(stackup.layers) if isinstance(layer, CopperLayer)
    ]
    master_layer_index_by_id = {
        id(layer): index for index, layer in enumerate(stackup.layers)
    }
    master_coppers = [
        stackup.layers[index]
        for index in copper_positions
        if isinstance(stackup.layers[index], CopperLayer)
    ]
    if len(master_coppers) < 2:
        raise ValueError("The Master Rigid Part must contain at least two copper layers.")

    master_intervals: dict[int, list[DielectricLayer | FlexCoreLayer]] = {}
    master_flex_start: dict[int, tuple[CoverlaySettings, str]] = {}
    master_flex_end: dict[int, tuple[CoverlaySettings, str]] = {}
    master_no_flow_name_by_layer_id: dict[int, str] = {}
    no_flow_dielectric_number = 0
    for lower_number, (left_index, right_index) in enumerate(
        zip(copper_positions, copper_positions[1:]),
        start=1,
    ):
        interval = [
            layer
            for layer in stackup.layers[left_index + 1 : right_index]
            if isinstance(layer, (DielectricLayer, FlexCoreLayer))
        ]
        master_intervals[lower_number] = interval
        for layer in interval:
            if not (
                isinstance(layer, DielectricLayer)
                and is_no_flow_prepreg_type(layer.dielectric_type)
            ):
                continue
            no_flow_dielectric_number += 1
            no_flow_name = (
                f"MasterRigidNoFLowDielectric{no_flow_dielectric_number}"
            )
            master_no_flow_name_by_layer_id[id(layer)] = no_flow_name
        flex_layers = [
            (layer_index, layer)
            for layer_index, layer in enumerate(
                stackup.layers[left_index + 1 : right_index],
                start=left_index + 1,
            )
            if isinstance(layer, FlexCoreLayer)
        ]
        if len(flex_layers) > 1:
            raise ValueError(
                f"Master copper interval L{lower_number}-L{lower_number + 1} "
                "contains more than one flex core."
            )
        if flex_layers:
            flex_layer_index, _flex_layer = flex_layers[0]
            coverlay = coverlay_by_layer_index[flex_layer_index]
            flex_name = flex_name_by_layer_index[flex_layer_index]
            master_flex_start[lower_number] = (coverlay, flex_name)
            master_flex_end[lower_number + 1] = (coverlay, flex_name)

    branch_top_masks: dict[int, list[_RigidFlexXpeditionDielectric]] = {}
    branch_bottom_masks: dict[int, list[_RigidFlexXpeditionDielectric]] = {}
    branch_dielectrics_before_copper: dict[
        int,
        list[_RigidFlexXpeditionDielectric],
    ] = {}
    rigid_part_fallback_number = 2
    for zone_index, zone in enumerate(zones[1:], start=1):
        if zone.kind.strip().lower() != "rigid":
            continue
        numbers = zone.global_copper_numbers
        if numbers is None:
            raise ValueError(
                f"{zone.label or f'Rigid Part {rigid_part_fallback_number}'} "
                "has no global copper-layer numbering."
            )
        if (
            len(numbers) != zone.stackup.copper_count()
            or numbers != sorted(set(numbers))
            or min(numbers, default=0) < 1
            or max(numbers, default=0) > len(master_coppers)
        ):
            raise ValueError(
                f"{zone.label or f'Rigid Part {rigid_part_fallback_number}'} "
                "has invalid global copper-layer numbering."
            )

        label_match = re.search(r"rigid\s*part\s*(\d+)", zone.label, re.IGNORECASE)
        part_number = (
            int(label_match.group(1))
            if label_match is not None
            else rigid_part_fallback_number
        )
        rigid_part_fallback_number = max(rigid_part_fallback_number + 1, part_number + 1)
        prefix = f"RigidPart{part_number}"
        branch_top_masks.setdefault(numbers[0], []).append(
            soldermask_record(
                name=f"{prefix}TopSoldermask",
                soldermask=zone.stackup.soldermask,
            )
        )
        branch_bottom_masks.setdefault(numbers[-1], []).append(
            soldermask_record(
                name=f"{prefix}BotSoldermask",
                soldermask=zone.stackup.soldermask,
            )
        )

        branch_copper_positions = [
            index
            for index, layer in enumerate(zone.stackup.layers)
            if isinstance(layer, CopperLayer)
        ]
        dielectric_number = 0
        no_flow_dielectric_number = 0
        for local_interval, (left_index, right_index) in enumerate(
            zip(branch_copper_positions, branch_copper_positions[1:])
        ):
            upper_global_number = numbers[local_interval + 1]
            for layer in zone.stackup.layers[left_index + 1 : right_index]:
                if isinstance(layer, FlexCoreLayer):
                    # The connected Flex Part is a single physical construction and
                    # is emitted once from the master's global flex slot.
                    continue
                if not isinstance(layer, DielectricLayer):
                    continue
                dielectric_number += 1
                if is_no_flow_prepreg_type(layer.dielectric_type):
                    no_flow_dielectric_number += 1
                    layer_name = (
                        f"{prefix}NoFLowDielectric{no_flow_dielectric_number}"
                    )
                else:
                    layer_name = f"{prefix}Dielectric{dielectric_number}"
                branch_dielectrics_before_copper.setdefault(
                    upper_global_number,
                    [],
                ).append(
                    dielectric_record(
                        zone.stackup,
                        layer,
                        name=layer_name,
                    )
                )

    for copper_number, copper in enumerate(master_coppers, start=1):
        if copper_number == 1:
            flat_layers.extend(branch_top_masks.get(copper_number, []))
        else:
            lower_number = copper_number - 1
            for layer in master_intervals.get(lower_number, []):
                if isinstance(layer, FlexCoreLayer):
                    flex_core_number += 1
                    freq = stackup.dielectric_frequency_ghz_or_none(layer, catalog)
                    dk, df = stackup.dielectric_dk_df_or_none(layer, catalog)
                    if freq is None or dk is None or df is None:
                        raise ValueError(
                            f"Flex core {flex_core_number} has incomplete Dk/Df data."
                        )
                    layer_index = master_layer_index_by_id[id(layer)]
                    flat_layers.append(
                        _RigidFlexXpeditionDielectric(
                            name=flex_name_by_layer_index[layer_index],
                            description=_xpedition_flex_core_description(layer),
                            layer_type="FLEX",
                            thickness_mm=layer.dielectric_thickness_mm,
                            dk=dk,
                            df=df,
                            freq_ghz=freq,
                            prepreg=2,
                        )
                    )
                    continue
                rigid_dielectric_number += 1
                flat_layers.append(
                    dielectric_record(
                        stackup,
                        layer,
                        name=master_no_flow_name_by_layer_id.get(
                            id(layer),
                            f"MasterRigidDielectric{rigid_dielectric_number}",
                        ),
                    )
                )
            flat_layers.extend(
                branch_dielectrics_before_copper.get(copper_number, [])
            )
            flat_layers.extend(branch_top_masks.get(copper_number, []))
            flex_start = master_flex_start.get(copper_number)
            if flex_start is not None:
                coverlay, flex_core_name = flex_start
                add_coverlay_component(
                    coverlay,
                    "pi",
                    flex_core_name=flex_core_name,
                    side="top",
                )
                add_coverlay_component(
                    coverlay,
                    "adhesive",
                    flex_core_name=flex_core_name,
                    side="top",
                )

        flat_layers.append(copper)

        flex_end = master_flex_end.get(copper_number)
        if flex_end is not None:
            coverlay, flex_core_name = flex_end
            add_coverlay_component(
                coverlay,
                "adhesive",
                flex_core_name=flex_core_name,
                side="bottom",
            )
            add_coverlay_component(
                coverlay,
                "pi",
                flex_core_name=flex_core_name,
                side="bottom",
            )
        flat_layers.extend(branch_bottom_masks.get(copper_number, []))
    return flat_layers


XPEDITION_RIGID_FLEX_SIGNAL_COLORS = (
    16711680,
    65280,
    16776960,
    255,
    16711935,
    65535,
    8388608,
    32768,
)


def _xpedition_signal_environment(
    flat_layers: list[CopperLayer | _RigidFlexXpeditionDielectric],
    index: int,
    soldermask: SolderMaskSettings,
) -> tuple[float, float, float]:
    signal_indices = [item_index for item_index, item in enumerate(flat_layers) if isinstance(item, CopperLayer)]
    signal_ordinal = signal_indices.index(index)
    if signal_ordinal in (0, len(signal_indices) - 1):
        return soldermask.dk, soldermask.df, soldermask.freq_ghz
    adjacent = [
        item
        for item in (flat_layers[index - 1], flat_layers[index + 1])
        if isinstance(item, _RigidFlexXpeditionDielectric)
    ]
    total_thickness = sum(item.thickness_mm for item in adjacent)
    if not adjacent or total_thickness <= 0.0:
        return 1.0, 0.0, 1.0
    dk = sum(item.dk * item.thickness_mm for item in adjacent) / total_thickness
    df = sum(item.df * item.thickness_mm for item in adjacent) / total_thickness
    freq = min(item.freq_ghz for item in adjacent)
    return dk, df, freq


def export_rigid_flex_xpedition(
    zones: list[RigidFlexZoneState],
    catalog: MaterialCatalog,
) -> str:
    """Flatten a branching rigid-flex project into one Xpedition STK layer list."""
    _validate_branching_rigid_flex_zones(zones)
    master = zones[0].stackup
    flat_layers = _rigid_flex_xpedition_flat_layers(zones, catalog)
    enable_roughness = any(
        isinstance(layer, CopperLayer) and layer.roughness_um not in (None, 0.0)
        for layer in flat_layers
    )
    mask = master.soldermask
    mask_description = _xpedition_description_text(
        f"{mask.manufacturer} SolderMask {_xpedition_number(mask.thickness_mm * 1000.0)}um"
    )
    lines = [
        "{STK_FILE}",
        "{VERSION=1.0}",
        "{APPLICATION_SETTINGS",
        f"\t(ENABLE_ROUGHNESS={1 if enable_roughness else 0})",
        "\t(RMS_VS_RA=0)",
        "\t(ROUGHNESS_MODEL=1)",
        "\t(ROUGHNESS_FACTOR=2)",
        "\t(TRAPEZOIDAL_TRACE=0)",
        "}",
        "{STACKUP",
        "\t(OPTIONS USE_DIE_FOR_METAL=0 LOCK_ATTACHED_LAYER=0)",
        (
            f'\t(LAYER NAME="MasterRigidTopSoldermask" DESCRIPTION="{mask_description}" TYPE=DIELECTRIC COLOR=0 FILL=0 VISIBLE=0 '
            f"THICKNESS={_xpedition_thickness_from_mm(mask.thickness_mm)} ER={_xpedition_number(mask.dk)} "
            f"TG={_xpedition_number(mask.df)} ER_FREQ={_xpedition_frequency_hz(mask.freq_ghz)} "
            f'THC=0.3 CONFORMAL=1 PREPREG=0 MATERIAL="Dielectric" ATCHMETAL=0)'
        ),
    ]

    signal_number = 0
    for index, layer in enumerate(flat_layers):
        if isinstance(layer, CopperLayer):
            signal_number += 1
            color = XPEDITION_RIGID_FLEX_SIGNAL_COLORS[
                (signal_number - 1) % len(XPEDITION_RIGID_FLEX_SIGNAL_COLORS)
            ]
            roughness = _xpedition_roughness_m(layer.roughness_um)
            description = layer.copper_type or "Copper"
            if layer.roughness_um is not None:
                description += f" - Rq:{_xpedition_number(layer.roughness_um)}um"
            dk, df, freq = _xpedition_signal_environment(flat_layers, index, mask)
            segments = [
                f'(LAYER NAME="L{signal_number}" DESCRIPTION="{_xpedition_description_text(description)}" TYPE=SIGNAL',
                f"COLOR={color}",
                "FILL=0",
                "VISIBLE=1",
                f"THICKNESS={_xpedition_thickness_from_mm(layer.thickness_mm)}",
                f"ER={_xpedition_number(dk)}",
                f"TG={_xpedition_number(df)}",
                f"ER_FREQ={_xpedition_frequency_hz(freq)}",
                "THC=393.693",
                "PLATING=0",
                "RB=1.724e-08",
                "TC=0.00393",
                "TTW=0.0001524",
                "DZ0=75",
                'MATERIAL="Metal"',
            ]
            if enable_roughness and roughness not in (None, "0"):
                segments.extend((f"ROUGH_TOP={roughness}", f"ROUGH_BOT={roughness}"))
            segments.extend(("ETCHFACTOR=0.741", "NARROWTOP=1)"))
            lines.append("\t" + " ".join(segments))
            continue

        lines.append(
            (
                f'\t(LAYER NAME="{layer.name}" DESCRIPTION="{layer.description}" TYPE={layer.layer_type} '
                f"COLOR=0 FILL=0 VISIBLE=0 THICKNESS={_xpedition_thickness_from_mm(layer.thickness_mm)} "
                f"ER={_xpedition_number(layer.dk)} TG={_xpedition_number(layer.df)} "
                f"ER_FREQ={_xpedition_frequency_hz(layer.freq_ghz)} THC=0.3 CONFORMAL={layer.conformal} "
                f'PREPREG={layer.prepreg} MATERIAL="Dielectric" ATCHMETAL=0)'
            )
        )

    lines.extend(
        [
            (
                f'\t(LAYER NAME="MasterRigidBotSoldermask" DESCRIPTION="{mask_description}" TYPE=DIELECTRIC COLOR=0 FILL=0 VISIBLE=0 '
                f"THICKNESS={_xpedition_thickness_from_mm(mask.thickness_mm)} ER={_xpedition_number(mask.dk)} "
                f"TG={_xpedition_number(mask.df)} ER_FREQ={_xpedition_frequency_hz(mask.freq_ghz)} "
                f'THC=0.3 CONFORMAL=1 PREPREG=0 MATERIAL="Dielectric" ATCHMETAL=0)'
            ),
            "}",
        ]
    )
    return "\n".join(lines) + "\n"


def export_stackup_xpedition(stackup: Stackup, catalog: MaterialCatalog) -> str:
    soldermask = stackup.soldermask
    enable_roughness = any(
        isinstance(layer, CopperLayer) and layer.roughness_um not in (None, 0.0)
        for layer in stackup.layers
    )
    lines = [
        "{STK_FILE}",
        "{VERSION=1.0}",
        "{APPLICATION_SETTINGS",
        f"\t(ENABLE_ROUGHNESS={1 if enable_roughness else 0})",
        "\t(RMS_VS_RA=1)",
        "\t(ROUGHNESS_MODEL=1)",
        "\t(ROUGHNESS_FACTOR=2)",
        "\t(TRAPEZOIDAL_TRACE=0)",
        "}",
        "{STACKUP",
        "\t(OPTIONS USE_DIE_FOR_METAL=0 LOCK_ATTACHED_LAYER=0)",
        (
            f'\t(LAYER NAME="SolderMaskTop" DESCRIPTION="Solder Mask" TYPE=DIELECTRIC COLOR=0 FILL=0 VISIBLE=0 '
            f"THICKNESS={_xpedition_thickness_from_mm(soldermask.thickness_mm)} ER={_xpedition_number(soldermask.dk)} "
            f"TG={_xpedition_number(soldermask.df)} ER_FREQ={_xpedition_frequency_hz(soldermask.freq_ghz)} "
            f'THC=0.3 CONFORMAL=1 PREPREG=0 MATERIAL="Dielectric" ATCHMETAL=0)'
        ),
    ]

    dielectric_number = 0
    for index, layer in enumerate(stackup.layers):
        if isinstance(layer, CopperLayer):
            copper_number = stackup.copper_layer_number(index)
            color = XPEDITION_SIGNAL_COLORS[(copper_number - 1) % len(XPEDITION_SIGNAL_COLORS)]
            description = _xpedition_signal_description(stackup, index)
            thickness = _xpedition_thickness_from_mm(layer.thickness_mm)
            roughness = _xpedition_roughness_m(layer.roughness_um)
            copper_segments = [
                f'(LAYER NAME="L{copper_number}" DESCRIPTION="{description}" TYPE=SIGNAL',
                f"COLOR={color}",
                "FILL=0",
                "VISIBLE=1",
                f"THICKNESS={thickness}",
                "ER=1",
                "TG=0.02",
                "ER_FREQ=1e+09",
                "THC=393.693",
                "PLATING=0",
                "RB=1.724e-08",
                "TC=0.00393",
                "TTW=0.0001524",
                "DZ0=50",
                'MATERIAL="Metal"',
            ]
            if enable_roughness and roughness is not None and roughness != "0":
                copper_segments.append(f"ROUGH_TOP={roughness}")
                copper_segments.append(f"ROUGH_BOT={roughness}")
            copper_segments.extend(["ETCHFACTOR=0.741", "NARROWTOP=1)"])
            lines.append("\t" + " ".join(copper_segments))
            continue

        dielectric_number += 1
        freq = stackup.dielectric_frequency_ghz_or_none(layer, catalog) or 0.0
        dk, df = stackup.dielectric_dk_df_or_none(layer, catalog)
        thickness_mm = stackup.dielectric_thickness_mm(layer, catalog) or 0.0
        thickness = _xpedition_thickness_from_mm(thickness_mm)
        visible = _xpedition_dielectric_visible(stackup, index)
        prepreg = _xpedition_prepreg_flag(layer.dielectric_type)
        lines.append(
            (
                f'\t(LAYER NAME="DIELECTRIC_{dielectric_number}" DESCRIPTION="{_xpedition_dielectric_description(stackup, layer, catalog)}" '
                f"TYPE=DIELECTRIC COLOR=0 FILL=0 VISIBLE={visible} THICKNESS={thickness} "
                f"ER={_xpedition_number(dk or 0.0)} TG={_xpedition_number(df or 0.0)} ER_FREQ={_xpedition_frequency_hz(freq)} "
                f'THC=0.3 CONFORMAL=0 PREPREG={prepreg} MATERIAL="Dielectric" ATCHMETAL=0)'
            )
        )

    lines.extend(
        [
            (
                f'\t(LAYER NAME="SolderMaskBot" DESCRIPTION="Solder Mask" TYPE=DIELECTRIC COLOR=0 FILL=0 VISIBLE=0 '
                f"THICKNESS={_xpedition_thickness_from_mm(soldermask.thickness_mm)} ER={_xpedition_number(soldermask.dk)} "
                f"TG={_xpedition_number(soldermask.df)} ER_FREQ={_xpedition_frequency_hz(soldermask.freq_ghz)} "
                f'THC=0.3 CONFORMAL=1 PREPREG=0 MATERIAL="Dielectric" ATCHMETAL=0)'
            ),
            "}",
        ]
    )
    return "\n".join(lines) + "\n"


def import_stackup_xpedition(text: str, catalog: MaterialCatalog) -> Stackup:
    if "{STK_FILE}" not in text or "{STACKUP" not in text:
        raise ValueError("This file is not a valid Xpedition stackup export.")

    enable_roughness_match = re.search(r"\(ENABLE_ROUGHNESS=([01])\)", text)
    enable_roughness = enable_roughness_match is not None and enable_roughness_match.group(1) == "1"

    layer_fields = [
        _parse_xpedition_field_line(line)
        for line in text.splitlines()
        if "(LAYER " in line
    ]
    if len(layer_fields) < 3:
        raise ValueError("The Xpedition stackup file does not contain enough layers.")

    def parse_soldermask(fields: dict[str, str]) -> SolderMaskSettings:
        thickness_mm = _parse_xpedition_thickness_mm(fields.get("THICKNESS"))
        dk = _parse_optional_float(fields.get("ER"))
        df = _parse_optional_float(fields.get("TG"))
        freq_ghz = _parse_xpedition_frequency_ghz(fields.get("ER_FREQ"))
        return SolderMaskSettings(
            thickness_mm=thickness_mm if thickness_mm is not None else 0.025,
            dk=dk if dk is not None else 3.5,
            df=df if df is not None else 0.022,
            freq_ghz=freq_ghz if freq_ghz is not None else 1.0,
        )

    soldermask = parse_soldermask(layer_fields[0])

    layers = []
    for fields in layer_fields[1:-1]:
        layer_type = fields.get("TYPE", "")
        if layer_type == "SIGNAL":
            thickness_mm = _parse_xpedition_thickness_mm(fields.get("THICKNESS"))
            roughness_um = None
            if enable_roughness:
                rough_top = _parse_xpedition_roughness_um(fields.get("ROUGH_TOP"))
                rough_bot = _parse_xpedition_roughness_um(fields.get("ROUGH_BOT"))
                if rough_top not in (None, 0.0) and rough_bot not in (None, 0.0):
                    roughness_um = (rough_top + rough_bot) / 2.0
            copper_type = infer_copper_type(roughness_um)
            layers.append(
                CopperLayer(
                    thickness_mm=thickness_mm if thickness_mm is not None else 0.0,
                    copper_type=copper_type,
                    roughness_um=roughness_um,
                )
            )
            continue

        if layer_type != "DIELECTRIC":
            continue

        dielectric_type = "prepreg" if fields.get("PREPREG") == "1" else "core"
        description = fields.get("DESCRIPTION", "").strip()
        family, construction = _parse_xpedition_dielectric_description(description, dielectric_type=dielectric_type)
        thickness_mm = _parse_xpedition_thickness_mm(fields.get("THICKNESS"))
        dk = _parse_optional_float(fields.get("ER"))
        df = _parse_optional_float(fields.get("TG"))
        freq_ghz = _parse_xpedition_frequency_ghz(fields.get("ER_FREQ"))
        material_id = _match_xpedition_dielectric_entry(
            catalog,
            dielectric_type=dielectric_type,
            family=family,
            construction=construction,
            thickness_mm=thickness_mm,
            freq_ghz=freq_ghz,
            dk=dk,
            df=df,
        )
        manufacturer = catalog.get(material_id).manufacturer if material_id and catalog.get(material_id) else None
        layers.append(
            DielectricLayer(
                dielectric_type=dielectric_type,
                material_id=material_id,
                selected_freq_ghz=freq_ghz,
                description_override=description or None,
                manufacturer_override=manufacturer,
                family_override=family,
                construction_override=construction,
                thickness_mm_override=thickness_mm,
                dk_override=dk,
                df_override=df,
            )
        )

    return Stackup(layers=layers, soldermask=soldermask)
