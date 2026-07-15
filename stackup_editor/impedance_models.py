from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from stackup_editor.models import CopperLayer, Stackup
from stackup_editor.units import SUPPORTED_UNITS

SectionKind = Literal["single_ended", "differential"]
CopperUid: TypeAlias = str

_PROFILE_PREFIX = {
    "single_ended": "SE",
    "differential": "DIF",
}


def profile_name_prefix(section_kind: SectionKind) -> str:
    return _PROFILE_PREFIX[section_kind]


def format_profile_name(
    name: str,
    *,
    section_kind: SectionKind,
    target_ohm: float | None = None,
) -> str:
    """Build a normalized tab name like SE_50Ω or DIF_85Ω."""
    prefix = profile_name_prefix(section_kind)
    value_text: str | None = None

    if target_ohm is not None and target_ohm > 0:
        value_text = f"{target_ohm:g}"
    else:
        match = re.search(r"(\d+(?:\.\d+)?)", name)
        if match:
            try:
                value_text = f"{float(match.group(1)):g}"
            except ValueError:
                value_text = match.group(1)

    if value_text is None:
        stripped = name.strip()
        return stripped if stripped else f"{prefix}_?"

    return f"{prefix}_{value_text}Ω"


@dataclass
class ImpedanceLayerEntry:
    ref_above_uid: CopperUid | None = None
    ref_below_uid: CopperUid | None = None
    width_mm: float | None = None
    spacing_mm: float | None = None
    calculated_impedance_ohm: float | None = None


@dataclass
class ImpedanceProfile:
    name: str
    target_impedance_ohm: float | None = None
    layers: dict[CopperUid, ImpedanceLayerEntry] = field(default_factory=dict)


@dataclass
class ImpedanceSectionState:
    display_unit: str = "mil"
    profiles: list[ImpedanceProfile] = field(default_factory=list)
    active_profile_index: int = 0

    def active_profile(self) -> ImpedanceProfile:
        if not self.profiles:
            self.profiles.append(ImpedanceProfile(name="Default"))
        self.active_profile_index = max(0, min(self.active_profile_index, len(self.profiles) - 1))
        return self.profiles[self.active_profile_index]


@dataclass
class ImpedanceWorkspaceState:
    single_ended: ImpedanceSectionState = field(
        default_factory=lambda: ImpedanceSectionState(
            display_unit="mil",
            profiles=[ImpedanceProfile(name="SE_50Ω", target_impedance_ohm=50.0)],
        )
    )
    differential: ImpedanceSectionState = field(
        default_factory=lambda: ImpedanceSectionState(
            display_unit="mil",
            profiles=[ImpedanceProfile(name="DIF_85Ω", target_impedance_ohm=85.0)],
        )
    )


def copper_stackup_entries(stackup: Stackup) -> list[tuple[int, CopperLayer]]:
    return [(index, layer) for index, layer in enumerate(stackup.layers) if isinstance(layer, CopperLayer)]


def copper_stackup_indices(stackup: Stackup) -> list[int]:
    return [index for index, _layer in copper_stackup_entries(stackup)]


def copper_uid_map(stackup: Stackup) -> dict[CopperUid, tuple[int, CopperLayer]]:
    return {layer.uid: (index, layer) for index, layer in copper_stackup_entries(stackup)}


def copper_index_for_uid(stackup: Stackup, copper_uid: CopperUid | None) -> int | None:
    if not copper_uid:
        return None
    pair = copper_uid_map(stackup).get(copper_uid)
    return pair[0] if pair is not None else None


def copper_number_for_uid(stackup: Stackup, copper_uid: CopperUid | None) -> int | None:
    index = copper_index_for_uid(stackup, copper_uid)
    if index is None:
        return None
    return stackup.copper_layer_number(index)


def copper_uid_for_number(stackup: Stackup, copper_number: int | None) -> CopperUid | None:
    if copper_number is None or copper_number <= 0:
        return None
    for index, layer in copper_stackup_entries(stackup):
        if stackup.copper_layer_number(index) == copper_number:
            return layer.uid
    return None


