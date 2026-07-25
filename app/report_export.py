from __future__ import annotations

import html
import io
import math
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFError
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


PNG_ROW_LIMIT = 100
PDF_ROW_LIMIT = 500
FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def display_value(value: Any, limit: int = 240) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        value = str(value)
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def normalized_table(result: dict[str, Any], row_limit: int) -> tuple[list[str], list[list[str]], int]:
    columns = [display_value(column, 120) for column in result.get("columns", [])]
    raw_rows = result.get("rows", [])
    rows = []
    for raw_row in raw_rows[:row_limit]:
        values = list(raw_row) if isinstance(raw_row, (list, tuple)) else [raw_row]
        rows.append([display_value(values[index]) if index < len(values) else "" for index in range(len(columns))])
    return columns, rows, max(len(raw_rows) - len(rows), 0)


def load_png_font(size: int):
    configured = os.getenv("ANYDATAS_EXPORT_FONT_PATH", "")
    candidates = ([configured] if configured else []) + list(FONT_CANDIDATES)
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            return ImageFont.truetype(candidate, size=size, index=0)
        except OSError:
            continue
    return ImageFont.load_default()


def register_pdf_font() -> str:
    font_name = "AnyDatasExport"
    if font_name in pdfmetrics.getRegisteredFontNames():
        return font_name
    configured = os.getenv("ANYDATAS_EXPORT_FONT_PATH", "")
    candidates = ([configured] if configured else []) + list(FONT_CANDIDATES)
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont(font_name, candidate, subfontIndex=0))
            return font_name
        except (OSError, TTFError):
            continue
    return "Helvetica"


def fit_png_text(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    if draw.textbbox((0, 0), text, font=font)[2] <= width:
        return text
    suffix = "..."
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = f"{text[:middle]}{suffix}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= width:
            low = middle
        else:
            high = middle - 1
    return f"{text[:low]}{suffix}"


def render_report_png(title: str, description: str, snapshot_created_at: str, result: dict[str, Any]) -> bytes:
    columns, rows, omitted = normalized_table(result, PNG_ROW_LIMIT)
    width = min(max(900, len(columns) * 150), 2400)
    margin = 32
    title_height = 112
    row_height = 34
    footer_height = 48
    height = title_height + row_height * (len(rows) + 1) + footer_height
    image = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(image)
    title_font = load_png_font(24)
    body_font = load_png_font(13)
    header_font = load_png_font(13)
    small_font = load_png_font(11)

    rendered_title = fit_png_text(draw, display_value(title, 160), title_font, width - margin * 2)
    draw.text((margin, 20), rendered_title, fill="#111827", font=title_font)
    subtitle = display_value(description, 220) or "Report snapshot"
    draw.text((margin, 55), fit_png_text(draw, subtitle, body_font, width - margin * 2), fill="#475569", font=body_font)
    draw.text((margin, 79), f"Snapshot: {snapshot_created_at}", fill="#64748b", font=small_font)

    table_left = margin
    table_width = width - margin * 2
    column_width = table_width / max(len(columns), 1)
    table_top = title_height
    draw.rectangle((table_left, table_top, table_left + table_width, table_top + row_height), fill="#1f2937")
    for column_index, column in enumerate(columns):
        x = int(table_left + column_index * column_width)
        draw.text(
            (x + 8, table_top + 8),
            fit_png_text(draw, column, header_font, max(int(column_width) - 16, 10)),
            fill="#ffffff",
            font=header_font,
        )
    for row_index, row in enumerate(rows):
        y = table_top + row_height * (row_index + 1)
        fill = "#ffffff" if row_index % 2 == 0 else "#eef2f7"
        draw.rectangle((table_left, y, table_left + table_width, y + row_height), fill=fill)
        for column_index, value in enumerate(row):
            x = int(table_left + column_index * column_width)
            draw.text(
                (x + 8, y + 8),
                fit_png_text(draw, value, body_font, max(int(column_width) - 16, 10)),
                fill="#1f2937",
                font=body_font,
            )
    footer = f"{len(rows)} rows shown"
    if omitted:
        footer += f"; {omitted} omitted. Download CSV or JSON for complete data."
    draw.text((margin, height - 30), fit_png_text(draw, footer, small_font, width - margin * 2), fill="#64748b", font=small_font)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_report_pdf(title: str, description: str, snapshot_created_at: str, result: dict[str, Any]) -> bytes:
    columns, rows, omitted = normalized_table(result, PDF_ROW_LIMIT)
    output = io.BytesIO()
    font_name = register_pdf_font()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=display_value(title, 160),
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "AnyDatasTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=18,
        leading=22,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#111827"),
        spaceAfter=4 * mm,
    )
    body_style = ParagraphStyle(
        "AnyDatasBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#334155"),
    )
    cell_style = ParagraphStyle(
        "AnyDatasCell",
        parent=body_style,
        fontSize=max(5.5, 8 - max(len(columns) - 8, 0) * 0.2),
        leading=8,
    )
    header_style = ParagraphStyle(
        "AnyDatasHeader",
        parent=cell_style,
        textColor=colors.white,
    )
    story = [
        Paragraph(html.escape(display_value(title, 160)), title_style),
        Paragraph(html.escape(display_value(description, 300) or "Report snapshot"), body_style),
        Paragraph(html.escape(f"Snapshot: {snapshot_created_at}"), body_style),
        Spacer(1, 5 * mm),
    ]
    table_data = [
        [Paragraph(f"<b>{html.escape(column)}</b>", header_style) for column in columns]
    ] + [[Paragraph(html.escape(value), cell_style) for value in row] for row in rows]
    available_width = landscape(A4)[0] - 24 * mm
    table = Table(table_data, colWidths=[available_width / max(len(columns), 1)] * max(len(columns), 1), repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("BACKGROUND", (0, 1), (-1, -1), colors.white),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f7")]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)
    footer = f"{len(rows)} rows shown"
    if omitted:
        footer += f"; {omitted} omitted. Download CSV or JSON for complete data."
    story.extend([Spacer(1, 4 * mm), Paragraph(html.escape(footer), body_style)])
    document.build(story)
    return output.getvalue()
