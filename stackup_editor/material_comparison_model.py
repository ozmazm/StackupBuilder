from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean

from stackup_editor.catalog import MaterialCatalog, MaterialEntry


_KEY_SEPARATOR = "\x1f"
_EXCLUDED_MANUFACTURERS = {"arlon"}


def manufacturer_is_comparable(manufacturer: str) -> bool:
    return manufacturer.strip().casefold() not in _EXCLUDED_MANUFACTURERS


@dataclass(frozen=True)
class FamilySummary:
    manufacturer: str
    family: str
    material_type: str
    frequency_ghz: float | None
    entry_count: int
    construction_count: int
    average_dk: float
    average_df: float
    min_thickness_mm: float
    max_thickness_mm: float
    min_resin_pct: float
    max_resin_pct: float
    max_frequency_ghz: float

    @property
    def key(self) -> str:
        return f"{self.manufacturer}{_KEY_SEPARATOR}{self.family}"

    @property
    def resin_span_pct(self) -> float:
        return self.max_resin_pct - self.min_resin_pct


RADAR_AXES = (
    "Average Dk",
    "Average Df",
    "Frequency ceiling",
    "Construction breadth",
    "Resin flexibility",
    "Thin-build access",
)

RADAR_AXIS_HELP = {
    "Average Dk": (
        "Arithmetic mean dielectric constant of the matching constructions at the selected "
        "frequency. A higher average plots farther outward."
    ),
    "Average Df": (
        "Arithmetic mean dissipation factor of the matching constructions at the selected "
        "frequency. Lower Df means lower dielectric loss; the graph plots the value directly, "
        "so a higher average plots farther outward."
    ),
    "Frequency ceiling": (
        "Highest characterization frequency available for the family in the catalog. A higher "
        "frequency plots farther outward."
    ),
    "Construction breadth": (
        "Number of unique glass-weave or laminate constructions matching the current filters. "
        "More constructions plot farther outward."
    ),
    "Resin flexibility": (
        "Span between the lowest and highest listed resin contents. It describes catalog choice, "
        "not mechanical flexibility; a wider span plots farther outward."
    ),
    "Thin-build access": (
        "Based on the thinnest listed construction. A thinner minimum build plots farther outward."
    ),
}


def entry_values(entry: MaterialEntry, frequency_ghz: float | None) -> tuple[float, float] | None:
    if frequency_ghz is None:
        return entry.reference_dk, entry.reference_df
    if not entry.has_frequency(frequency_ghz):
        return None
    return entry.dk_at(frequency_ghz), entry.df_at(frequency_ghz)


def build_family_summaries(
    catalog: MaterialCatalog,
    *,
    manufacturer: str | None = None,
    material_type: str | None = None,
    frequency_ghz: float | None = 10.0,
    search: str = "",
) -> list[FamilySummary]:
    search_folded = search.strip().casefold()
    grouped: dict[tuple[str, str], list[MaterialEntry]] = {}
    for entry in catalog.entries:
        if entry.material_type not in {"core", "prepreg"}:
            continue
        if not manufacturer_is_comparable(entry.manufacturer):
            continue
        if manufacturer and entry.manufacturer != manufacturer:
            continue
        if material_type and entry.material_type != material_type:
            continue
        haystack = f"{entry.manufacturer} {entry.family} {entry.series}".casefold()
        if search_folded and search_folded not in haystack:
            continue
        grouped.setdefault((entry.manufacturer, entry.family), []).append(entry)

    summaries: list[FamilySummary] = []
    for (entry_manufacturer, family), family_entries in grouped.items():
        valued_entries = [
            (entry, values)
            for entry in family_entries
            if (values := entry_values(entry, frequency_ghz)) is not None
        ]
        if not valued_entries:
            continue
        material_types = sorted({entry.material_type for entry, _values in valued_entries})
        summaries.append(
            FamilySummary(
                manufacturer=entry_manufacturer,
                family=family,
                material_type=material_types[0] if len(material_types) == 1 else "mixed",
                frequency_ghz=frequency_ghz,
                entry_count=len(valued_entries),
                construction_count=len({entry.construction for entry, _values in valued_entries}),
                average_dk=mean(values[0] for _entry, values in valued_entries),
                average_df=mean(values[1] for _entry, values in valued_entries),
                min_thickness_mm=min(entry.thickness_mm for entry, _values in valued_entries),
                max_thickness_mm=max(entry.thickness_mm for entry, _values in valued_entries),
                min_resin_pct=min(entry.resin_content_pct for entry, _values in valued_entries),
                max_resin_pct=max(entry.resin_content_pct for entry, _values in valued_entries),
                max_frequency_ghz=max(entry.max_freq_ghz for entry in family_entries),
            )
        )
    return sorted(summaries, key=lambda item: (item.manufacturer.casefold(), item.family.casefold()))


def normalized_profiles(summaries: list[FamilySummary]) -> dict[str, tuple[float, ...]]:
    if not summaries:
        return {}
    raw_rows = [
        (
            summary.average_dk,
            summary.average_df,
            summary.max_frequency_ghz,
            float(summary.construction_count),
            summary.resin_span_pct,
            -summary.min_thickness_mm,
        )
        for summary in summaries
    ]
    columns = list(zip(*raw_rows))
    mins = [min(column) for column in columns]
    maxs = [max(column) for column in columns]
    profiles: dict[str, tuple[float, ...]] = {}
    for summary, row in zip(summaries, raw_rows):
        scores = []
        for value, low, high in zip(row, mins, maxs):
            normalized = 0.65 if math.isclose(low, high) else (value - low) / (high - low)
            scores.append(0.18 + (0.82 * normalized))
        profiles[summary.key] = tuple(scores)
    return profiles


def frequency_label(frequency_ghz: float | None) -> str:
    if frequency_ghz is None:
        return "Catalog reference"
    if frequency_ghz < 1:
        return f"{frequency_ghz * 1000:g} MHz"
    return f"{frequency_ghz:g} GHz"
