from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from pypdf import PdfReader

try:
    import pdfplumber
except ImportError:
    pdfplumber = None


ROOT = Path(__file__).resolve().parents[1]
PDF_ROOT = ROOT / "Materials"
OUTPUT = ROOT / "data" / "material_catalog.json"

MEGTRON_FAMILY_LABELS = {
    "R-579Y(N)": "Megtron8 R-579Y(N)",
    "R-569Y(N)": "Megtron8 R-569Y(N)",
    "R-579Y(U)": "Megtron8 R-579Y(U)",
    "R-569Y(U)": "Megtron8 R-569Y(U)",
    "R-578Y(N)": "Megtron7 R-578Y(N)",
    "R-568Y(N)": "Megtron7 R-568Y(N)",
    "R-5785(N)": "Megtron7 R-5785(N)",
    "R-5680(N)": "Megtron7 R-5680(N)",
    "R-5775(N)": "Megtron6 R-5775(N)",
    "R-5670(N)": "Megtron6 R-5670(N)",
    "R-5775(K)": "Megtron6 R-5775(K)",
    "R-5670(K)": "Megtron6 R-5670(K)",
    "R-5775(G)": "Megtron6 R-5775(G)",
    "R-5670(G)": "Megtron6 R-5670(G)",
}

PDF_SOURCES = {
    "fr408hr-laminate-and-prepreg__Dk_Df_Tables.pdf": {
        "manufacturer": "Isola",
        "series": "FR408HR",
        "family": "FR408HR",
        "parser": "isola",
    },
    "i-speed-laminate-and-prepreg__Dk_Df_Tables.pdf": {
        "manufacturer": "Isola",
        "series": "I-Speed",
        "family": "I-Speed",
        "parser": "isola",
    },
    "i-tera-mt40__Dk_Df_Tables.pdf": {
        "manufacturer": "Isola",
        "series": "I-Tera MT40",
        "family": "I-Tera MT40",
        "parser": "isola",
    },
    "i-tera-mt40-rf-mw__Dk_Df_Tables.pdf": {
        "manufacturer": "Isola",
        "series": "I-Tera MT40 RF/MW",
        "family": "I-Tera MT40 RF/MW",
        "parser": "isola",
    },
    "tachyon-100g-laminate-and-prepreg__Dk_Df_Tables.pdf": {
        "manufacturer": "Isola",
        "series": "Tachyon 100G",
        "family": "Tachyon 100G",
        "parser": "isola",
    },
    "terragreen-400ge__Dk_Df_Tables.pdf": {
        "manufacturer": "Isola",
        "series": "TerraGreen 400GE",
        "family": "TerraGreen 400GE",
        "parser": "isola",
    },
    "CDS_MEGTRON6_R-5775_22081031.pdf": {
        "manufacturer": "Panasonic",
        "series": "MEGTRON 6",
        "family": "MEGTRON 6",
        "parser": "megtron",
    },
    "MEGTRON7_R-578Y(N).pdf": {
        "manufacturer": "Panasonic",
        "series": "MEGTRON 7",
        "family": "MEGTRON 7",
        "parser": "megtron",
    },
    "MEGTRON_R-579Y.pdf": {
        "manufacturer": "Panasonic",
        "series": "MEGTRON",
        "family": "MEGTRON",
        "parser": "megtron",
    },
    "Megtron6_R-5775(N)_R-5670(N).pdf": {
        "manufacturer": "Panasonic",
        "series": "MEGTRON 6",
        "family": "MEGTRON 6",
        "parser": "megtron",
    },
    "Megtron6_R-5775(K)_R-5670(K)_R-5775(G)_R-5670(G).pdf": {
        "manufacturer": "Panasonic",
        "series": "MEGTRON 6",
        "family": "MEGTRON 6",
        "parser": "megtron",
    },
    "Megtron7_R-5785(N)_R-5680(N).pdf": {
        "manufacturer": "Panasonic",
        "series": "MEGTRON 7",
        "family": "MEGTRON 7",
        "parser": "megtron",
    },
    "ThunderClad 3_Dk Df_Table.pdf": {
        "manufacturer": "TUC",
        "series": "ThunderClad 3",
        "family": "ThunderClad 3",
        "parser": "tuc",
    },
    "ThunderClad 3+_Dk Df_Table.pdf": {
        "manufacturer": "TUC",
        "series": "ThunderClad 3+",
        "family": "ThunderClad 3+",
        "parser": "tuc",
    },
    "N4000-13EP Laminate Table.pdf": {
        "manufacturer": "Nelco",
        "series": "N4000-13 EP",
        "family": "N4000-13 EP",
        "parser": "nelco",
    },
    "N4000-13EP SI Laminate Table.pdf": {
        "manufacturer": "Nelco",
        "series": "N4000-13 EP SI",
        "family": "N4000-13 EP SI",
        "parser": "nelco",
    },
    "N4000-6-6FC-Laminate Tables.pdf": {
        "manufacturer": "Nelco",
        "series": "N4000-6",
        "family": "N4000-6",
        "parser": "nelco",
    },
    "FR370HR.pdf": {
        "manufacturer": "Isola",
        "series": "FR370HR",
        "family": "FR370HR",
        "parser": "isola",
    },
    "I-TERA MT40.pdf": {
        "manufacturer": "Isola",
        "series": "I-Tera MT40",
        "family": "I-Tera MT40",
        "parser": "isola",
    },
    "IS415.pdf": {
        "manufacturer": "Isola",
        "series": "IS415",
        "family": "IS415",
        "parser": "isola",
    },
    "S1000-2M.pdf": {
        "manufacturer": "Shengyi",
        "series": "S1000-2M",
        "family": "S1000-2M",
        "variant": "S1000-2M/S1000-2MB",
        "parser": "shengyi",
    },
}


