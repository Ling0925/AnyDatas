from __future__ import annotations

from io import BytesIO

from PIL import Image

from app.report_export import PNG_ROW_LIMIT, normalized_table, render_report_pdf, render_report_png


def sample_result(row_count: int = 3):
    return {
        "columns": ["区域", "收入", "说明"],
        "rows": [[f"华东 {index}", index * 100, "月度数据"] for index in range(row_count)],
    }


def test_visual_report_exports_render_valid_png_and_pdf_with_cjk_text():
    result = sample_result()

    png = render_report_png("月度经营报表", "最近成功快照", "2026-07-11 10:00:00", result)
    pdf = render_report_pdf("月度经营报表", "最近成功快照", "2026-07-11 10:00:00", result)

    with Image.open(BytesIO(png)) as image:
        assert image.format == "PNG"
        assert image.width >= 900
        assert image.height > 200
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")


def test_visual_report_table_reports_omitted_rows_at_the_resource_limit():
    columns, rows, omitted = normalized_table(sample_result(PNG_ROW_LIMIT + 7), PNG_ROW_LIMIT)

    assert columns == ["区域", "收入", "说明"]
    assert len(rows) == PNG_ROW_LIMIT
    assert omitted == 7
