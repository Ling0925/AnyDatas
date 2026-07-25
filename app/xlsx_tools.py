from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from .quality_tools import build_quality_summary


NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg_rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
XLSX_MAX_ROWS = 1_048_576
XLSX_MAX_COLUMNS = 16_384


def read_text(zip_file: zipfile.ZipFile, name: str) -> str:
    return zip_file.read(name).decode("utf-8")


def cell_index(cell_reference: str) -> int:
    letters = "".join(re.findall(r"[A-Z]+", cell_reference.upper()))
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return max(index - 1, 0)


def sheet_target(zip_file: zipfile.ZipFile) -> tuple[str, str]:
    workbook = ElementTree.fromstring(read_text(zip_file, "xl/workbook.xml"))
    first_sheet = workbook.find("main:sheets/main:sheet", NS)
    if first_sheet is None:
        raise ValueError("Workbook has no sheets")
    sheet_name = first_sheet.attrib.get("name", "Sheet1")
    relationship_id = first_sheet.attrib.get(f"{{{NS['rel']}}}id")
    if not relationship_id:
        return "xl/worksheets/sheet1.xml", sheet_name

    rels = ElementTree.fromstring(read_text(zip_file, "xl/_rels/workbook.xml.rels"))
    for relationship in rels.findall("pkg_rel:Relationship", NS):
        if relationship.attrib.get("Id") == relationship_id:
            target = relationship.attrib["Target"]
            if target.startswith("/"):
                return target.lstrip("/"), sheet_name
            return f"xl/{target}", sheet_name
    raise ValueError("Workbook sheet relationship is missing")


def shared_strings(zip_file: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zip_file.namelist():
        return []
    root = ElementTree.fromstring(read_text(zip_file, "xl/sharedStrings.xml"))
    values = []
    for item in root.findall("main:si", NS):
        text_parts = [node.text or "" for node in item.findall(".//main:t", NS)]
        values.append("".join(text_parts))
    return values


def parse_cell(cell, strings: list[str]) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        parts = [node.text or "" for node in cell.findall(".//main:t", NS)]
        return "".join(parts)
    value = cell.find("main:v", NS)
    if value is None or value.text is None:
        return ""
    raw_value = value.text
    if cell_type == "s":
        return strings[int(raw_value)] if raw_value.isdigit() and int(raw_value) < len(strings) else ""
    if cell_type == "b":
        return raw_value == "1"
    try:
        numeric = float(raw_value)
    except ValueError:
        return raw_value
    return int(numeric) if numeric.is_integer() else numeric


def normalize_header(value: Any, index: int, seen: set[str]) -> str:
    name = str(value).strip() if value is not None else ""
    if not name:
        name = f"column_{index + 1}"
    original = name
    suffix = 2
    while name in seen:
        name = f"{original}_{suffix}"
        suffix += 1
    seen.add(name)
    return name


def read_xlsx_rows(path: Path) -> tuple[list[str], list[dict[str, Any]], str]:
    with zipfile.ZipFile(path) as zip_file:
        target, sheet_name = sheet_target(zip_file)
        strings = shared_strings(zip_file)
        root = ElementTree.fromstring(read_text(zip_file, target))
        raw_rows: list[list[Any]] = []
        for row in root.findall(".//main:sheetData/main:row", NS):
            values: list[Any] = []
            for cell in row.findall("main:c", NS):
                reference = cell.attrib.get("r", "")
                index = cell_index(reference) if reference else len(values)
                while len(values) <= index:
                    values.append("")
                values[index] = parse_cell(cell, strings)
            raw_rows.append(values)

    while raw_rows and all(value == "" for value in raw_rows[0]):
        raw_rows.pop(0)
    if not raw_rows:
        return [], [], sheet_name

    header_values = raw_rows[0]
    seen: set[str] = set()
    columns = [normalize_header(value, index, seen) for index, value in enumerate(header_values)]
    rows = []
    for raw_row in raw_rows[1:]:
        row = {}
        for index, column in enumerate(columns):
            row[column] = raw_row[index] if index < len(raw_row) else ""
        rows.append(row)
    return columns, rows, sheet_name


def write_rows_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def column_letter(index: int) -> str:
    value = index + 1
    letters = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def xml_text(value: Any) -> str:
    text = str(value)
    return "".join(
        character
        for character in text
        if character in "\x09\x0A\x0D"
        or "\x20" <= character <= "\uD7FF"
        or "\uE000" <= character <= "\uFFFD"
        or "\U00010000" <= character <= "\U0010FFFF"
    )


def xlsx_cell(reference: str, value: Any, style: int = 0) -> str:
    style_attribute = f' s="{style}"' if style else ""
    if value is None:
        return f'<c r="{reference}"{style_attribute}/>'
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"{style_attribute}><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(float(value)):
                return f'<c r="{reference}" t="n"{style_attribute}><v>{value}</v></c>'
        except OverflowError:
            pass
    text = escape(xml_text(value))
    return f'<c r="{reference}" t="inlineStr"{style_attribute}><is><t xml:space="preserve">{text}</t></is></c>'


def render_xlsx(columns: list[Any], rows: list[list[Any]], sheet_name: str = "Report") -> bytes:
    if not columns:
        raise ValueError("XLSX export requires at least one column.")
    if len(columns) > XLSX_MAX_COLUMNS:
        raise ValueError(f"XLSX export supports at most {XLSX_MAX_COLUMNS} columns.")
    if len(rows) + 1 > XLSX_MAX_ROWS:
        raise ValueError(f"XLSX export supports at most {XLSX_MAX_ROWS - 1} data rows.")
    cleaned_sheet_name = "".join("_" if character in "[]:*?/\\" else character for character in xml_text(sheet_name))
    safe_sheet_name = cleaned_sheet_name.strip().strip("'")[:31] or "Report"
    escaped_sheet_name = escape(safe_sheet_name, {'"': "&quot;"})
    sheet_rows = []
    header_cells = [xlsx_cell(f"{column_letter(index)}1", column, style=1) for index, column in enumerate(columns)]
    sheet_rows.append(f'<row r="1">{"".join(header_cells)}</row>')
    for row_index, row in enumerate(rows, start=2):
        cells = [
            xlsx_cell(f"{column_letter(column_index)}{row_index}", row[column_index] if column_index < len(row) else None)
            for column_index in range(len(columns))
        ]
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    last_cell = f"{column_letter(len(columns) - 1)}{max(len(rows) + 1, 1)}"
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_cell}"/>'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        f'<autoFilter ref="A1:{column_letter(len(columns) - 1)}{max(len(rows) + 1, 1)}"/>'
        '</worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escaped_sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
        '<cellXfs count="2"><xf fontId="0" fillId="0" borderId="0" xfId="0"/><xf fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
        '</styleSheet>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as workbook_zip:
        workbook_zip.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        )
        workbook_zip.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        )
        workbook_zip.writestr("xl/workbook.xml", workbook)
        workbook_zip.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        )
        workbook_zip.writestr("xl/worksheets/sheet1.xml", worksheet)
        workbook_zip.writestr("xl/styles.xml", styles)
    return output.getvalue()


def inspect_xlsx(path: Path, preview_limit: int = 50) -> tuple[list[str], list[dict[str, Any]], int, dict[str, Any], str, list[dict[str, Any]]]:
    if not path.exists():
        raise FileNotFoundError(f"XLSX file not found: {path}")
    columns, rows, sheet_name = read_xlsx_rows(path)
    preview = rows[:preview_limit]
    return columns, preview, len(rows), build_quality_summary(columns, rows), sheet_name, rows