def mirror_copper_uid(stackup: Stackup, copper_uid: CopperUid | None) -> CopperUid | None:
    index = copper_index_for_uid(stackup, copper_uid)
    if index is None:
        return None
    mirror_index = stackup.mirror_index(index)
    if mirror_index == index:
        return None
    mirror_layer = stackup.layers[mirror_index]
    if not isinstance(mirror_layer, CopperLayer):
        return None
    return mirror_layer.uid


def mirrored_reference_uids(
    stackup: Stackup,
    ref_above_uid: CopperUid | None,
    ref_below_uid: CopperUid | None,
) -> tuple[CopperUid | None, CopperUid | None]:
    # Mirroring flips top and bottom references across the symmetry axis.
    return mirror_copper_uid(stackup, ref_below_uid), mirror_copper_uid(stackup, ref_above_uid)


def sync_workspace_with_stackup(workspace: ImpedanceWorkspaceState, stackup: Stackup) -> None:
    valid_uids = {layer.uid for _index, layer in copper_stackup_entries(stackup)}
    for section in (workspace.single_ended, workspace.differential):
        for profile in section.profiles:
            for stale_uid in list(profile.layers):
                if stale_uid not in valid_uids:
                    del profile.layers[stale_uid]
            for _index, layer in copper_stackup_entries(stackup):
                profile.layers.setdefault(layer.uid, ImpedanceLayerEntry())
        if not section.profiles:
            section.profiles.append(ImpedanceProfile(name="Default"))
        section.active_profile_index = max(0, min(section.active_profile_index, len(section.profiles) - 1))


def migrate_legacy_copper_impedance(workspace: ImpedanceWorkspaceState, stackup: Stackup) -> None:
    """Copy per-layer impedance fields from copper layers into the default profiles once."""
    se_profile = workspace.single_ended.profiles[0]
    diff_profile = workspace.differential.profiles[0]
    for _index, layer in copper_stackup_entries(stackup):
        se_entry = se_profile.layers.setdefault(layer.uid, ImpedanceLayerEntry())
        diff_entry = diff_profile.layers.setdefault(layer.uid, ImpedanceLayerEntry())
        if layer.trace_width_mm is not None and se_entry.width_mm is None and diff_entry.width_mm is None:
            se_entry.width_mm = layer.trace_width_mm
            diff_entry.width_mm = layer.trace_width_mm
        if layer.trace_spacing_mm is not None and diff_entry.spacing_mm is None:
            diff_entry.spacing_mm = layer.trace_spacing_mm
        if layer.target_impedance_ohm is not None:
            if se_profile.target_impedance_ohm is None:
                se_profile.target_impedance_ohm = layer.target_impedance_ohm
            if diff_profile.target_impedance_ohm is None:
                diff_profile.target_impedance_ohm = layer.target_impedance_ohm


def deviation_percent(calculated_ohm: float | None, target_ohm: float | None) -> float | None:
    if calculated_ohm is None or target_ohm is None or target_ohm <= 0:
        return None
    return abs(calculated_ohm - target_ohm) / target_ohm * 100.0


def _entry_has_saved_data(entry: ImpedanceLayerEntry) -> bool:
    return any(
        value is not None
        for value in (
            entry.ref_above_uid,
            entry.ref_below_uid,
            entry.width_mm,
            entry.spacing_mm,
            entry.calculated_impedance_ohm,
        )
    )


def _serialize_entry(stackup: Stackup, copper_uid: CopperUid, entry: ImpedanceLayerEntry) -> dict[str, float | int | None]:
    return {
        "layer_number": copper_number_for_uid(stackup, copper_uid),
        "ref_above_layer_number": copper_number_for_uid(stackup, entry.ref_above_uid),
        "ref_below_layer_number": copper_number_for_uid(stackup, entry.ref_below_uid),
        "width_mm": entry.width_mm,
        "spacing_mm": entry.spacing_mm,
        "calculated_impedance_ohm": entry.calculated_impedance_ohm,
    }