@dataclass
class MaterialRecord:
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
    dk_by_freq_ghz: dict[str, float]
    df_by_freq_ghz: dict[str, float]
    reference_freq_ghz: float
    reference_dk: float
    reference_df: float
    max_freq_ghz: float
    notes: str


def main() -> None:
    refreshed_sources = {
        pdf_path.name
        for pdf_path in sorted(PDF_ROOT.rglob("*.pdf"))
        if pdf_path.name in PDF_SOURCES
    }
    existing_records = [
        record for record in load_existing_records(OUTPUT) if record.source_pdf not in refreshed_sources
    ]
    records_by_identity = {record_identity(record): record for record in existing_records}
    parsed_count = 0

    for pdf_path in sorted(PDF_ROOT.rglob("*.pdf")):
        source = PDF_SOURCES.get(pdf_path.name)
        if source is None:
            continue
        parser = source["parser"]
        if parser == "isola":
            parsed_records = parse_isola_table_pdf(pdf_path, source)
        elif parser == "megtron":
            parsed_records = parse_megtron_pdf(pdf_path, source)
        elif parser == "tuc":
            parsed_records = parse_tuc_table_pdf(pdf_path, source)
        elif parser == "nelco":
            parsed_records = parse_nelco_table_pdf(pdf_path, source)
        elif parser == "shengyi":
            parsed_records = parse_shengyi_table_pdf(pdf_path, source)
        else:
            parsed_records = []

        parsed_count += len(parsed_records)
        for record in parsed_records:
            records_by_identity[record_identity(record)] = record

    records = sorted(
        records_by_identity.values(),
        key=lambda item: (
            item.manufacturer,
            item.family,
            item.material_type,
            item.variant,
            item.thickness_mm,
            item.construction,
            item.classification or "",
        ),
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_from": "local datasheet PDFs merged with the existing catalog",
        "materials": [asdict(record) for record in records],
    }
    OUTPUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(records)} material rows to {OUTPUT} ({parsed_count} refreshed from {PDF_ROOT})")


def load_existing_records(path: Path) -> list[MaterialRecord]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = []
    for raw in payload.get("materials", []):
        normalized = dict(raw)
        if normalized.get("manufacturer") == "Panasonic":
            normalized["family"] = normalize_megtron_family(normalized.get("family", ""))
        records.append(MaterialRecord(**normalized))
    return records


def record_identity(record: MaterialRecord) -> tuple[object, ...]:
    return (
        record.manufacturer,
        record.series,
        record.family,
        record.material_type,
        record.variant,
        record.construction,
        round(record.resin_content_pct, 6),
        round(record.thickness_mm, 6),
        record.classification or "",
    )


def parse_isola_table_pdf(pdf_path: Path, source: dict[str, str]) -> list[MaterialRecord]:
    manufacturer = source["manufacturer"]
    series = source["series"]
    family = source["family"]
    reader = PdfReader(str(pdf_path))

    sections: dict[str, list[str]] = defaultdict(list)
    current_section: str | None = None

    for page in reader.pages:
        text = (page.extract_text() or "").replace("\x00", " ")
        if "Prepreg Dielectric Constant" in text:
            current_section = "prepreg"
        elif "Core Data" in text:
            current_section = "core"
        elif text.lstrip().startswith("NOTE"):
            current_section = None
            continue

        if current_section:
            sections[current_section].append(text)

    records = []
    for material_type in ("core", "prepreg"):
        block = "\n".join(sections[material_type])
        if not block.strip():
            continue
        records.extend(parse_isola_block(block, pdf_path.name, manufacturer, series, family, material_type))
    return records


def parse_isola_block(
    block: str,
    source_pdf: str,
    manufacturer: str,
    series: str,
    family: str,
    material_type: str,
) -> list[MaterialRecord]:
    normalized = re.sub(r"\s+", " ", block)
    freqs = parse_frequency_tokens(normalized)
    if not freqs:
        raise ValueError(f"Could not find frequencies in {source_pdf} ({material_type})")

    row_pattern = re.compile(
        r"(?P<construction>[A-Za-z0-9xX/().-]+)\s+"
        r"(?P<resin>\d+(?:\.\d+)?)%\s+"
        r"(?:(?P<classification>Standard|Alternate)\s+)?"
        r"(?P<thickness_in>\d+\.\d+)\s+"
        r"(?P<thickness_mm>\d+\.\d+)"
    )
    matches = list(row_pattern.finditer(normalized))
    records = []

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        numeric_values = re.findall(r"\d+\.\d+", normalized[start:end])[: len(freqs) * 2]
        if len(numeric_values) != len(freqs) * 2:
            continue

        dk_map, df_map = build_frequency_maps(freqs, numeric_values)
        thickness_mm = float(match.group("thickness_mm"))
        thickness_in = float(match.group("thickness_in"))
        construction = match.group("construction")
        classification = match.group("classification")
        resin_pct = float(match.group("resin"))
        max_freq = max(freqs)

        record = MaterialRecord(
            id=make_id(source_pdf, material_type, family, construction, thickness_mm, classification),
            manufacturer=manufacturer,
            series=series,
            family=family,
            material_type=material_type,
            variant=family,
            source_pdf=source_pdf,
            construction=construction,
            resin_content_pct=resin_pct,
            thickness_mm=thickness_mm,
            thickness_in=thickness_in,
            thickness_um=thickness_mm * 1000,
            classification=classification,
            style=construction,
            plies=parse_ply_count(construction),
            dk_by_freq_ghz=dk_map,
            df_by_freq_ghz=df_map,
            reference_freq_ghz=max_freq,
            reference_dk=dk_map[str(max_freq)],
            reference_df=df_map[str(max_freq)],
            max_freq_ghz=max_freq,
            notes=f"{material_type} row parsed from local Isola Dk/Df table.",
        )
        records.append(record)

    return records


