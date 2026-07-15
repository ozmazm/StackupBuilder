from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TypeAlias
from uuid import uuid4

from stackup_editor.catalog import MaterialCatalog, MaterialEntry
from stackup_editor.flex_catalog import CoverlayEntry, CoverlayMaterialCatalog, FlexCoreEntry, FlexCoreMaterialCatalog
from stackup_editor.units import from_display

COPPER_TYPES = ("RTF", "VLP", "HVLP", "STD")
FLEX_COPPER_TYPES = ("ED", "RA")
COPPER_ROUGHNESS_BY_TYPE_UM = {
    "RTF": 5.0,
    "VLP": 3.0,
    "HVLP": 2.0,
    "STD": 7.0,
    "ED": 7.0,
    "RA": 3.0,
}


def copper_roughness_um(copper_type: str) -> float:
    return COPPER_ROUGHNESS_BY_TYPE_UM.get(copper_type.upper(), 5.0)


def infer_copper_type(roughness_um: float | None, *, tolerance_um: float = 0.35) -> str:
    if roughness_um is None:
        return ""
    best_type = ""
    best_delta = None
    for copper_type, reference in COPPER_ROUGHNESS_BY_TYPE_UM.items():
        delta = abs(reference - roughness_um)
        if best_delta is None or delta < best_delta:
            best_type = copper_type
            best_delta = delta
    if best_delta is not None and best_delta <= tolerance_um:
        return best_type
    return ""


def new_copper_uid() -> str:
    return uuid4().hex


@dataclass
class CopperLayer:
    kind: str = "copper"
    uid: str = field(default_factory=new_copper_uid)
    thickness_mm: float = 0.035
    copper_type: str = "HVLP"
    roughness_um: float | None = None
    trace_width_mm: float | None = None
    trace_spacing_mm: float | None = None
    target_impedance_ohm: float | None = None

    def __post_init__(self) -> None:
        if not self.uid:
            self.uid = new_copper_uid()
        if self.roughness_um is None and self.copper_type:
            self.sync_roughness()

    @property
    def roughness_label(self) -> str:
        if self.roughness_um is None:
            return ""
        return f"Ra <= {self.roughness_um:.1f} um"

    def sync_roughness(self) -> None:
        if not self.copper_type:
            self.roughness_um = None
            return
        self.roughness_um = copper_roughness_um(self.copper_type)

    def set_copper_type(self, copper_type: str) -> None:
        self.copper_type = copper_type
        self.sync_roughness()

    def regenerate_uid(self) -> None:
        self.uid = new_copper_uid()

    def clear_impedance_inputs(self) -> None:
        self.trace_width_mm = None
        self.trace_spacing_mm = None
        self.target_impedance_ohm = None


@dataclass
class DielectricLayer:
    kind: str = "dielectric"
    dielectric_type: str = "prepreg"
    material_id: str = ""
    selected_freq_ghz: float | None = None
    description_override: str | None = None
    manufacturer_override: str | None = None
    family_override: str | None = None
    construction_override: str | None = None
    resin_content_pct_override: float | None = None
    thickness_mm_override: float | None = None
    dk_override: float | None = None
    df_override: float | None = None


@dataclass
class FlexCoreLayer:
    kind: str = "flex_core"
    material_id: str = ""
    selected_freq_ghz: float | None = None
    manufacturer: str = "Panasonic"
    family: str = "R-F777"
    variant_code: str = ""
    copper_type: str = "ED"
    copper_thickness_top_mm: float = 0.0
    copper_thickness_bottom_mm: float = 0.0
    symmetric_copper: bool = True
    dielectric_thickness_mm: float = 0.0
    construction: str = ""
    dk_by_freq_ghz: dict[float, float] = field(default_factory=dict)
    df_by_freq_ghz: dict[float, float] = field(default_factory=dict)
    reference_freq_ghz: float = 1.0
    reference_dk: float = 0.0
    reference_df: float = 0.0
    max_freq_ghz: float = 1.0
    source_pdf: str = ""
    notes: str = ""

    @property
    def display_name(self) -> str:
        bits = [self.family]
        if self.variant_code:
            bits.append(self.variant_code)
        return " ".join(bit for bit in bits if bit).strip()

    @property
    def sorted_frequencies(self) -> list[float]:
        return sorted(self.dk_by_freq_ghz)

    def closest_frequency(self, target_ghz: float | None = None) -> float:
        options = self.sorted_frequencies
        if not options:
            if target_ghz is None:
                return self.reference_freq_ghz
            return target_ghz
        if target_ghz is None:
            return options[-1]
        return min(options, key=lambda freq: (abs(freq - target_ghz), freq))

    def dk_at(self, freq_ghz: float) -> float:
        return self.dk_by_freq_ghz[freq_ghz]

    def df_at(self, freq_ghz: float) -> float:
        return self.df_by_freq_ghz[freq_ghz]

    @classmethod
    def from_entry(
        cls,
        entry: FlexCoreEntry,
        *,
        selected_freq_ghz: float | None = None,
    ) -> "FlexCoreLayer":
        chosen_freq = entry.closest_frequency(selected_freq_ghz)
        return cls(
            material_id=entry.id,
            selected_freq_ghz=chosen_freq,
            manufacturer=entry.manufacturer,
            family=entry.family,
            variant_code=entry.variant_code,
            copper_type=entry.copper_type,
            copper_thickness_top_mm=entry.copper_thickness_top_um / 1000.0,
            copper_thickness_bottom_mm=entry.copper_thickness_bottom_um / 1000.0,
            symmetric_copper=entry.symmetric_copper,
            dielectric_thickness_mm=entry.dielectric_thickness_mm,
            construction=entry.construction_label,
            dk_by_freq_ghz=dict(entry.dk_by_freq_ghz),
            df_by_freq_ghz=dict(entry.df_by_freq_ghz),
            reference_freq_ghz=entry.reference_freq_ghz,
            reference_dk=entry.reference_dk,
            reference_df=entry.reference_df,
            max_freq_ghz=entry.max_freq_ghz,
            source_pdf=entry.source_pdf,
            notes=entry.notes,
        )


