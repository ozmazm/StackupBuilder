"""flex_catalog.py — loaders for the rigid-flex material catalogs.

Mirrors the shape of stackup_editor.catalog.MaterialCatalog, but for the two
flex-specific catalogs built by tools/build_flex_material_catalog.py:

  - FlexCoreMaterialCatalog  <- data/flex_core_material_catalog.json
                                (Panasonic R-F777 polyimide core + copper foil)
  - CoverlayMaterialCatalog  <- data/coverlay_material_catalog.json
                                (Arisawa C33 coverlay polyimide + adhesive)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Panasonic's sheet calls copper types "ED" (electrodeposited) and "RA"
# (rolled-annealed). The rest of the app's copper-roughness model is keyed
# off the rigid-side names STD/VLP. Per spec: ED behaves like STD roughness,
# RA behaves like VLP roughness -- but the UI must always display "ED"/"RA"
# for flex-core copper, never "STD"/"VLP".
FLEX_TO_RIGID_COPPER_TYPE = {"ED": "STD", "RA": "VLP"}
RIGID_TO_FLEX_COPPER_TYPE = {value: key for key, value in FLEX_TO_RIGID_COPPER_TYPE.items()}


@dataclass(frozen=True)
class FlexCoreEntry:
    id: str
    manufacturer: str
    series: str
    family: str
    variant_code: str
    copper_type: str  # "ED" or "RA"
    copper_thickness_top_um: float
    copper_thickness_bottom_um: float
    symmetric_copper: bool
    dielectric_thickness_um: float
    dielectric_thickness_mm: float
    total_product_thickness_um: float
    dk_by_freq_ghz: dict[float, float]
    df_by_freq_ghz: dict[float, float]
    reference_freq_ghz: float
    reference_dk: float
    reference_df: float
    max_freq_ghz: float
    source_pdf: str
    notes: str

    @property
    def sorted_frequencies(self) -> list[float]:
        return sorted(self.dk_by_freq_ghz)

    def closest_frequency(self, target_ghz: float | None = None) -> float:
        options = self.sorted_frequencies
        if not options:
            raise LookupError(f"No frequencies available for {self.id}")
        if target_ghz is None:
            return options[-1]
        return min(options, key=lambda freq: (abs(freq - target_ghz), freq))

    def dk_at(self, freq_ghz: float) -> float:
        return self.dk_by_freq_ghz[freq_ghz]

    def df_at(self, freq_ghz: float) -> float:
        return self.df_by_freq_ghz[freq_ghz]

    @property
    def construction_label(self) -> str:
        top = f"{self.copper_thickness_top_um:g}"
        pi = f"{self.dielectric_thickness_um:g}"
        bottom = f"{self.copper_thickness_bottom_um:g}"
        return f"{top}-{pi}-{bottom}um ({self.copper_type})"

    @property
    def display_label(self) -> str:
        # Matches the requested format: "R-F777 11ED | 35-25-35um (ED) | Dk 3.2"
        return f"{self.family} {self.variant_code} | {self.construction_label} | Dk {self.reference_dk:g}"

    @property
    def rigid_copper_type(self) -> str:
        """The STD/VLP-space equivalent, for reuse of existing roughness lookups."""
        return FLEX_TO_RIGID_COPPER_TYPE.get(self.copper_type, "STD")


class FlexCoreMaterialCatalog:
    def __init__(self, entries: list[FlexCoreEntry]) -> None:
        self.entries = entries
        self._by_id = {entry.id: entry for entry in entries}

    @classmethod
    def load(cls, path: Path) -> "FlexCoreMaterialCatalog":
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = []
        for raw in payload["materials"]:
            entries.append(
                FlexCoreEntry(
                    id=raw["id"],
                    manufacturer=raw["manufacturer"],
                    series=raw["series"],
                    family=raw["family"],
                    variant_code=raw["variant_code"],
                    copper_type=raw["copper_type"],
                    copper_thickness_top_um=float(raw["copper_thickness_top_um"]),
                    copper_thickness_bottom_um=float(raw["copper_thickness_bottom_um"]),
                    symmetric_copper=bool(raw["symmetric_copper"]),
                    dielectric_thickness_um=float(raw["dielectric_thickness_um"]),
                    dielectric_thickness_mm=float(raw["dielectric_thickness_mm"]),
                    total_product_thickness_um=float(raw["total_product_thickness_um"]),
                    dk_by_freq_ghz={float(k): float(v) for k, v in raw["dk_by_freq_ghz"].items()},
                    df_by_freq_ghz={float(k): float(v) for k, v in raw["df_by_freq_ghz"].items()},
                    reference_freq_ghz=float(raw["reference_freq_ghz"]),
                    reference_dk=float(raw["reference_dk"]),
                    reference_df=float(raw["reference_df"]),
                    max_freq_ghz=float(raw["max_freq_ghz"]),
                    source_pdf=raw["source_pdf"],
                    notes=raw["notes"],
                )
            )
        entries.sort(key=lambda item: (item.dielectric_thickness_um, item.copper_thickness_top_um, item.variant_code))
        return cls(entries)

    def find(self, material_id: str) -> FlexCoreEntry:
        return self._by_id[material_id]

    def get(self, material_id: str) -> FlexCoreEntry | None:
        return self._by_id.get(material_id)

    def filter_entries(
        self,
        *,
        manufacturer: str | None = None,
        family: str | None = None,
    ) -> list[FlexCoreEntry]:
        items = self.entries
        if manufacturer:
            items = [entry for entry in items if entry.manufacturer == manufacturer]
        if family:
            items = [entry for entry in items if entry.family == family]
        return items

    def manufacturers(self) -> list[str]:
        return sorted({entry.manufacturer for entry in self.entries})

    def families(self, *, manufacturer: str | None = None) -> list[str]:
        return sorted(
            {
                entry.family
                for entry in self.filter_entries(manufacturer=manufacturer)
            }
        )


@dataclass(frozen=True)
class CoverlayEntry:
    id: str
    manufacturer: str
    series: str
    family: str
    component: str  # "coverlay_pi" or "coverlay_adhesive"
    thickness_um: float
    thickness_mm: float
    dk_by_freq_ghz: dict[float, float]
    df_by_freq_ghz: dict[float, float]
    reference_freq_ghz: float
    reference_dk: float
    reference_df: float
    max_freq_ghz: float
    peel_strength_n_per_cm: float
    solder_heat_resistance_c: float
    source_pdf: str
    notes: str

    @property
    def sorted_frequencies(self) -> list[float]:
        return sorted(self.dk_by_freq_ghz)

    def closest_frequency(self, target_ghz: float | None = None) -> float:
        options = self.sorted_frequencies
        if not options:
            raise LookupError(f"No frequencies available for {self.id}")
        if target_ghz is None:
            return options[-1]
        return min(options, key=lambda freq: (abs(freq - target_ghz), freq))

    def dk_at(self, freq_ghz: float) -> float:
        return self.dk_by_freq_ghz[freq_ghz]

    def df_at(self, freq_ghz: float) -> float:
        return self.df_by_freq_ghz[freq_ghz]

    @property
    def display_label(self) -> str:
        prefix = "CVL-PI" if self.component == "coverlay_pi" else "CVL-Adh."
        return f"{prefix} {self.thickness_um:g}um ({self.family})"


class CoverlayMaterialCatalog:
    def __init__(self, entries: list[CoverlayEntry]) -> None:
        self.entries = entries
        self._by_id = {entry.id: entry for entry in entries}

    @classmethod
    def load(cls, path: Path) -> "CoverlayMaterialCatalog":
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = []
        for raw in payload["materials"]:
            entries.append(
                CoverlayEntry(
                    id=raw["id"],
                    manufacturer=raw["manufacturer"],
                    series=raw["series"],
                    family=raw["family"],
                    component=raw["component"],
                    thickness_um=float(raw["thickness_um"]),
                    thickness_mm=float(raw["thickness_mm"]),
                    dk_by_freq_ghz={float(k): float(v) for k, v in raw["dk_by_freq_ghz"].items()},
                    df_by_freq_ghz={float(k): float(v) for k, v in raw["df_by_freq_ghz"].items()},
                    reference_freq_ghz=float(raw["reference_freq_ghz"]),
                    reference_dk=float(raw["reference_dk"]),
                    reference_df=float(raw["reference_df"]),
                    max_freq_ghz=float(raw["max_freq_ghz"]),
                    peel_strength_n_per_cm=float(raw["peel_strength_n_per_cm"]),
                    solder_heat_resistance_c=float(raw["solder_heat_resistance_c"]),
                    source_pdf=raw["source_pdf"],
                    notes=raw["notes"],
                )
            )
        return cls(entries)

    def find(self, material_id: str) -> CoverlayEntry:
        return self._by_id[material_id]

    def get(self, material_id: str) -> CoverlayEntry | None:
        return self._by_id.get(material_id)

    def pi_component(self) -> CoverlayEntry:
        return next(entry for entry in self.entries if entry.component == "coverlay_pi")

    def adhesive_component(self) -> CoverlayEntry:
        return next(entry for entry in self.entries if entry.component == "coverlay_adhesive")