def _serialize_section(
    workspace: ImpedanceWorkspaceState,
    stackup: Stackup,
) -> dict[str, object]:
    profiles: list[dict[str, object]] = []
    for profile in workspace.profiles:
        layers: list[dict[str, float | int | None]] = []
        for _index, layer in copper_stackup_entries(stackup):
            entry = profile.layers.get(layer.uid)
            if entry is None or not _entry_has_saved_data(entry):
                continue
            layers.append(_serialize_entry(stackup, layer.uid, entry))
        profiles.append(
            {
                "name": profile.name,
                "target_impedance_ohm": profile.target_impedance_ohm,
                "layers": layers,
            }
        )
    return {
        "display_unit": workspace.display_unit,
        "active_profile_index": workspace.active_profile_index,
        "profiles": profiles,
    }


def impedance_workspace_to_dict(workspace: ImpedanceWorkspaceState, stackup: Stackup) -> dict[str, object]:
    return {
        "version": 1,
        "single_ended": _serialize_section(workspace.single_ended, stackup),
        "differential": _serialize_section(workspace.differential, stackup),
    }


def _coerce_optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _deserialize_entry(stackup: Stackup, payload: object) -> tuple[CopperUid | None, ImpedanceLayerEntry | None]:
    if not isinstance(payload, dict):
        return None, None
    copper_uid = copper_uid_for_number(stackup, _coerce_optional_int(payload.get("layer_number")))
    if copper_uid is None:
        return None, None
    return copper_uid, ImpedanceLayerEntry(
        ref_above_uid=copper_uid_for_number(stackup, _coerce_optional_int(payload.get("ref_above_layer_number"))),
        ref_below_uid=copper_uid_for_number(stackup, _coerce_optional_int(payload.get("ref_below_layer_number"))),
        width_mm=_coerce_optional_float(payload.get("width_mm")),
        spacing_mm=_coerce_optional_float(payload.get("spacing_mm")),
        calculated_impedance_ohm=_coerce_optional_float(payload.get("calculated_impedance_ohm")),
    )


def _deserialize_section(
    payload: object,
    *,
    section_kind: SectionKind,
    stackup: Stackup,
    fallback: ImpedanceSectionState,
) -> ImpedanceSectionState:
    if not isinstance(payload, dict):
        return fallback

    display_unit = str(payload.get("display_unit") or fallback.display_unit)
    if display_unit not in SUPPORTED_UNITS:
        display_unit = fallback.display_unit

    profiles_payload = payload.get("profiles")
    profiles: list[ImpedanceProfile] = []
    if isinstance(profiles_payload, list):
        for profile_payload in profiles_payload:
            if not isinstance(profile_payload, dict):
                continue
            target_ohm = _coerce_optional_float(profile_payload.get("target_impedance_ohm"))
            name = str(profile_payload.get("name") or "").strip()
            if not name:
                name = format_profile_name("", section_kind=section_kind, target_ohm=target_ohm)
            profile = ImpedanceProfile(name=name, target_impedance_ohm=target_ohm)
            layers_payload = profile_payload.get("layers")
            if isinstance(layers_payload, list):
                for layer_payload in layers_payload:
                    copper_uid, entry = _deserialize_entry(stackup, layer_payload)
                    if copper_uid is None or entry is None:
                        continue
                    profile.layers[copper_uid] = entry
            profiles.append(profile)

    if not profiles:
        profiles = [ImpedanceProfile(name=profile.name, target_impedance_ohm=profile.target_impedance_ohm) for profile in fallback.profiles]

    active_profile_index = _coerce_optional_int(payload.get("active_profile_index"))
    if active_profile_index is None:
        active_profile_index = fallback.active_profile_index

    section = ImpedanceSectionState(
        display_unit=display_unit,
        profiles=profiles,
        active_profile_index=max(0, min(active_profile_index, len(profiles) - 1)),
    )
    return section


def impedance_workspace_from_dict(payload: object, stackup: Stackup) -> ImpedanceWorkspaceState:
    workspace = ImpedanceWorkspaceState()
    if not isinstance(payload, dict):
        sync_workspace_with_stackup(workspace, stackup)
        return workspace

    workspace.single_ended = _deserialize_section(
        payload.get("single_ended"),
        section_kind="single_ended",
        stackup=stackup,
        fallback=workspace.single_ended,
    )
    workspace.differential = _deserialize_section(
        payload.get("differential"),
        section_kind="differential",
        stackup=stackup,
        fallback=workspace.differential,
    )
    sync_workspace_with_stackup(workspace, stackup)
    return workspace
