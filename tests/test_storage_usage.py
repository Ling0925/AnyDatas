from pathlib import Path

from app import storage_usage


def test_workspace_storage_counts_managed_unique_source_files(monkeypatch, tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    original = upload_dir / "book.xlsx"
    dataset = upload_dir / "book.csv"
    outside = tmp_path / "outside.csv"
    original.write_bytes(b"workbook")
    dataset.write_bytes(b"rows")
    outside.write_bytes(b"ignored")
    monkeypatch.setattr(storage_usage, "UPLOAD_DIR", upload_dir)

    rows = [
        {
            "id": "xlsx-source",
            "path": str(dataset),
            "connection_json": f'{{"original_path": "{original}"}}',
        },
        {
            "id": "duplicate-source",
            "path": str(dataset),
            "connection_json": "{}",
        },
        {
            "id": "outside-source",
            "path": str(outside),
            "connection_json": "{}",
        },
    ]

    class FakeConnection:
        def execute(self, _query, _params):
            return self

        def fetchall(self):
            return rows

    assert storage_usage.workspace_storage_bytes(FakeConnection(), "workspace") == len(b"workbookrows")
    assert storage_usage.workspace_storage_bytes(
        FakeConnection(), "workspace", exclude_source_id="xlsx-source"
    ) == len(b"rows")


def test_storage_capacity_accepts_exact_limit_and_rejects_overage():
    storage_usage.ensure_workspace_storage_capacity(8, 2, 10)

    try:
        storage_usage.ensure_workspace_storage_capacity(8, 3, 10)
    except ValueError as exc:
        assert "11 bytes would exceed 10 bytes" in str(exc)
    else:
        raise AssertionError("Expected storage overage to be rejected")