def parse_megtron_pdf(pdf_path: Path, source: dict[str, str]) -> list[MaterialRecord]:
    manufacturer = source["manufacturer"]
    series = source["series"]
    reader = PdfReader(str(pdf_path))
    sections: dict[tuple[str, str, str], dict[str, str]] = defaultdict(dict)

    for page in reader.pages:
        text = (page.extract_text() or "").replace("\x00", " ")
        if "Dielectric Properties" not in text:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = next((line for line in lines if "Dielectric Properties" in line), "")
        parsed = parse_megtron_title(title)
        if not parsed:
            continue
        material_type, family, variant = parsed
        prop = "dk" if "Typical Dk" in text else "df"
        sections[(material_type, family, variant)][prop] = text

    records = []
    for (material_type, family, variant), pages in sections.items():
        if "dk" not in pages or "df" not in pages:
            continue
        if material_type == "core":
            dk_rows = parse_megtron_core_rows(pages["dk"])
            df_rows = parse_megtron_core_rows(pages["df"])
        else:
            dk_rows = parse_megtron_prepreg_rows(pages["dk"])
            df_rows = parse_megtron_prepreg_rows(pages["df"])

        df_lookup = {row["key"]: row for row in df_rows}
        for dk_row in dk_rows:
            df_row = df_lookup.get(dk_row["key"])
            if not df_row:
                continue

            frequencies = dk_row["frequencies"]
            dk_map = {str(freq): value for freq, value in zip(frequencies, dk_row["values"])}
            df_map = {str(freq): value for freq, value in zip(frequencies, df_row["values"])}
            max_freq = max(frequencies)

            records.append(
                MaterialRecord(
                    id=make_id(pdf_path.name, material_type, family, dk_row["construction"], dk_row["thickness_mm"], variant),
                    manufacturer=manufacturer,
                    series=series,
                    family=family,
                    material_type=material_type,
                    variant=variant,
                    source_pdf=pdf_path.name,
                    construction=dk_row["construction"],
                    resin_content_pct=dk_row["resin_content_pct"],
                    thickness_mm=dk_row["thickness_mm"],
                    thickness_in=dk_row["thickness_in"],
                    thickness_um=dk_row["thickness_mm"] * 1000,
                    classification=None,
                    style=dk_row["style"],
                    plies=dk_row["plies"],
                    dk_by_freq_ghz=dk_map,
                    df_by_freq_ghz=df_map,
                    reference_freq_ghz=max_freq,
                    reference_dk=dk_map[str(max_freq)],
                    reference_df=df_map[str(max_freq)],
                    max_freq_ghz=max_freq,
                    notes=f"{variant} parsed from local Panasonic/Megtron datasheet.",
                )
            )

    if records:
        return records
    return parse_megtron_spec_pdf(pdf_path, source)


def parse_megtron_spec_pdf(pdf_path: Path, source: dict[str, str]) -> list[MaterialRecord]:
    manufacturer = source["manufacturer"]
    series = source["series"]
    reader = PdfReader(str(pdf_path))
    page_texts = [(page.extract_text() or "").replace("\x00", " ") for page in reader.pages]
    core_sections: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    prepreg_sections: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)

    for page_index, text in enumerate(page_texts):
        parsed = parse_megtron_spec_header(text)
        if not parsed:
            continue
        material_type, family, raw_family, prop = parsed
        if material_type == "core":
            core_sections[(family, raw_family)][prop] = text
        else:
            prepreg_sections[(family, raw_family)][prop] = page_index

    records: list[MaterialRecord] = []
    for (family, raw_family), pages in core_sections.items():
        if "dk" not in pages or "df" not in pages:
            continue
        dk_rows = parse_megtron_core_rows(pages["dk"])
        df_rows = parse_megtron_core_rows(pages["df"])
        df_lookup = {row["key"]: row for row in df_rows}
        for dk_row in dk_rows:
            df_row = df_lookup.get(dk_row["key"])
            if not df_row:
                continue

            frequencies = dk_row["frequencies"]
            dk_map = {str(freq): value for freq, value in zip(frequencies, dk_row["values"])}
            df_map = {str(freq): value for freq, value in zip(frequencies, df_row["values"])}
            max_freq = max(frequencies)
            records.append(
                MaterialRecord(
                    id=make_id(pdf_path.name, "core", family, dk_row["construction"], dk_row["thickness_mm"], raw_family),
                    manufacturer=manufacturer,
                    series=series,
                    family=family,
                    material_type="core",
                    variant=raw_family,
                    source_pdf=pdf_path.name,
                    construction=dk_row["construction"],
                    resin_content_pct=dk_row["resin_content_pct"],
                    thickness_mm=dk_row["thickness_mm"],
                    thickness_in=dk_row["thickness_in"],
                    thickness_um=dk_row["thickness_mm"] * 1000,
                    classification=None,
                    style=dk_row["style"],
                    plies=dk_row["plies"],
                    dk_by_freq_ghz=dk_map,
                    df_by_freq_ghz=df_map,
                    reference_freq_ghz=max_freq,
                    reference_dk=dk_map[str(max_freq)],
                    reference_df=df_map[str(max_freq)],
                    max_freq_ghz=max_freq,
                    notes=f"{raw_family} parsed from local Panasonic/Megtron datasheet.",
                )
            )

    if prepreg_sections:
        if pdfplumber is None:
            raise ValueError(
                "pdfplumber is required to parse the newer Panasonic prepreg datasheets. "
                "Use the bundled workspace Python or install pdfplumber."
            )
        with pdfplumber.open(str(pdf_path)) as pdf:
            for (family, raw_family), pages in prepreg_sections.items():
                if "dk" not in pages or "df" not in pages:
                    continue
                dk_rows = parse_megtron_prepreg_spec_rows(page_texts[pages["dk"]], pdf.pages[pages["dk"]])
                df_rows = parse_megtron_prepreg_spec_rows(page_texts[pages["df"]], pdf.pages[pages["df"]])
                df_lookup = {row["key"]: row for row in df_rows}
                for dk_row in dk_rows:
                    df_row = df_lookup.get(dk_row["key"])
                    if not df_row:
                        continue

                    frequencies = dk_row["frequencies"]
                    dk_map = {str(freq): value for freq, value in zip(frequencies, dk_row["values"])}
                    df_map = {str(freq): value for freq, value in zip(frequencies, df_row["values"])}
                    max_freq = max(frequencies)
                    records.append(
                        MaterialRecord(
                            id=make_id(
                                pdf_path.name,
                                "prepreg",
                                family,
                                dk_row["construction"],
                                dk_row["thickness_mm"],
                                raw_family,
                            ),
                            manufacturer=manufacturer,
                            series=series,
                            family=family,
                            material_type="prepreg",
                            variant=raw_family,
                            source_pdf=pdf_path.name,
                            construction=dk_row["construction"],
                            resin_content_pct=dk_row["resin_content_pct"],
                            thickness_mm=dk_row["thickness_mm"],
                            thickness_in=dk_row["thickness_in"],
                            thickness_um=dk_row["thickness_mm"] * 1000,
                            classification=None,
                            style=dk_row["style"],
                            plies=dk_row["plies"],
                            dk_by_freq_ghz=dk_map,
                            df_by_freq_ghz=df_map,
                            reference_freq_ghz=max_freq,
                            reference_dk=dk_map[str(max_freq)],
                            reference_df=df_map[str(max_freq)],
                            max_freq_ghz=max_freq,
                            notes=f"{raw_family} parsed from local Panasonic/Megtron datasheet.",
                        )
                    )

    return records


