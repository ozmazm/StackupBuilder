from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MaterialEntry:
    id: str
    manufacturer: str
    series: str
    family: str
    material_type: str
    variant: str
    source_pdf: str
    construction: str
    resin_content_pct: float
    thickness_mm: float
    thickness_in: float | None
    thickness_um: float
    classification: str | None
    style: str
    plies: int | None
    dk_by_freq_ghz: dict[float, float]
    df_by_freq_ghz: dict[float, float]
    reference_freq_ghz: float
    reference_dk: float
    reference_df: float
    max_freq_ghz: float
    notes: str

    @property
    def display_name(self) -> str:
        bits = [self.family, self.construction]
        bits.append(f"{self.thickness_mm:.3f} mm")
        bits.append(f"RC {self.resin_content_pct:.1f}%")
        bits.append(f"Dk {self.reference_dk:.3f}")
        if self.classification:
            bits.append(self.classification)
        return " | ".join(bits)

    @property
    def frequency_summary(self) -> str:
        parts = []
        for freq in self.sorted_frequencies:
            dk = self.dk_by_freq_ghz[freq]
            df = self.df_by_freq_ghz[freq]
            label = f"{freq:.3g} GHz" if freq >= 1 else f"{freq * 1000:.0f} MHz"
            parts.append(f"{label}: Dk {dk:.3f} / Df {df:.4f}")
        return "; ".join(parts)

    @property
    def sorted_frequencies(self) -> list[float]:
        return sorted(self.dk_by_freq_ghz)

    def has_frequency(self, freq_ghz: float) -> bool:
        return freq_ghz in self.dk_by_freq_ghz and freq_ghz in self.df_by_freq_ghz

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


class MaterialCatalog:
    def __init__(self, entries: list[MaterialEntry]) -> None:
        self.entries = entries
        self._by_id = {entry.id: entry for entry in entries}

    @classmethod
    def load(cls, path: Path) -> "MaterialCatalog":
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = []
        for raw in payload["materials"]:
            entries.append(
                MaterialEntry(
                    id=raw["id"],
                    manufacturer=raw["manufacturer"],
                    series=raw["series"],
                    family=raw["family"],
                    material_type=raw["material_type"],
                    variant=raw["variant"],
                    source_pdf=raw["source_pdf"],
                    construction=raw["construction"],
                    resin_content_pct=float(raw["resin_content_pct"]),
                    thickness_mm=float(raw["thickness_mm"]),
                    thickness_in=float(raw["thickness_in"]) if raw["thickness_in"] is not None else None,
                    thickness_um=float(raw["thickness_um"]),
                    classification=raw["classification"],
                    style=raw["style"],
                    plies=int(raw["plies"]) if raw["plies"] is not None else None,
                    dk_by_freq_ghz={float(key): float(value) for key, value in raw["dk_by_freq_ghz"].items()},
                    df_by_freq_ghz={float(key): float(value) for key, value in raw["df_by_freq_ghz"].items()},
                    reference_freq_ghz=float(raw["reference_freq_ghz"]),
                    reference_dk=float(raw["reference_dk"]),
                    reference_df=float(raw["reference_df"]),
                    max_freq_ghz=float(raw["max_freq_ghz"]),
                    notes=raw["notes"],
                )
            )
        entries.sort(key=lambda item: (item.manufacturer, item.family, item.material_type, item.thickness_mm))
        return cls(entries)

    def find(self, material_id: str) -> MaterialEntry:
        return self._by_id[material_id]

    def get(self, material_id: str) -> MaterialEntry | None:
        return self._by_id.get(material_id)

    def filter_entries(
        self,
        *,
        material_type: str | None = None,
        manufacturer: str | None = None,
        family: str | None = None,
    ) -> list[MaterialEntry]:
        items = self.entries
        if material_type:
            items = [entry for entry in items if entry.material_type == material_type]
        if manufacturer:
            items = [entry for entry in items if entry.manufacturer == manufacturer]
        if family:
            items = [entry for entry in items if entry.family == family]
        return items

    def manufacturers(self, material_type: str | None = None) -> list[str]:
        return sorted({entry.manufacturer for entry in self.filter_entries(material_type=material_type)})

    def families(self, *, material_type: str, manufacturer: str | None = None) -> list[str]:
        return sorted(
            {entry.family for entry in self.filter_entries(material_type=material_type, manufacturer=manufacturer)}
        )

    def first_for(self, material_type: str, preferred_family: str | None = None) -> MaterialEntry:
        if preferred_family:
            preferred = self.filter_entries(material_type=material_type, family=preferred_family)
            if preferred:
                return preferred[0]
        items = self.filter_entries(material_type=material_type)
        if not items:
            raise LookupError(f"No material entries available for type {material_type!r}")
        return items[0]