@dataclass
class CoverlaySettings:
    manufacturer: str = "Arisawa"
    family: str = "C33"
    selected_freq_ghz: float | None = None
    pi_material_id: str = ""
    pi_thickness_mm: float = 0.0
    pi_dk_by_freq_ghz: dict[float, float] = field(default_factory=dict)
    pi_df_by_freq_ghz: dict[float, float] = field(default_factory=dict)
    adhesive_material_id: str = ""
    adhesive_thickness_mm: float = 0.0
    adhesive_dk_by_freq_ghz: dict[float, float] = field(default_factory=dict)
    adhesive_df_by_freq_ghz: dict[float, float] = field(default_factory=dict)
    source_pdf: str = ""

    @classmethod
    def from_entries(
        cls,
        pi_entry: CoverlayEntry,
        adhesive_entry: CoverlayEntry,
        *,
        selected_freq_ghz: float | None = None,
    ) -> "CoverlaySettings":
        chosen_freq = pi_entry.closest_frequency(selected_freq_ghz)
        return cls(
            manufacturer=pi_entry.manufacturer,
            family=pi_entry.family,
            selected_freq_ghz=chosen_freq,
            pi_material_id=pi_entry.id,
            pi_thickness_mm=pi_entry.thickness_mm,
            pi_dk_by_freq_ghz=dict(pi_entry.dk_by_freq_ghz),
            pi_df_by_freq_ghz=dict(pi_entry.df_by_freq_ghz),
            adhesive_material_id=adhesive_entry.id,
            adhesive_thickness_mm=adhesive_entry.thickness_mm,
            adhesive_dk_by_freq_ghz=dict(adhesive_entry.dk_by_freq_ghz),
            adhesive_df_by_freq_ghz=dict(adhesive_entry.df_by_freq_ghz),
            source_pdf=pi_entry.source_pdf,
        )

    def closest_frequency(self, target_ghz: float | None = None) -> float:
        options = sorted(set(self.pi_dk_by_freq_ghz) | set(self.adhesive_dk_by_freq_ghz))
        if not options:
            if target_ghz is None:
                return 1.0
            return target_ghz
        if target_ghz is None:
            return options[-1]
        return min(options, key=lambda freq: (abs(freq - target_ghz), freq))

    def component_label(self, component: str) -> str:
        return "CVL-PI" if component == "pi" else "CVL-Adh."

    def component_thickness_mm(self, component: str) -> float:
        return self.pi_thickness_mm if component == "pi" else self.adhesive_thickness_mm

    def component_dk_df(self, component: str, freq_ghz: float | None = None) -> tuple[float | None, float | None]:
        chosen_freq = self.closest_frequency(freq_ghz or self.selected_freq_ghz)
        if component == "pi":
            dk = self.pi_dk_by_freq_ghz.get(chosen_freq)
            df = self.pi_df_by_freq_ghz.get(chosen_freq)
            return dk, df
        dk = self.adhesive_dk_by_freq_ghz.get(chosen_freq)
        df = self.adhesive_df_by_freq_ghz.get(chosen_freq)
        return dk, df

    def component_frequency_ghz(self, component: str) -> float | None:
        chosen_freq = self.closest_frequency(self.selected_freq_ghz)
        dk, df = self.component_dk_df(component, chosen_freq)
        if dk is None and df is None:
            return None
        return chosen_freq

    def total_thickness_mm(self) -> float:
        return self.pi_thickness_mm + self.adhesive_thickness_mm


@dataclass
class SolderMaskSettings:
    thickness_mm: float = 0.025
    dk: float = 3.5
    df: float = 0.022
    freq_ghz: float = 1.0
    manufacturer: str = "TAIYO AMERICA"


DielectricLikeLayer: TypeAlias = DielectricLayer | FlexCoreLayer
Layer: TypeAlias = CopperLayer | DielectricLayer | FlexCoreLayer


def is_dielectric_like(layer: object) -> bool:
    return isinstance(layer, (DielectricLayer, FlexCoreLayer))


def clone_layer(layer: Layer) -> Layer:
    return deepcopy(layer)