def parse_tuc_table_pdf(pdf_path: Path, source: dict[str, str]) -> list[MaterialRecord]:
    manufacturer = source["manufacturer"]
    series = source["series"]
    family = source["family"]
    reader = PdfReader(str(pdf_path))
    pages = [(page.extract_text() or "").replace("\x00", " ") for page in reader.pages]
    if len(pages) < 2:
        raise ValueError(f"Unexpected TUC table layout in {pdf_path.name}")

    frequencies = [1.0, 3.0, 5.0, 10.0, 15.0, 20.0]
    records = []
    records.extend(parse_tuc_core_rows(pages[0], pdf_path.name, manufacturer, series, family, frequencies))
    records.extend(parse_tuc_prepreg_rows(pages[1], pdf_path.name, manufacturer, series, family, frequencies))
    return records


def parse_tuc_core_rows(
    text: str,
    source_pdf: str,
    manufacturer: str,
    series: str,
    family: str,
    frequencies: list[float],
) -> list[MaterialRecord]:
    records = []
    for line in pdf_lines(text):
        if not re.match(r"^\d+\.\d+\s+\d+\.\d+\s+[*0-9A-Za-zxX]+", line):
            continue
        tokens = repair_numeric_token_splits(line.split())
        if len(tokens) != 15:
            continue

        thickness_in = float(tokens[0])
        thickness_mm = float(tokens[1])
        construction = tokens[2].replace("*", "")
        dk_map, df_map = build_grouped_frequency_maps(frequencies, [float(token) for token in tokens[3:]])
        style, plies = parse_tuc_construction(construction)
        max_freq = max(frequencies)
        records.append(
            MaterialRecord(
                id=make_id(source_pdf, "core", family, construction, thickness_mm, None),
                manufacturer=manufacturer,
                series=series,
                family=family,
                material_type="core",
                variant=family,
                source_pdf=source_pdf,
                construction=construction,
                resin_content_pct=0.0,
                thickness_mm=thickness_mm,
                thickness_in=thickness_in,
                thickness_um=thickness_mm * 1000,
                classification=None,
                style=style,
                plies=plies,
                dk_by_freq_ghz=dk_map,
                df_by_freq_ghz=df_map,
                reference_freq_ghz=max_freq,
                reference_dk=dk_map[str(max_freq)],
                reference_df=df_map[str(max_freq)],
                max_freq_ghz=max_freq,
                notes="core row parsed from local TUC Dk/Df table.",
            )
        )
    return records


def parse_tuc_prepreg_rows(
    text: str,
    source_pdf: str,
    manufacturer: str,
    series: str,
    family: str,
    frequencies: list[float],
) -> list[MaterialRecord]:
    records = []
    for line in pdf_lines(text):
        if not re.match(r"^\d+\.\d+\s+\d+\.\d+\s+[*0-9A-Za-z]+\s+\d+", line):
            continue
        tokens = repair_numeric_token_splits(line.split())
        if len(tokens) != 16:
            continue

        thickness_in = float(tokens[0])
        thickness_mm = float(tokens[1])
        construction = tokens[2].lstrip("*")
        resin_pct = float(tokens[3])
        dk_map, df_map = build_grouped_frequency_maps(frequencies, [float(token) for token in tokens[4:]])
        max_freq = max(frequencies)
        records.append(
            MaterialRecord(
                id=make_id(source_pdf, "prepreg", family, construction, thickness_mm, None),
                manufacturer=manufacturer,
                series=series,
                family=family,
                material_type="prepreg",
                variant=family,
                source_pdf=source_pdf,
                construction=construction,
                resin_content_pct=resin_pct,
                thickness_mm=thickness_mm,
                thickness_in=thickness_in,
                thickness_um=thickness_mm * 1000,
                classification=None,
                style=construction,
                plies=None,
                dk_by_freq_ghz=dk_map,
                df_by_freq_ghz=df_map,
                reference_freq_ghz=max_freq,
                reference_dk=dk_map[str(max_freq)],
                reference_df=df_map[str(max_freq)],
                max_freq_ghz=max_freq,
                notes="prepreg row parsed from local TUC Dk/Df table.",
            )
        )
    return records


