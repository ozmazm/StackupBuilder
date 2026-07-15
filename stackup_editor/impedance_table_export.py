from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZipInfo

WORKSHEET_PATH = "xl/worksheets/sheet1.xml"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
X14AC_NS = "http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"
XR_NS = "http://schemas.microsoft.com/office/spreadsheetml/2014/revision"
XR2_NS = "http://schemas.microsoft.com/office/spreadsheetml/2015/revision2"
XR3_NS = "http://schemas.microsoft.com/office/spreadsheetml/2016/revision3"

ET.register_namespace("", MAIN_NS)
ET.register_namespace("r", R_NS)
ET.register_namespace("mc", MC_NS)
ET.register_namespace("x14ac", X14AC_NS)
ET.register_namespace("xr", XR_NS)
ET.register_namespace("xr2", XR2_NS)
ET.register_namespace("xr3", XR3_NS)

HEADER_TITLES = (
    "TL Type",
    "Trace layer",
    "Trace Width",
    "Trace Gap",
    "Reference Above",
    "Reference Below",
    "Calculated Impedance",
    "Target Impedance",
)


@dataclass(frozen=True)
class ImpedanceTableRow:
    tl_type: str
    trace_layer: str
    trace_width: str
    trace_gap: str
    reference_above: str
    reference_below: str
    calculated_impedance_ohm: float | None
    target_impedance_ohm: float | None


def export_impedance_table_xlsx(
    template_path: Path,
    output_path: Path,
    rows: Sequence[ImpedanceTableRow],
) -> None:
    if not template_path.exists():
        raise FileNotFoundError(f"Impedance table template was not found at {template_path}.")

    package_entries: list[tuple[ZipInfo, bytes]] = []
    with ZipFile(template_path, "r") as source_zip:
        for info in source_zip.infolist():
            data = source_zip.read(info.filename)
            if info.filename == WORKSHEET_PATH:
                data = _rewrite_worksheet(data, rows)
            package_entries.append((info, data))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output_path, "w") as target_zip:
        for source_info, data in package_entries:
            target_info = ZipInfo(source_info.filename, date_time=source_info.date_time)
            target_info.compress_type = source_info.compress_type
            target_info.comment = source_info.comment
            target_info.extra = source_info.extra
            target_info.create_system = source_info.create_system
            target_info.create_version = source_info.create_version
            target_info.extract_version = source_info.extract_version
            target_info.flag_bits = source_info.flag_bits
            target_info.volume = source_info.volume
            target_info.internal_attr = source_info.internal_attr
            target_info.external_attr = source_info.external_attr
            target_zip.writestr(target_info, data)


def _rewrite_worksheet(sheet_xml: bytes, rows: Sequence[ImpedanceTableRow]) -> bytes:
    root = ET.fromstring(sheet_xml)
    root.set(f"{{{MC_NS}}}Ignorable", "x14ac xr")
    sheet_data = root.find(f"{{{MAIN_NS}}}sheetData")
    if sheet_data is None:
        raise ValueError("The impedance table template is missing worksheet data.")

    sheet_data.clear()
    sheet_data.append(_build_header_row())
    for row_number, row in enumerate(rows, start=2):
        sheet_data.append(_build_data_row(row_number, row))

    dimension = root.find(f"{{{MAIN_NS}}}dimension")
    if dimension is not None:
        last_row = max(1, len(rows) + 1)
        dimension.set("ref", f"A1:H{last_row}")

    selection = root.find(f".//{{{MAIN_NS}}}selection")
    if selection is not None:
        active_row = 2 if rows else 1
        selection.set("activeCell", f"A{active_row}")
        selection.set("sqref", f"A{active_row}")

    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return xml_bytes.replace(b'mc:Ignorable="x14ac xr xr2 xr3"', b'mc:Ignorable="x14ac xr"')


def _build_header_row() -> ET.Element:
    row = _new_row(1)
    for column_number, title in enumerate(HEADER_TITLES, start=1):
        row.append(_inline_string_cell(column_number, 1, title))
    return row


def _build_data_row(row_number: int, row_data: ImpedanceTableRow) -> ET.Element:
    row = _new_row(row_number)
    string_values = (
        row_data.tl_type,
        row_data.trace_layer,
        row_data.trace_width,
        row_data.trace_gap,
        row_data.reference_above,
        row_data.reference_below,
    )
    for column_number, value in enumerate(string_values, start=1):
        if value:
            row.append(_inline_string_cell(column_number, row_number, value))

    if row_data.calculated_impedance_ohm is not None:
        row.append(_numeric_cell(7, row_number, row_data.calculated_impedance_ohm))
    if row_data.target_impedance_ohm is not None:
        row.append(_numeric_cell(8, row_number, row_data.target_impedance_ohm))
    return row


def _new_row(row_number: int) -> ET.Element:
    row = ET.Element(f"{{{MAIN_NS}}}row")
    row.set("r", str(row_number))
    row.set("spans", "1:8")
    row.set(f"{{{X14AC_NS}}}dyDescent", "0.3")
    return row


def _inline_string_cell(column_number: int, row_number: int, value: str) -> ET.Element:
    cell = ET.Element(f"{{{MAIN_NS}}}c")
    cell.set("r", f"{_column_name(column_number)}{row_number}")
    cell.set("t", "inlineStr")
    inline = ET.SubElement(cell, f"{{{MAIN_NS}}}is")
    text = ET.SubElement(inline, f"{{{MAIN_NS}}}t")
    text.text = value
    return cell


def _numeric_cell(column_number: int, row_number: int, value: float) -> ET.Element:
    cell = ET.Element(f"{{{MAIN_NS}}}c")
    cell.set("r", f"{_column_name(column_number)}{row_number}")
    value_node = ET.SubElement(cell, f"{{{MAIN_NS}}}v")
    value_node.text = _format_number(value)
    return cell


def _column_name(column_number: int) -> str:
    result = ""
    number = column_number
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _format_number(value: float) -> str:
    rounded = round(value, 2)
    text = f"{rounded:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