@dataclass
class Stackup:
    layers: list[Layer] = field(default_factory=list)
    soldermask: SolderMaskSettings = field(default_factory=SolderMaskSettings)
    mode: str = "rigid"
    coverlay: CoverlaySettings | None = None
    flex_sandwich_slots: list[int] = field(default_factory=list)
    flex_slot_capacity: int = 0

    def copper_count(self) -> int:
        return sum(1 for layer in self.layers if isinstance(layer, CopperLayer))

    def flex_core_count(self) -> int:
        return sum(1 for layer in self.layers if isinstance(layer, FlexCoreLayer))

    def flex_sandwich_slot_ids(self) -> list[int]:
        expected = self.flex_core_count()
        if expected <= 0:
            return []
        if len(self.flex_sandwich_slots) == expected:
            return list(self.flex_sandwich_slots)
        return list(range(expected))

    def flex_slot_capacity_or_count(self) -> int:
        slot_ids = self.flex_sandwich_slot_ids()
        inferred = (max(slot_ids) + 1) if slot_ids else 0
        return max(self.flex_slot_capacity, inferred, self.flex_core_count())

    def flex_slot_for_layer_index(self, layer_index: int) -> int:
        slot_ids = self.flex_sandwich_slot_ids()
        sandwich_index = layer_index // 3
        if 0 <= sandwich_index < len(slot_ids):
            return slot_ids[sandwich_index]
        return sandwich_index

    def active_flex_slot_ids(self) -> set[int]:
        return set(self.flex_sandwich_slot_ids())

    def layer_thickness_mm(self, layer: Layer, catalog: MaterialCatalog) -> float:
        if isinstance(layer, CopperLayer):
            return layer.thickness_mm
        if isinstance(layer, FlexCoreLayer):
            return layer.dielectric_thickness_mm
        thickness_mm = self.dielectric_thickness_mm(layer, catalog)
        return thickness_mm or 0.0

    def total_thickness_mm(self, catalog: MaterialCatalog) -> float:
        surface_thickness = (
            self.coverlay.total_thickness_mm() * 2 * max(1, self.flex_core_count())
            if self.coverlay is not None
            else (self.soldermask.thickness_mm * 2)
        )
        return (
            sum(self.layer_thickness_mm(layer, catalog) for layer in self.layers)
            + surface_thickness
        )

    def dielectric_entry(self, layer: DielectricLikeLayer, catalog: MaterialCatalog) -> MaterialEntry | None:
        if isinstance(layer, FlexCoreLayer):
            return None
        if not layer.material_id:
            return None
        return catalog.get(layer.material_id)

    def dielectric_thickness_mm(self, layer: DielectricLikeLayer, catalog: MaterialCatalog) -> float | None:
        if isinstance(layer, FlexCoreLayer):
            return layer.dielectric_thickness_mm
        if layer.thickness_mm_override is not None:
            return layer.thickness_mm_override
        entry = self.dielectric_entry(layer, catalog)
        if entry is not None:
            return entry.thickness_mm
        return None

    def dielectric_manufacturer(self, layer: DielectricLikeLayer, catalog: MaterialCatalog) -> str | None:
        if isinstance(layer, FlexCoreLayer):
            return layer.manufacturer
        if layer.manufacturer_override:
            return layer.manufacturer_override
        entry = self.dielectric_entry(layer, catalog)
        return entry.manufacturer if entry is not None else None

    def dielectric_family(self, layer: DielectricLikeLayer, catalog: MaterialCatalog) -> str | None:
        if isinstance(layer, FlexCoreLayer):
            return layer.family
        if layer.family_override:
            return layer.family_override
        entry = self.dielectric_entry(layer, catalog)
        return entry.family if entry is not None else None

    def dielectric_construction(self, layer: DielectricLikeLayer, catalog: MaterialCatalog) -> str | None:
        if isinstance(layer, FlexCoreLayer):
            return layer.construction
        if layer.construction_override:
            return layer.construction_override
        entry = self.dielectric_entry(layer, catalog)
        return entry.construction if entry is not None else None

    def dielectric_resin_content_pct(self, layer: DielectricLikeLayer, catalog: MaterialCatalog) -> float | None:
        if isinstance(layer, FlexCoreLayer):
            return None
        if layer.resin_content_pct_override is not None:
            return layer.resin_content_pct_override
        entry = self.dielectric_entry(layer, catalog)
        return entry.resin_content_pct if entry is not None else None

    def dielectric_description(self, layer: DielectricLikeLayer, catalog: MaterialCatalog) -> str:
        if isinstance(layer, FlexCoreLayer):
            return layer.display_name
        if layer.description_override:
            return layer.description_override
        family = self.dielectric_family(layer, catalog)
        suffix = "PP" if layer.dielectric_type == "prepreg" else "Core"
        if family:
            return f"{family} - {suffix}"
        return ""

    def dielectric_frequency_ghz(self, layer: DielectricLikeLayer, catalog: MaterialCatalog) -> float:
        if isinstance(layer, FlexCoreLayer):
            if layer.selected_freq_ghz is None:
                raise ValueError("No dielectric frequency is available for this flex-core layer.")
            return layer.closest_frequency(layer.selected_freq_ghz)
        entry = self.dielectric_entry(layer, catalog)
        if layer.dk_override is not None or layer.df_override is not None or entry is None:
            if layer.selected_freq_ghz is None:
                raise ValueError("No dielectric frequency is available for this layer.")
            return layer.selected_freq_ghz
        return entry.closest_frequency(layer.selected_freq_ghz)

    def dielectric_frequency_ghz_or_none(self, layer: DielectricLikeLayer, catalog: MaterialCatalog) -> float | None:
        try:
            return self.dielectric_frequency_ghz(layer, catalog)
        except ValueError:
            return None

    def dielectric_dk_df(self, layer: DielectricLikeLayer, catalog: MaterialCatalog) -> tuple[float, float]:
        if isinstance(layer, FlexCoreLayer):
            freq = self.dielectric_frequency_ghz(layer, catalog)
            return layer.dk_at(freq), layer.df_at(freq)
        if layer.dk_override is not None and layer.df_override is not None:
            return layer.dk_override, layer.df_override
        entry = self.dielectric_entry(layer, catalog)
        if entry is None:
            raise ValueError("No dielectric Dk/Df values are available for this layer.")
        freq = self.dielectric_frequency_ghz(layer, catalog)
        return entry.dk_at(freq), entry.df_at(freq)

    def dielectric_dk_df_or_none(self, layer: DielectricLikeLayer, catalog: MaterialCatalog) -> tuple[float | None, float | None]:
        try:
            return self.dielectric_dk_df(layer, catalog)
        except ValueError:
            return None, None

    def set_dielectric_frequency(self, index: int, target_freq_ghz: float, catalog: MaterialCatalog) -> float:
        layer = self.layers[index]
        if not is_dielectric_like(layer):
            raise ValueError("The selected layer is not dielectric.")
        if isinstance(layer, FlexCoreLayer):
            layer.selected_freq_ghz = layer.closest_frequency(target_freq_ghz)
            return layer.selected_freq_ghz
        entry = self.dielectric_entry(layer, catalog)
        if entry is None or layer.dk_override is not None or layer.df_override is not None:
            layer.selected_freq_ghz = target_freq_ghz
        else:
            layer.selected_freq_ghz = entry.closest_frequency(target_freq_ghz)
        return layer.selected_freq_ghz

    def apply_frequency_to_all_dielectrics(self, target_freq_ghz: float, catalog: MaterialCatalog) -> list[tuple[int, float]]:
        applied = []
        for index, layer in enumerate(self.layers):
            if not is_dielectric_like(layer):
                continue
            applied_freq = self.set_dielectric_frequency(index, target_freq_ghz, catalog)
            applied.append((index, applied_freq))
        return applied

    def consecutive_core_pair(
        self,
        replacements: dict[int, Layer] | None = None,
        removed_indices: set[int] | None = None,
    ) -> tuple[int, int] | None:
        """Return the first adjacent core pair in dielectric order, ignoring copper rows."""
        if self.mode != "rigid":
            return None
        previous_dielectric_index: int | None = None
        previous_was_core = False
        replacement_layers = replacements or {}
        removed = removed_indices or set()
        for index, current_layer in enumerate(self.layers):
            if index in removed:
                continue
            layer = replacement_layers.get(index, current_layer)
            if not is_dielectric_like(layer):
                continue
            is_core = isinstance(layer, FlexCoreLayer) or (
                isinstance(layer, DielectricLayer) and layer.dielectric_type == "core"
            )
            if previous_was_core and is_core and previous_dielectric_index is not None:
                return previous_dielectric_index, index
            previous_dielectric_index = index
            previous_was_core = is_core
        return None

    def symmetry_report(self, catalog: MaterialCatalog) -> tuple[bool, list[str]]:
        issues: list[str] = []
        core_pair = self.consecutive_core_pair()
        if core_pair is not None:
            issues.append("Core materials must be separated by prepreg.")
        total = len(self.layers)
        for index in range(total // 2):
            top = self.layers[index]
            bottom = self.layers[-1 - index]

            if type(top) is not type(bottom):
                issues.append(f"Layer pair {index + 1}/{total - index} are different types.")
                continue

            if isinstance(top, CopperLayer) and isinstance(bottom, CopperLayer):
                if abs(top.thickness_mm - bottom.thickness_mm) > 1e-6:
                    issues.append(f"{self.copper_layer_number(index)} and {self.copper_layer_number(total - 1 - index)} copper thicknesses differ.")
                if top.copper_type != bottom.copper_type:
                    issues.append(f"{self.copper_layer_number(index)} and {self.copper_layer_number(total - 1 - index)} copper types differ.")
                continue

            if not is_dielectric_like(top) or not is_dielectric_like(bottom):
                continue

            top_name = "Flex Core" if isinstance(top, FlexCoreLayer) else f"Dielectric {self.dielectric_layer_number(index)}"
            bottom_name = "Flex Core" if isinstance(bottom, FlexCoreLayer) else f"Dielectric {self.dielectric_layer_number(total - 1 - index)}"

            if isinstance(top, DielectricLayer) and isinstance(bottom, DielectricLayer):
                if top.dielectric_type != bottom.dielectric_type:
                    issues.append(f"{top_name} and {bottom_name} use different dielectric types.")
            elif type(top) is not type(bottom):
                issues.append(f"{top_name} and {bottom_name} use different dielectric layer kinds.")
            if (top.material_id or "") != (bottom.material_id or ""):
                issues.append(f"{top_name} and {bottom_name} use different material entries.")
            top_description = self.dielectric_description(top, catalog)
            bottom_description = self.dielectric_description(bottom, catalog)
            if top_description != bottom_description:
                issues.append(f"{top_name} and {bottom_name} use different material descriptions.")
            top_thickness = self.dielectric_thickness_mm(top, catalog)
            bottom_thickness = self.dielectric_thickness_mm(bottom, catalog)
            if top_thickness is not None and bottom_thickness is not None and abs(top_thickness - bottom_thickness) > 1e-6:
                issues.append(f"{top_name} and {bottom_name} have different thicknesses.")
            top_freq = self.dielectric_frequency_ghz_or_none(top, catalog)
            bottom_freq = self.dielectric_frequency_ghz_or_none(bottom, catalog)
            if top_freq is not None and bottom_freq is not None and top_freq != bottom_freq:
                issues.append(f"{top_name} and {bottom_name} use different frequencies.")
            top_dk, top_df = self.dielectric_dk_df_or_none(top, catalog)
            bottom_dk, bottom_df = self.dielectric_dk_df_or_none(bottom, catalog)
            if top_dk is not None and bottom_dk is not None and abs(top_dk - bottom_dk) > 1e-9:
                issues.append(f"{top_name} and {bottom_name} use different Dk values.")
            if top_df is not None and bottom_df is not None and abs(top_df - bottom_df) > 1e-9:
                issues.append(f"{top_name} and {bottom_name} use different Df values.")

        if self.coverlay is not None:
            freq = self.coverlay.selected_freq_ghz
            top_pi_dk, top_pi_df = self.coverlay.component_dk_df("pi", freq)
            top_adh_dk, top_adh_df = self.coverlay.component_dk_df("adhesive", freq)
            if top_pi_dk is None or top_pi_df is None or top_adh_dk is None or top_adh_df is None:
                issues.append("Coverlay settings are incomplete.")

        return not issues, issues

    def copper_layer_number(self, index: int) -> int:
        if not isinstance(self.layers[index], CopperLayer):
            raise ValueError("The selected layer is not copper.")
        return sum(1 for layer in self.layers[: index + 1] if isinstance(layer, CopperLayer))

    def dielectric_layer_number(self, index: int) -> int:
        if not is_dielectric_like(self.layers[index]):
            raise ValueError("The selected layer is not dielectric.")
        return sum(1 for layer in self.layers[: index + 1] if is_dielectric_like(layer))

    def mirror_index(self, index: int) -> int:
        return len(self.layers) - 1 - index

    def insert_copper_above(
        self,
        index: int,
        *,
        copper: CopperLayer | None = None,
        dielectric: DielectricLayer | None = None,
    ) -> None:
        if not isinstance(self.layers[index], CopperLayer):
            raise ValueError("A new copper layer can only be inserted relative to an existing copper layer.")
        copper_layer = replace(copper or CopperLayer())
        copper_layer.regenerate_uid()
        copper_layer.sync_roughness()
        copper_layer.clear_impedance_inputs()
        dielectric_layer = replace(dielectric or DielectricLayer())
        self.layers[index:index] = [copper_layer, dielectric_layer]

    def insert_copper_below(
        self,
        index: int,
        *,
        copper: CopperLayer | None = None,
        dielectric: DielectricLayer | None = None,
    ) -> None:
        if not isinstance(self.layers[index], CopperLayer):
            raise ValueError("A new copper layer can only be inserted relative to an existing copper layer.")
        copper_layer = replace(copper or CopperLayer())
        copper_layer.regenerate_uid()
        copper_layer.sync_roughness()
        copper_layer.clear_impedance_inputs()
        dielectric_layer = replace(dielectric or DielectricLayer())
        self.layers[index + 1 : index + 1] = [dielectric_layer, copper_layer]

    def can_remove_copper(self, index: int) -> bool:
        return isinstance(self.layers[index], CopperLayer) and self.copper_count() > 2

    def _default_insert_items_for_boundary(
        self,
        boundary: int,
        *,
        copper: CopperLayer | None = None,
        dielectric: DielectricLayer | None = None,
    ) -> list[Layer]:
        copper_layer = replace(copper or self._template_copper_layer())
        copper_layer.regenerate_uid()
        copper_layer.sync_roughness()
        copper_layer.clear_impedance_inputs()
        dielectric_layer = replace(dielectric or self._template_dielectric_layer())
        if boundary % 2 == 0:
            return [copper_layer, dielectric_layer]
        return [dielectric_layer, copper_layer]

    def _template_copper_layer(self) -> CopperLayer:
        for layer in self.layers:
            if isinstance(layer, CopperLayer):
                return layer
        return CopperLayer()

    def _template_dielectric_layer(self) -> DielectricLayer:
        for layer in self.layers:
            if isinstance(layer, DielectricLayer) and layer.material_id:
                return layer
        return DielectricLayer()

    def add_symmetric_layers(
        self,
        boundary: int,
        *,
        copper: CopperLayer | None = None,
        dielectric: DielectricLayer | None = None,
    ) -> tuple[int, int]:
        if boundary < 0 or boundary > len(self.layers):
            raise IndexError("Boundary is out of range.")

        mirror_boundary = len(self.layers) - boundary
        top_boundary, bottom_boundary = sorted((boundary, mirror_boundary))

        bottom_items = self._default_insert_items_for_boundary(
            bottom_boundary,
            copper=replace(copper or self._template_copper_layer()),
            dielectric=replace(dielectric or self._template_dielectric_layer()),
        )
        top_items = self._default_insert_items_for_boundary(
            top_boundary,
            copper=replace(copper or self._template_copper_layer()),
            dielectric=replace(dielectric or self._template_dielectric_layer()),
        )

        self.layers[bottom_boundary:bottom_boundary] = bottom_items
        self.layers[top_boundary:top_boundary] = top_items

        top_copper_index = next(
            index for index in range(top_boundary, top_boundary + len(top_items)) if isinstance(self.layers[index], CopperLayer)
        )
        bottom_copper_index = next(
            index
            for index in range(bottom_boundary + len(top_items), bottom_boundary + len(top_items) + len(bottom_items))
            if isinstance(self.layers[index], CopperLayer)
        )
        return top_copper_index, bottom_copper_index

    def add_symmetric_dielectrics(
        self,
        boundary: int,
        *,
        dielectric: DielectricLayer | None = None,
    ) -> tuple[int, int]:
        if boundary <= 0 or boundary >= len(self.layers):
            raise IndexError("Material insertion must stay between existing layers.")

        mirror_boundary = len(self.layers) - boundary
        top_boundary, bottom_boundary = sorted((boundary, mirror_boundary))

        self.layers[bottom_boundary:bottom_boundary] = [replace(dielectric or self._template_dielectric_layer())]
        self.layers[top_boundary:top_boundary] = [replace(dielectric or self._template_dielectric_layer())]
        return top_boundary, bottom_boundary + 1

    def apply_symmetric_dielectric(
        self,
        index: int,
        *,
        dielectric: DielectricLayer | None = None,
    ) -> int:
        source = replace(dielectric or self.layers[index])
        if not isinstance(source, DielectricLayer):
            raise ValueError("The selected layer is not dielectric.")

        mirror = self.mirror_index(index)
        if not isinstance(self.layers[index], DielectricLayer) or not isinstance(self.layers[mirror], DielectricLayer):
            raise ValueError("The selected dielectric layer does not have a dielectric symmetry pair.")

        self.layers[index] = replace(source)
        if mirror != index:
            self.layers[mirror] = replace(source)
        return mirror

    def apply_symmetric_copper(
        self,
        index: int,
        *,
        copper: CopperLayer | None = None,
    ) -> int:
        source = replace(copper or self.layers[index])
        if not isinstance(source, CopperLayer):
            raise ValueError("The selected layer is not copper.")
        source.sync_roughness()

        mirror = self.mirror_index(index)
        if not isinstance(self.layers[index], CopperLayer) or not isinstance(self.layers[mirror], CopperLayer):
            raise ValueError("The selected copper layer does not have a copper symmetry pair.")
        if mirror == index:
            raise ValueError("A copper layer cannot be its own symmetry pair.")

        top_existing = self.layers[index]
        bottom_existing = self.layers[mirror]
        if not isinstance(top_existing, CopperLayer) or not isinstance(bottom_existing, CopperLayer):
            raise ValueError("The selected copper layer does not have a copper symmetry pair.")

        self.layers[index] = replace(
            top_existing,
            thickness_mm=source.thickness_mm,
            copper_type=source.copper_type,
            roughness_um=source.roughness_um,
        )
        self.layers[mirror] = replace(
            bottom_existing,
            thickness_mm=source.thickness_mm,
            copper_type=source.copper_type,
            roughness_um=source.roughness_um,
        )
        return mirror

    def remove_symmetric_copper_pair(self, index: int) -> tuple[int, int]:
        if not self.can_remove_copper(index):
            raise ValueError("At least two copper layers must remain in the stackup.")

        mirror = self.mirror_index(index)
        if not isinstance(self.layers[index], CopperLayer) or not isinstance(self.layers[mirror], CopperLayer):
            raise ValueError("Selected layer must be copper and have a copper mirror.")

        if index == mirror:
            raise ValueError("A copper layer cannot be its own symmetry pair.")

        if index < mirror:
            top_slice = slice(index, index + 2) if index % 2 == 0 else slice(index - 1, index + 1)
            bottom_slice = slice(mirror - 1, mirror + 1) if mirror % 2 == 0 else slice(mirror, mirror + 2)
        else:
            top_slice = slice(mirror, mirror + 2) if mirror % 2 == 0 else slice(mirror - 1, mirror + 1)
            bottom_slice = slice(index - 1, index + 1) if index % 2 == 0 else slice(index, index + 2)

        top_start, top_stop = top_slice.start, top_slice.stop
        bottom_start, bottom_stop = bottom_slice.start, bottom_slice.stop
        if top_stop - top_start != 2 or bottom_stop - bottom_start != 2:
            raise ValueError("Symmetric copper removal requires exactly one copper-dielectric pair per side.")

        del self.layers[bottom_start:bottom_stop]
        del self.layers[top_start:top_stop]
        return top_start, max(top_start, bottom_start - 2)

    def _dielectric_span_bounds(self, index: int) -> tuple[int, int]:
        if not isinstance(self.layers[index], DielectricLayer):
            raise ValueError("The selected layer is not dielectric.")

        left = index - 1
        while left >= 0 and not isinstance(self.layers[left], CopperLayer):
            left -= 1

        right = index + 1
        while right < len(self.layers) and not isinstance(self.layers[right], CopperLayer):
            right += 1

        if left < 0 or right >= len(self.layers):
            raise ValueError("Dielectric materials must stay between copper layers.")
        return left, right

    def _dielectric_span_count(self, index: int) -> int:
        left, right = self._dielectric_span_bounds(index)
        return right - left - 1

    def can_remove_symmetric_dielectric(self, index: int) -> tuple[bool, str | None]:
        if not isinstance(self.layers[index], DielectricLayer):
            return False, "Select a dielectric material row to remove materials."

        mirror = self.mirror_index(index)
        if not isinstance(self.layers[mirror], DielectricLayer):
            return False, "The selected dielectric layer does not have a dielectric symmetry pair."

        span_left, span_right = self._dielectric_span_bounds(index)
        span_count = span_right - span_left - 1

        if mirror == index:
            if span_count <= 1:
                return False, "At least one dielectric material must remain between neighboring copper layers."
            return True, None

        mirror_left, mirror_right = self._dielectric_span_bounds(mirror)
        mirror_count = mirror_right - mirror_left - 1

        if (span_left, span_right) == (mirror_left, mirror_right):
            if span_count <= 2:
                return False, "At least one dielectric material must remain between neighboring copper layers."
            return True, None

        if span_count <= 1 or mirror_count <= 1:
            return False, "At least one dielectric material must remain between neighboring copper layers."
        return True, None

    def remove_symmetric_dielectric_pair(self, index: int) -> tuple[int, int]:
        allowed, reason = self.can_remove_symmetric_dielectric(index)
        if not allowed:
            raise ValueError(reason or "The selected dielectric material cannot be removed.")

        mirror = self.mirror_index(index)
        if mirror == index:
            del self.layers[index]
            return max(0, index - 1), max(0, index - 1)

        top_index, bottom_index = sorted((index, mirror))
        del self.layers[bottom_index]
        del self.layers[top_index]
        return top_index, max(top_index, bottom_index - 1)

    def remove_copper(self, index: int) -> None:
        if not self.can_remove_copper(index):
            raise ValueError("At least two copper layers must remain in the stackup.")
        if index == 0:
            del self.layers[0:2]
            return
        if index == len(self.layers) - 1:
            del self.layers[-2:]
            return
        del self.layers[index : index + 2]


def build_default_stackup(catalog: MaterialCatalog) -> Stackup:
    preferred = "FR408HR" if any(entry.family == "FR408HR" for entry in catalog.entries) else None
    core = catalog.first_for("core", preferred_family=preferred)
    prepreg = catalog.first_for("prepreg", preferred_family=preferred)
    one_oz_mm = from_display(1.0, "oz")
    return Stackup(
        layers=[
            CopperLayer(thickness_mm=one_oz_mm, copper_type="RTF"),
            DielectricLayer(dielectric_type="prepreg", material_id=prepreg.id, selected_freq_ghz=prepreg.max_freq_ghz),
            CopperLayer(thickness_mm=one_oz_mm, copper_type="HVLP"),
            DielectricLayer(dielectric_type="core", material_id=core.id, selected_freq_ghz=core.max_freq_ghz),
            CopperLayer(thickness_mm=one_oz_mm, copper_type="HVLP"),
            DielectricLayer(dielectric_type="prepreg", material_id=prepreg.id, selected_freq_ghz=prepreg.max_freq_ghz),
            CopperLayer(thickness_mm=one_oz_mm, copper_type="RTF"),
        ]
    )


def _preferred_material_entry(
    catalog: MaterialCatalog,
    *,
    material_type: str,
    family: str,
    construction: str,
    thickness_mm: float,
    classification: str | None = None,
) -> MaterialEntry:
    normalized_construction = construction.replace(" ", "").lower()
    normalized_classification = classification.lower() if classification is not None else None
    candidates = catalog.filter_entries(material_type=material_type, family=family)
    exact = [
        entry
        for entry in candidates
        if entry.construction.replace(" ", "").lower() == normalized_construction
        and abs(entry.thickness_mm - thickness_mm) <= 1e-6
        and (
            normalized_classification is None
            or (entry.classification or "").lower() == normalized_classification
        )
    ]
    if exact:
        return exact[0]
    fallback = [
        entry
        for entry in candidates
        if entry.construction.replace(" ", "").lower() == normalized_construction
        and (
            normalized_classification is None
            or (entry.classification or "").lower() == normalized_classification
        )
    ]
    if fallback:
        return min(fallback, key=lambda entry: abs(entry.thickness_mm - thickness_mm))
    return catalog.first_for(material_type, preferred_family=family)


def preferred_default_flex_core_entry(
    flex_core_catalog: FlexCoreMaterialCatalog,
) -> FlexCoreEntry:
    preferred_entry = next(
        (
            entry
            for entry in flex_core_catalog.entries
            if entry.symmetric_copper
            and abs(entry.copper_thickness_top_um - 18.0) < 1e-6
            and abs(entry.dielectric_thickness_um - 25.0) < 1e-6
            and entry.copper_type == "ED"
        ),
        None,
    )
    return preferred_entry or flex_core_catalog.entries[0]


def build_default_flex_stackup(
    flex_core_catalog: FlexCoreMaterialCatalog,
    coverlay_catalog: CoverlayMaterialCatalog,
    *,
    flex_entry: FlexCoreEntry | None = None,
) -> Stackup:
    flex_entry = flex_entry or preferred_default_flex_core_entry(flex_core_catalog)
    flex_layer = FlexCoreLayer.from_entry(flex_entry, selected_freq_ghz=flex_entry.max_freq_ghz)
    coverlay = CoverlaySettings.from_entries(
        coverlay_catalog.pi_component(),
        coverlay_catalog.adhesive_component(),
        selected_freq_ghz=coverlay_catalog.pi_component().closest_frequency(None),
    )
    return Stackup(
        mode="flex",
        coverlay=coverlay,
        flex_sandwich_slots=[0],
        flex_slot_capacity=1,
        layers=[
            CopperLayer(
                thickness_mm=flex_layer.copper_thickness_top_mm,
                copper_type=flex_layer.copper_type,
            ),
            flex_layer,
            CopperLayer(
                thickness_mm=flex_layer.copper_thickness_bottom_mm,
                copper_type=flex_layer.copper_type,
            ),
        ],
    )


def build_flex_stackup_from_templates(
    *,
    sandwich_count: int | None = None,
    flex_core_template: FlexCoreLayer,
    coverlay: CoverlaySettings,
    slot_indices: list[int] | None = None,
    slot_capacity: int | None = None,
) -> Stackup:
    if slot_indices is None:
        if sandwich_count is None:
            raise ValueError("A flex zone build needs either sandwich_count or slot_indices.")
        slot_indices = list(range(sandwich_count))
    slot_indices = sorted(slot_indices)
    if not slot_indices:
        raise ValueError("A flex zone must contain at least one flex sandwich.")
    if slot_capacity is None:
        slot_capacity = max(slot_indices) + 1
    if slot_capacity < max(slot_indices) + 1:
        raise ValueError("Flex slot capacity cannot be smaller than the highest active slot index.")

    layers: list[Layer] = []
    for _slot_index in slot_indices:
        top_copper = CopperLayer(
            thickness_mm=flex_core_template.copper_thickness_top_mm,
            copper_type=flex_core_template.copper_type,
        )
        top_copper.sync_roughness()
        bottom_copper = CopperLayer(
            thickness_mm=flex_core_template.copper_thickness_bottom_mm,
            copper_type=flex_core_template.copper_type,
        )
        bottom_copper.sync_roughness()
        flex_core = deepcopy(flex_core_template)
        layers.extend([top_copper, flex_core, bottom_copper])

    return Stackup(
        mode="flex",
        coverlay=deepcopy(coverlay),
        flex_sandwich_slots=list(slot_indices),
        flex_slot_capacity=slot_capacity,
        layers=layers,
    )


def rigid_shared_region_bounds_for_capacity(
    rigid_stackup: Stackup,
    slot_capacity: int,
) -> tuple[int, int]:
    rigid_copper_count = rigid_stackup.copper_count()
    flex_copper_capacity = slot_capacity * 2
    if rigid_copper_count <= 0 or flex_copper_capacity <= 0:
        raise ValueError("Rigid and flex stackups must both contain copper layers.")
    if flex_copper_capacity > rigid_copper_count:
        raise ValueError("Flex slot capacity cannot exceed the rigid copper count.")
    if (rigid_copper_count - flex_copper_capacity) % 2 != 0:
        raise ValueError("Flex copper count must leave a symmetric rigid copper count on both sides.")

    outer_rigid_copper_per_side = (rigid_copper_count - flex_copper_capacity) // 2
    copper_indices = [index for index, layer in enumerate(rigid_stackup.layers) if isinstance(layer, CopperLayer)]
    start = copper_indices[outer_rigid_copper_per_side]
    end = copper_indices[outer_rigid_copper_per_side + flex_copper_capacity - 1]
    if start < 0 or end >= len(rigid_stackup.layers):
        raise ValueError("Computed rigid shared-region bounds are out of range.")
    return start, end


def rigid_shared_region_bounds(
    rigid_stackup: Stackup,
    flex_stackup: Stackup,
) -> tuple[int, int]:
    slot_capacity = flex_stackup.flex_slot_capacity_or_count()
    if slot_capacity <= 0:
        raise ValueError("Flex stackup must contain at least one flex-core sandwich.")
    return rigid_shared_region_bounds_for_capacity(rigid_stackup, slot_capacity)


def rigid_slot_copper_indices(
    rigid_stackup: Stackup,
    slot_capacity: int,
    slot_id: int,
) -> tuple[int, int]:
    """Return the rigid copper-row indices occupied by one reserved flex slot."""
    if not 0 <= slot_id < slot_capacity:
        raise ValueError("Flex slot is outside the reserved slot capacity.")
    rigid_copper_count = rigid_stackup.copper_count()
    flex_copper_capacity = slot_capacity * 2
    if flex_copper_capacity > rigid_copper_count:
        raise ValueError("Flex slot capacity cannot exceed the rigid copper count.")
    if (rigid_copper_count - flex_copper_capacity) % 2 != 0:
        raise ValueError("Flex copper count must leave a symmetric rigid copper count on both sides.")
    outer_rigid_copper_per_side = (rigid_copper_count - flex_copper_capacity) // 2
    copper_indices = [
        index for index, layer in enumerate(rigid_stackup.layers) if isinstance(layer, CopperLayer)
    ]
    copper_offset = outer_rigid_copper_per_side + (slot_id * 2)
    return copper_indices[copper_offset], copper_indices[copper_offset + 1]


def rebuild_rigid_stackup_from_slot_activity(
    rigid_stackup: Stackup,
    *,
    slot_capacity: int,
    active_slot_ids: set[int],
    slot_templates: dict[int, FlexCoreLayer],
    rigid_core_template: DielectricLayer,
    bridge_dielectric_template: DielectricLayer | None = None,
    outer_boundary_dielectric_template: DielectricLayer | None = None,
) -> Stackup:
    start, end = rigid_shared_region_bounds_for_capacity(rigid_stackup, slot_capacity)
    top_prefix = [clone_layer(layer) for layer in rigid_stackup.layers[:start]]
    bottom_suffix = [clone_layer(layer) for layer in rigid_stackup.layers[end + 1 :]]
    bridge_template = deepcopy(bridge_dielectric_template) if bridge_dielectric_template is not None else None
    if bridge_template is not None and bridge_template.dielectric_type != "prepreg":
        raise ValueError("Inter-sandwich rigid material must be prepreg.")
    outer_boundary_template = (
        deepcopy(outer_boundary_dielectric_template)
        if outer_boundary_dielectric_template is not None
        else (deepcopy(bridge_dielectric_template) if bridge_dielectric_template is not None else None)
    )

    slot_copper_pairs = [
        rigid_slot_copper_indices(rigid_stackup, slot_capacity, slot_id)
        for slot_id in range(slot_capacity)
    ]
    preserved_bridges = [
        [
            clone_layer(layer)
            for layer in rigid_stackup.layers[bottom_index + 1 : next_top_index]
            if isinstance(layer, DielectricLayer)
        ]
        for (_top_index, bottom_index), (next_top_index, _next_bottom_index) in zip(
            slot_copper_pairs,
            slot_copper_pairs[1:],
        )
    ]

    def normalized_bridge(layers: list[Layer]) -> list[Layer]:
        if bridge_template is None:
            raise ValueError("An inter-sandwich rigid prepreg template is required for multiple flex sandwiches.")
        normalized: list[Layer] = []
        for layer in layers:
            if not isinstance(layer, DielectricLayer):
                continue
            layer_is_core = layer.dielectric_type == "core"
            previous_is_core = bool(
                normalized
                and isinstance(normalized[-1], DielectricLayer)
                and normalized[-1].dielectric_type == "core"
            )
            if (not normalized and layer_is_core) or (previous_is_core and layer_is_core):
                normalized.append(deepcopy(bridge_template))
            normalized.append(clone_layer(layer))
        if not normalized or (
            isinstance(normalized[-1], DielectricLayer)
            and normalized[-1].dielectric_type == "core"
        ):
            normalized.append(deepcopy(bridge_template))
        return normalized

    fallback_flex_template = next(iter(slot_templates.values()), None)
    rebuilt_shared_layers: list[Layer] = []
    for slot_id in range(slot_capacity):
        top_index, bottom_index = slot_copper_pairs[slot_id]
        top_existing = rigid_stackup.layers[top_index]
        bottom_existing = rigid_stackup.layers[bottom_index]

        slot_template = slot_templates.get(slot_id) or fallback_flex_template
        active_flex_template = slot_template if slot_id in active_slot_ids else None
        if isinstance(top_existing, CopperLayer) and active_flex_template is not None:
            top_copper = replace(
                top_existing,
                thickness_mm=active_flex_template.copper_thickness_top_mm,
                copper_type=active_flex_template.copper_type,
            )
            top_copper.sync_roughness()
        elif isinstance(top_existing, CopperLayer):
            top_copper = clone_layer(top_existing)
        elif slot_template is not None:
            top_copper = CopperLayer(
                thickness_mm=slot_template.copper_thickness_top_mm,
                copper_type=slot_template.copper_type,
            )
            top_copper.sync_roughness()
        else:
            top_copper = CopperLayer()

        if isinstance(bottom_existing, CopperLayer) and active_flex_template is not None:
            bottom_copper = replace(
                bottom_existing,
                thickness_mm=active_flex_template.copper_thickness_bottom_mm,
                copper_type=active_flex_template.copper_type,
            )
            bottom_copper.sync_roughness()
        elif isinstance(bottom_existing, CopperLayer):
            bottom_copper = clone_layer(bottom_existing)
        elif slot_template is not None:
            bottom_copper = CopperLayer(
                thickness_mm=slot_template.copper_thickness_bottom_mm,
                copper_type=slot_template.copper_type,
            )
            bottom_copper.sync_roughness()
        else:
            bottom_copper = CopperLayer()

        dielectric = (
            deepcopy(slot_templates[slot_id])
            if slot_id in active_slot_ids and slot_id in slot_templates
            else deepcopy(rigid_core_template)
        )
        rebuilt_shared_layers.extend([top_copper, dielectric, bottom_copper])
        if slot_id < slot_capacity - 1:
            rebuilt_shared_layers.extend(normalized_bridge(preserved_bridges[slot_id]))

    if outer_boundary_template is not None:
        if top_prefix and isinstance(top_prefix[-1], DielectricLayer):
            top_prefix[-1] = deepcopy(outer_boundary_template)
        if bottom_suffix and isinstance(bottom_suffix[0], DielectricLayer):
            bottom_suffix[0] = deepcopy(outer_boundary_template)

    return Stackup(
        mode="rigid",
        soldermask=deepcopy(rigid_stackup.soldermask),
        layers=top_prefix + rebuilt_shared_layers + bottom_suffix,
    )


def rebuild_rigid_stackup_from_flex_zone(
    rigid_stackup: Stackup,
    flex_stackup: Stackup,
    bridge_dielectric_template: DielectricLayer | None = None,
    outer_boundary_dielectric_template: DielectricLayer | None = None,
) -> Stackup:
    start, end = rigid_shared_region_bounds(rigid_stackup, flex_stackup)
    top_prefix = [clone_layer(layer) for layer in rigid_stackup.layers[:start]]
    bottom_suffix = [clone_layer(layer) for layer in rigid_stackup.layers[end + 1 :]]
    sandwich_count = flex_stackup.flex_core_count()
    if sandwich_count <= 0:
        raise ValueError("Flex stackup must contain at least one flex-core sandwich.")

    bridge_template = deepcopy(bridge_dielectric_template) if bridge_dielectric_template is not None else None
    outer_boundary_template = (
        deepcopy(outer_boundary_dielectric_template)
        if outer_boundary_dielectric_template is not None
        else (deepcopy(bridge_dielectric_template) if bridge_dielectric_template is not None else None)
    )
    rebuilt_shared_layers: list[Layer] = []
    for sandwich_index in range(sandwich_count):
        flex_start = sandwich_index * 3
        rebuilt_shared_layers.extend(clone_layer(layer) for layer in flex_stackup.layers[flex_start : flex_start + 3])
        if sandwich_index < sandwich_count - 1:
            if bridge_template is None:
                raise ValueError("An inter-sandwich rigid dielectric template is required for multiple flex sandwiches.")
            rebuilt_shared_layers.append(deepcopy(bridge_template))

    if outer_boundary_template is not None:
        if top_prefix and isinstance(top_prefix[-1], DielectricLayer):
            top_prefix[-1] = deepcopy(outer_boundary_template)
        if bottom_suffix and isinstance(bottom_suffix[0], DielectricLayer):
            bottom_suffix[0] = deepcopy(outer_boundary_template)

    return Stackup(
        mode="rigid",
        soldermask=deepcopy(rigid_stackup.soldermask),
        layers=top_prefix + rebuilt_shared_layers + bottom_suffix,
    )


def build_default_rigid_flex_rigid_stackup(
    catalog: MaterialCatalog,
    *,
    flex_entry: FlexCoreEntry,
) -> Stackup:
    core = _preferred_material_entry(
        catalog,
        material_type="core",
        family="FR370HR",
        construction="3x7628",
        thickness_mm=0.61,
        classification="standard",
    )
    prepreg = _preferred_material_entry(
        catalog,
        material_type="prepreg",
        family="FR370HR",
        construction="1080",
        thickness_mm=0.081,
        classification="standard",
    )
    flex_layer = FlexCoreLayer.from_entry(flex_entry, selected_freq_ghz=flex_entry.max_freq_ghz)
    outer_copper_mm = from_display(1.0, "oz")
    inner_copper_mm = from_display(0.5, "oz")
    return Stackup(
        mode="rigid",
        layers=[
            CopperLayer(thickness_mm=outer_copper_mm, copper_type="RTF"),
            DielectricLayer(dielectric_type="core", material_id=core.id, selected_freq_ghz=core.max_freq_ghz),
            CopperLayer(thickness_mm=inner_copper_mm, copper_type="STD"),
            DielectricLayer(dielectric_type="prepreg", material_id=prepreg.id, selected_freq_ghz=prepreg.max_freq_ghz),
            CopperLayer(thickness_mm=flex_layer.copper_thickness_top_mm, copper_type=flex_layer.copper_type),
            flex_layer,
            CopperLayer(thickness_mm=flex_layer.copper_thickness_bottom_mm, copper_type=flex_layer.copper_type),
            DielectricLayer(dielectric_type="prepreg", material_id=prepreg.id, selected_freq_ghz=prepreg.max_freq_ghz),
            CopperLayer(thickness_mm=inner_copper_mm, copper_type="STD"),
            DielectricLayer(dielectric_type="core", material_id=core.id, selected_freq_ghz=core.max_freq_ghz),
            CopperLayer(thickness_mm=outer_copper_mm, copper_type="RTF"),
        ],
    )