def parse_nelco_table_pdf(pdf_path: Path, source: dict[str, str]) -> list[MaterialRecord]:
    manufacturer = source["manufacturer"]
    series = source["series"]
    family = source["family"]
    reader = PdfReader(str(pdf_path))
    pages = [(page.extract_text() or "").replace("\x00", " ") for page in reader.pages]
    frequencies = [2.0, 10.0]

    records = []
    for page_text in pages:
        records.extend(parse_nelco_core_rows(page_text, pdf_path.name, manufacturer, series, family, frequencies))
        records.extend(parse_nelco_prepreg_rows(page_text, pdf_path.name, manufacturer, series, family, frequencies))
    return records


def parse_nelco_core_rows(
    text: str,
    source_pdf: str,
    manufacturer: str,
    series: str,
    family: str,
    frequencies: list[float],
) -> list[MaterialRecord]:
    row_pattern = re.compile(
        r"^(?P<thickness_in>0\.\d+)\s+±\s+(?P<tolerance>0\.\d+)\s+"
        r"(?P<construction>.+?)\s+(?P<resin>\d+(?:\.\d+)?)%\s+"
        r"(?P<dk_2>\d+\.\d+)\s+±\s+\d+\.\d+\s+"
        r"(?P<df_2>\d+\.\d+)\s+±\s+\d+\.\d+\s+"
        r"(?P<dk_10>\d+\.\d+)\s+±\s+\d+\.\d+\s+"
        r"(?P<df_10>\d+\.\d+)\s+±\s+\d+\.\d+"
    )

    records = []
    for line in pdf_lines(text):
        match = row_pattern.match(line)
        if not match:
            continue

        thickness_in = float(match.group("thickness_in"))
        thickness_mm = thickness_in * 25.4
        construction, style, plies = normalize_nelco_construction(match.group("construction"))
        resin_pct = float(match.group("resin"))
        dk_map = {
            str(frequencies[0]): float(match.group("dk_2")),
            str(frequencies[1]): float(match.group("dk_10")),
        }
        df_map = {
            str(frequencies[0]): float(match.group("df_2")),
            str(frequencies[1]): float(match.group("df_10")),
        }
        max_freq = max(frequencies)
        records.append(
            MaterialRecord(
                id=make_id(source_pdf, "core", family, construction, thickness_mm, None),
                manufacturer=manufacturer,
                series=series,
                family=family,
                material_type="core",
                variant=family,
                source_pdf=source_pdf,
                construction=construction,
                resin_content_pct=resin_pct,
                thickness_mm=thickness_mm,
                thickness_in=thickness_in,
                thickness_um=thickness_mm * 1000,
                classification=None,
                style=style,
                plies=plies,
                dk_by_freq_ghz=dk_map,
                df_by_freq_ghz=df_map,
                reference_freq_ghz=max_freq,
                reference_dk=dk_map[str(max_freq)],
                reference_df=df_map[str(max_freq)],
                max_freq_ghz=max_freq,
                notes="core row parsed from local Nelco dielectric properties table.",
            )
        )
    return records


def parse_nelco_prepreg_rows(
    text: str,
    source_pdf: str,
    manufacturer: str,
    series: str,
    family: str,
    frequencies: list[float],
) -> list[MaterialRecord]:
    row_pattern = re.compile(
        r"^(?P<style>\d+)\s+(?P<resin>\d+(?:\.\d+)?)\s+"
        r"(?P<dk_2>\d+\.\d+)\s+(?P<df_2>\d+\.\d+)\s+"
        r"(?P<dk_10>\d+\.\d+)\s+(?P<df_10>\d+\.\d+)\s+"
        r"(?P<thickness_in>0\.\d+)"
    )

    records = []
    for line in pdf_lines(text):
        match = row_pattern.match(line)
        if not match:
            continue

        thickness_in = float(match.group("thickness_in"))
        thickness_mm = thickness_in * 25.4
        construction = match.group("style")
        resin_pct = float(match.group("resin"))
        dk_map = {
            str(frequencies[0]): float(match.group("dk_2")),
            str(frequencies[1]): float(match.group("dk_10")),
        }
        df_map = {
            str(frequencies[0]): float(match.group("df_2")),
            str(frequencies[1]): float(match.group("df_10")),
        }
        max_freq = max(frequencies)
        records.append(
            MaterialRecord(
                id=make_id(source_pdf, "prepreg", family, construction, thickness_mm, None),
                manufacturer=manufacturer,
                series=series,
                family=family,
                material_type="prepreg",
                variant=family,
                source_pdf=source_pdf,
                construction=construction,
                resin_content_pct=resin_pct,
                thickness_mm=thickness_mm,
                thickness_in=thickness_in,
                thickness_um=thickness_mm * 1000,
                classification=None,
                style=construction,
                plies=None,
                dk_by_freq_ghz=dk_map,
                df_by_freq_ghz=df_map,
                reference_freq_ghz=max_freq,
                reference_dk=dk_map[str(max_freq)],
                reference_df=df_map[str(max_freq)],
                max_freq_ghz=max_freq,
                notes="prepreg row parsed from local Nelco dielectric properties table.",
            )
        )
    return records


