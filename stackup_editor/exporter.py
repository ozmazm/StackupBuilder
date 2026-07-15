from __future__ import annotations

import json
import re

from stackup_editor.catalog import MaterialCatalog
from stackup_editor.impedance_models import (
    ImpedanceWorkspaceState,
    impedance_workspace_from_dict,
    impedance_workspace_to_dict,
)
from stackup_editor.models import (
    CopperLayer,
    DielectricLayer,
    SolderMaskSettings,
    Stackup,
    infer_copper_type,
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
                f"{index + 1}. {layer.dielectric_type.title()} dielectric",
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
    if dielectric_type == "prepreg":
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

    candidates = catalog.filter_entries(material_type=dielectric_type, family=family)
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
        material_type=material_type,
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


def _xpedition_dielectric_description(stackup: Stackup, layer: DielectricLayer, catalog: MaterialCatalog) -> str:
    if layer.description_override:
        return layer.description_override
    family = stackup.dielectric_family(layer, catalog) or ""
    construction = stackup.dielectric_construction(layer, catalog) or ""
    suffix = "PP" if layer.dielectric_type == "prepreg" else "Core"
    if family and construction:
        return f"{family}-{suffix}, {construction}"
    if family:
        return f"{family}-{suffix}"
    return ""


def _xpedition_dielectric_visible(stackup: Stackup, index: int) -> int:
    if index > 0 and isinstance(stackup.layers[index - 1], DielectricLayer):
        return 1
    return 0


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
        prepreg = 1 if layer.dielectric_type == "prepreg" else 0
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