def parse_shengyi_table_pdf(pdf_path: Path, source: dict[str, str]) -> list[MaterialRecord]:
    manufacturer = source["manufacturer"]
    series = source["series"]
    family = source["family"]
    variant = source.get("variant", family)
    reader = PdfReader(str(pdf_path))
    frequencies = [1.0, 3.0, 5.0, 10.0]

    core_lines: list[str] = []
    prepreg_lines: list[str] = []
    section: str | None = None

    for page in reader.pages:
        text = (page.extract_text() or "").replace("\x00", " ")
        for line in pdf_lines(text):
            upper = line.upper()
            if "1. CORE" in upper:
                section = "core"
                continue
            if "2. PREPREG" in upper:
                section = "prepreg"
                continue
            if "3. REMARK" in upper:
                section = None
                continue

            if section == "core":
                core_lines.append(line)
            elif section == "prepreg":
                prepreg_lines.append(line)

    records = []
    records.extend(
        parse_shengyi_core_rows(core_lines, pdf_path.name, manufacturer, series, family, variant, frequencies)
    )
    records.extend(
        parse_shengyi_prepreg_rows(prepreg_lines, pdf_path.name, manufacturer, series, family, variant, frequencies)
    )
    return records


def parse_shengyi_core_rows(
    lines: list[str],
    source_pdf: str,
    manufacturer: str,
    series: str,
    family: str,
    variant: str,
    frequencies: list[float],
) -> list[MaterialRecord]:
    records = []
    for line in lines:
        tokens = repair_numeric_token_splits(line.split())
        if len(tokens) != 12:
            continue
        try:
            thickness_mm = float(tokens[0])
            construction = tokens[2].replace("X", "x")
            resin_pct = float(tokens[3])
            values = [float(token) for token in tokens[4:]]
        except ValueError:
            continue
        if not re.fullmatch(r"[0-9A-Za-zx+/]+", construction):
            continue

        dk_map, df_map = build_grouped_frequency_maps(frequencies, values)
        max_freq = max(frequencies)
        records.append(
            MaterialRecord(
                id=make_id(source_pdf, "core", family, construction, thickness_mm, variant),
                manufacturer=manufacturer,
                series=series,
                family=family,
                material_type="core",
                variant=variant,
                source_pdf=source_pdf,
                construction=construction,
                resin_content_pct=resin_pct,
                thickness_mm=thickness_mm,
                thickness_in=thickness_mm / 25.4,
                thickness_um=thickness_mm * 1000,
                classification=None,
                style=construction,
                plies=parse_ply_count(construction),
                dk_by_freq_ghz=dk_map,
                df_by_freq_ghz=df_map,
                reference_freq_ghz=max_freq,
                reference_dk=dk_map[str(max_freq)],
                reference_df=df_map[str(max_freq)],
                max_freq_ghz=max_freq,
                notes="core row parsed from local Shengyi based material line up.",
            )
        )
    return records


def parse_shengyi_prepreg_rows(
    lines: list[str],
    source_pdf: str,
    manufacturer: str,
    series: str,
    family: str,
    variant: str,
    frequencies: list[float],
) -> list[MaterialRecord]:
    records = []
    for line in lines:
        tokens = repair_numeric_token_splits(line.split())
        if len(tokens) != 12:
            continue
        try:
            construction = tokens[0].replace("X", "x")
            resin_pct = float(tokens[1])
            thickness_mm = float(tokens[2])
            values = [float(token) for token in tokens[4:]]
        except ValueError:
            continue
        if not re.fullmatch(r"[0-9A-Za-zx+/]+", construction):
            continue

        dk_map, df_map = build_grouped_frequency_maps(frequencies, values)
        max_freq = max(frequencies)
        records.append(
            MaterialRecord(
                id=make_id(source_pdf, "prepreg", family, construction, thickness_mm, variant),
                manufacturer=manufacturer,
                series=series,
                family=family,
                material_type="prepreg",
                variant=variant,
                source_pdf=source_pdf,
                construction=construction,
                resin_content_pct=resin_pct,
                thickness_mm=thickness_mm,
                thickness_in=thickness_mm / 25.4,
                thickness_um=thickness_mm * 1000,
                classification=None,
                style=construction,
                plies=None,
                dk_by_freq_ghz=dk_map,
                df_by_freq_ghz=df_map,
                reference_freq_ghz=max_freq,
                reference_dk=dk_map[str(max_freq)],
                reference_df=df_map[str(max_freq)],
                max_freq_ghz=max_freq,
                notes="prepreg row parsed from local Shengyi based material line up.",
            )
        )
    return records


def parse_megtron_title(title: str) -> tuple[str, str, str] | None:
    match = re.search(r"Dielectric Properties\s*/\s*(Laminate|Prepreg)\s+([^:]+)\s*:\s*(.+)", title)
    if not match:
        return None
    material_type = "core" if match.group(1) == "Laminate" else "prepreg"
    family = normalize_megtron_family(match.group(2).strip())
    variant = match.group(3).strip()
    return material_type, family, variant


def parse_megtron_spec_header(text: str) -> tuple[str, str, str, str] | None:
    match = re.search(r"Specification\s*/\s*(Laminate|Prepreg)\s+([A-Za-z0-9\-\(\)]+)", text)
    if not match:
        return None
    material_type = "core" if match.group(1) == "Laminate" else "prepreg"
    raw_family = match.group(2).strip()
    family = normalize_megtron_family(raw_family)
    if "Typical Dk" in text:
        prop = "dk"
    elif "Typical Df" in text:
        prop = "df"
    else:
        return None
    return material_type, family, raw_family, prop


def parse_megtron_core_rows(text: str) -> list[dict[str, object]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    freq_line = next((line for line in lines if line.startswith("mil mm ")), "")
    if not freq_line:
        raise ValueError("Could not find frequency header for Megtron core table.")
    frequencies = parse_frequency_tokens(freq_line)
    start = lines.index(freq_line) + 1
    rows = []

    for line in lines[start:]:
        if line.startswith("ply") or line.startswith("Core") or line.startswith("Cloth") or line.startswith("Dielectric"):
            continue
        match = re.match(
            r"(?P<core_type>\d+(?:\.\d+)?)\s+"
            r"(?P<thickness_in>\d+(?:\.\d+)?)\s+"
            r"(?P<thickness_mm>\d+\.\d+)\s+"
            r"(?P<style>\d+)\s+"
            r"(?P<plies>\d+)\s+"
            r"(?P<resin>\d+)\s+"
            r"(?P<values>.+)",
            line,
        )
        if not match:
            continue
        values = [float(token) for token in match.group("values").split()]
        if len(values) != len(frequencies):
            continue
        thickness_mm = float(match.group("thickness_mm"))
        style = match.group("style")
        plies = int(match.group("plies"))
        resin = float(match.group("resin"))
        construction = f"{style} x {plies} ply"
        key = f"{style}|{plies}|{thickness_mm:.3f}|{resin:.1f}"
        rows.append(
            {
                "key": key,
                "construction": construction,
                "style": style,
                "plies": plies,
                "resin_content_pct": resin,
                "thickness_mm": thickness_mm,
                "thickness_in": float(match.group("thickness_in")),
                "frequencies": frequencies,
                "values": values,
            }
        )
    return rows


def parse_megtron_prepreg_rows(text: str) -> list[dict[str, object]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    freq_line = next((line for line in lines if len(parse_frequency_tokens(line)) >= 2), "")
    if not freq_line:
        raise ValueError("Could not find frequency header for Megtron prepreg table.")
    frequencies = parse_frequency_tokens(freq_line)
    start = lines.index(freq_line) + 1
    rows = []
    current_style: str | None = None

    for line in lines[start:]:
        if line.startswith("*") or line.startswith("14-") or line.startswith("1GHz") or line.startswith("Dielectric"):
            continue
        tokens = line.split()
        if not tokens:
            continue
        if len(tokens) == 1 and tokens[0].isdigit() and len(tokens[0]) >= 4:
            current_style = tokens[0]
            continue
        if len(tokens) < 2:
            continue

        if len(tokens[0]) >= 4 and tokens[0].isdigit():
            if len(tokens) < 3:
                continue
            style = tokens[0]
            resin_token = tokens[1]
            thickness_token = tokens[2]
            value_tokens = tokens[3:]
            current_style = style
        else:
            if current_style is None:
                continue
            style = current_style
            resin_token = tokens[0]
            thickness_token = tokens[1]
            value_tokens = tokens[2:]

        if len(value_tokens) != len(frequencies):
            continue

        resin = float(clean_number(resin_token))
        thickness_um = float(clean_number(thickness_token))
        thickness_mm = thickness_um / 1000
        values = [float(token) for token in value_tokens]
        construction = style
        key = f"{style}|{thickness_mm:.3f}|{resin:.1f}"
        rows.append(
            {
                "key": key,
                "construction": construction,
                "style": style,
                "plies": None,
                "resin_content_pct": resin,
                "thickness_mm": thickness_mm,
                "thickness_in": thickness_mm / 25.4,
                "frequencies": frequencies,
                "values": values,
            }
        )
    return rows


def parse_megtron_prepreg_spec_rows(text: str, pdf_page) -> list[dict[str, object]]:
    text_rows = parse_megtron_prepreg_rows(text)
    style_sequence = parse_megtron_prepreg_style_sequence_from_pdfplumber(pdf_page)
    if style_sequence:
        text_rows = apply_megtron_prepreg_style_sequence(text_rows, style_sequence)
    table_rows = parse_megtron_prepreg_rows_from_pdfplumber(pdf_page)
    return table_rows if len(table_rows) > len(text_rows) else text_rows


def parse_megtron_prepreg_rows_from_pdfplumber(pdf_page) -> list[dict[str, object]]:
    best_table = None
    for table in pdf_page.extract_tables():
        if not table:
            continue
        if max((len(row) for row in table if row), default=0) < 5:
            continue
        joined = " ".join(" ".join(cell or "" for cell in row) for row in table)
        if "Cloth" in joined and "GHz" in joined and ("Typical Dk" in joined or "Typical Df" in joined):
            best_table = table
            break
    if best_table is None:
        return []

    freq_row_index = None
    frequencies: list[float] = []
    for index, row in enumerate(best_table[:4]):
        row_freqs = parse_frequency_tokens(" ".join(cell or "" for cell in row))
        if len(row_freqs) >= 2:
            freq_row_index = index
            frequencies = row_freqs
            break
    if freq_row_index is None:
        return []

    rows = []
    current_style: str | None = None
    for row in best_table[freq_row_index + 1 :]:
        cells = [(cell or "").strip() for cell in row]
        if len(cells) < 3 + len(frequencies):
            continue
        resin_text = clean_number(cells[1])
        thickness_text = clean_number(cells[2])
        if not resin_text or not thickness_text:
            continue
        style_text = clean_number(cells[0])
        if style_text:
            current_style = style_text
        if current_style is None:
            continue

        values: list[float] = []
        for cell in cells[3 : 3 + len(frequencies)]:
            value_text = clean_number(cell)
            if not value_text:
                values = []
                break
            values.append(float(value_text))
        if len(values) != len(frequencies):
            continue

        resin = float(resin_text)
        thickness_um = float(thickness_text)
        thickness_mm = thickness_um / 1000
        construction = current_style
        key = f"{construction}|{thickness_mm:.3f}|{resin:.1f}"
        rows.append(
            {
                "key": key,
                "construction": construction,
                "style": construction,
                "plies": None,
                "resin_content_pct": resin,
                "thickness_mm": thickness_mm,
                "thickness_in": thickness_mm / 25.4,
                "frequencies": frequencies,
                "values": values,
            }
        )
    return rows


def parse_megtron_prepreg_style_sequence_from_pdfplumber(pdf_page) -> list[str]:
    known_styles = {"1027", "1035", "1067", "1078", "1080", "2013", "2116", "3313"}
    block_lines: list[str] = []
    for table in pdf_page.extract_tables():
        for row in table:
            for cell in row:
                if cell:
                    block_lines.extend(line.strip() for line in cell.splitlines() if line.strip())

    styles: list[str] = []
    current_style: str | None = None
    for line in block_lines:
        if (
            "Specification" in line
            or "Typical" in line
            or "Cloth" in line
            or "Resin" in line
            or "Thickness" in line
            or "Style" in line
            or line.startswith("*")
        ):
            continue
        compressed = re.sub(r"\s+", "", line)
        if re.fullmatch(r"\d{4}", compressed):
            current_style = compressed
            continue
        if "." not in compressed:
            continue
        prefix = compressed.split(".", 1)[0]
        if len(prefix) < 4:
            continue
        digits_before_first_value = prefix[:-1]
        if len(digits_before_first_value) >= 8:
            candidate_style = digits_before_first_value[:4]
            if candidate_style in known_styles:
                current_style = candidate_style
        if current_style is None:
            continue
        styles.append(current_style)
    return styles


def apply_megtron_prepreg_style_sequence(
    rows: list[dict[str, object]],
    styles: list[str],
) -> list[dict[str, object]]:
    if len(rows) != len(styles):
        return rows
    updated_rows = []
    for row, style in zip(rows, styles):
        updated = dict(row)
        updated["construction"] = style
        updated["style"] = style
        updated["key"] = f"{style}|{updated['thickness_mm']:.3f}|{updated['resin_content_pct']:.1f}"
        updated_rows.append(updated)
    return updated_rows


def pdf_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def repair_numeric_token_splits(tokens: list[str]) -> list[str]:
    merged = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if index + 1 < len(tokens):
            next_token = tokens[index + 1]
            if token.isdigit() and next_token.startswith("."):
                merged.append(token + next_token)
                index += 2
                continue
            decimal_match = re.fullmatch(r"(\d+)\.(\d*)", token)
            if (
                decimal_match
                and next_token.isdigit()
                and len(decimal_match.group(2)) < 4
                and len(next_token) <= 3
                and float(token) < 1
            ):
                merged.append(token + next_token)
                index += 2
                continue
        merged.append(token)
        index += 1
    return merged


def parse_frequency_tokens(text: str) -> list[float]:
    frequencies = []
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(MHz|GHz)", text, flags=re.IGNORECASE):
        freq = float(value)
        if unit.lower() == "mhz":
            freq = freq / 1000
        if freq not in frequencies:
            frequencies.append(freq)
    return frequencies


def build_frequency_maps(frequencies: list[float], numeric_values: list[str]) -> tuple[dict[str, float], dict[str, float]]:
    dk_map: dict[str, float] = {}
    df_map: dict[str, float] = {}
    for index, freq in enumerate(frequencies):
        dk_map[str(freq)] = float(numeric_values[index * 2])
        df_map[str(freq)] = float(numeric_values[(index * 2) + 1])
    return dk_map, df_map


def build_grouped_frequency_maps(frequencies: list[float], numeric_values: list[float]) -> tuple[dict[str, float], dict[str, float]]:
    split = len(frequencies)
    dk_map = {str(freq): numeric_values[index] for index, freq in enumerate(frequencies)}
    df_map = {str(freq): numeric_values[index + split] for index, freq in enumerate(frequencies)}
    return dk_map, df_map


def parse_tuc_construction(construction: str) -> tuple[str, int | None]:
    match = re.fullmatch(r"([A-Za-z0-9]+)[xX](\d+)", construction)
    if not match:
        return construction, None
    return match.group(1), int(match.group(2))


def normalize_nelco_construction(construction: str) -> tuple[str, str, int | None]:
    segments = []
    style_parts = []
    total_plies = 0
    tokens = construction.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "+":
            index += 1
            continue
        if index + 1 < len(tokens) and token.isdigit():
            count = int(token)
            style = tokens[index + 1]
            segments.append(f"{count}x{style}")
            style_parts.append(style)
            total_plies += count
            index += 2
            continue
        segments.append(token)
        style_parts.append(token)
        index += 1

    normalized = " + ".join(segments)
    style = " + ".join(style_parts)
    return normalized, style, total_plies or None


def parse_ply_count(construction: str) -> int | None:
    values = [int(value) for value in re.findall(r"(\d+)x", construction)]
    if not values:
        return None
    return sum(values)


def clean_number(token: str) -> str:
    return re.sub(r"[^0-9.]+", "", token)


def normalize_megtron_family(family: str) -> str:
    return MEGTRON_FAMILY_LABELS.get(family.strip(), family.strip())


def make_id(source_pdf: str, material_type: str, family: str, construction: str, thickness_mm: float, variant: str | None) -> str:
    raw = f"{source_pdf}-{material_type}-{family}-{construction}-{thickness_mm:.3f}-{variant or ''}"
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")


if __name__ == "__main__":
    main()
