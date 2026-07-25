from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .csv_tools import inspect_csv
from .db import UPLOAD_DIR
from .parquet_tools import inspect_parquet
from .s3_tools import download_s3_object
from .xlsx_tools import inspect_xlsx, write_rows_csv


@dataclass
class S3Snapshot:
    filename: str
    original_path: Path
    dataset_path: Path
    runtime_format: str
    columns: list[str]
    preview: list[dict[str, Any]]
    row_count: int
    quality: dict[str, Any]
    object_metadata: dict[str, Any]
    format_metadata: dict[str, Any]

    @property
    def size_bytes(self) -> int:
        paths = {self.original_path.resolve(), self.dataset_path.resolve()}
        return sum(path.stat().st_size for path in paths if path.is_file())

    def remove_files(self) -> None:
        self.original_path.unlink(missing_ok=True)
        if self.dataset_path != self.original_path:
            self.dataset_path.unlink(missing_ok=True)


def inspect_snapshot_file(
    source_id: str,
    filename: str,
    downloaded_path: Path,
    xlsx_csv_path: Optional[Path] = None,
) -> tuple[str, Path, list[str], list[dict[str, Any]], int, dict[str, Any], dict[str, Any]]:
    extension = Path(filename).suffix.lower()
    if extension == ".parquet":
        columns, preview, row_count, quality = inspect_parquet(downloaded_path)
        return "parquet", downloaded_path, columns, preview, row_count, quality, {}
    if extension == ".xlsx":
        columns, preview, row_count, quality, sheet_name, rows = inspect_xlsx(downloaded_path)
        dataset_path = xlsx_csv_path or UPLOAD_DIR / f"{source_id}_s3_{Path(filename).stem}.csv"
        write_rows_csv(dataset_path, columns, rows)
        return (
            "csv",
            dataset_path,
            columns,
            preview,
            row_count,
            quality,
            {"sheet": sheet_name, "original_path": str(downloaded_path)},
        )
    if extension == ".csv":
        columns, preview, row_count, quality = inspect_csv(downloaded_path)
        return "csv", downloaded_path, columns, preview, row_count, quality, {}
    raise ValueError("S3 imports currently accept CSV, XLSX, or Parquet objects.")


def import_s3_snapshot(
    source_id: str,
    secret_value: str,
    bucket: str,
    object_key: str,
    max_bytes: int,
) -> S3Snapshot:
    filename = Path(object_key).name
    extension = Path(filename).suffix.lower()
    if extension not in {".csv", ".xlsx", ".parquet"}:
        raise ValueError("S3 imports currently accept CSV, XLSX, or Parquet objects.")
    downloaded_path = UPLOAD_DIR / f"{source_id}_s3_{filename}"
    xlsx_csv_path = UPLOAD_DIR / f"{source_id}_s3_{Path(filename).stem}.csv" if extension == ".xlsx" else None
    try:
        object_metadata = download_s3_object(secret_value, bucket, object_key, downloaded_path, max_bytes)
        runtime_format, dataset_path, columns, preview, row_count, quality, format_metadata = inspect_snapshot_file(
            source_id,
            filename,
            downloaded_path,
            xlsx_csv_path,
        )
    except Exception:
        downloaded_path.unlink(missing_ok=True)
        if xlsx_csv_path is not None:
            xlsx_csv_path.unlink(missing_ok=True)
        raise
    return S3Snapshot(
        filename=filename,
        original_path=downloaded_path,
        dataset_path=dataset_path,
        runtime_format=runtime_format,
        columns=columns,
        preview=preview,
        row_count=row_count,
        quality=quality,
        object_metadata=object_metadata,
        format_metadata=format_metadata,
    )


def managed_snapshot_path(value: str) -> Path:
    path = Path(value).resolve()
    upload_root = UPLOAD_DIR.resolve()
    if path == upload_root or upload_root not in path.parents:
        raise ValueError("S3 snapshot path is outside the managed upload directory.")
    return path


def refresh_s3_snapshot(
    source: Any,
    connection: dict[str, Any],
    secret_value: str,
    max_bytes: int,
    max_snapshot_bytes: int,
) -> S3Snapshot:
    filename = source["filename"]
    extension = Path(filename).suffix.lower()
    temporary_id = uuid.uuid4().hex
    temporary_download = UPLOAD_DIR / f".{source['id']}_{temporary_id}{extension}"
    temporary_csv = UPLOAD_DIR / f".{source['id']}_{temporary_id}.csv" if extension == ".xlsx" else None
    try:
        dataset_path = managed_snapshot_path(source["path"])
        original_path = (
            managed_snapshot_path(connection.get("original_path", ""))
            if extension == ".xlsx"
            else dataset_path
        )
        object_metadata = download_s3_object(
            secret_value,
            connection["bucket"],
            connection["object_key"],
            temporary_download,
            max_bytes,
        )
        runtime_format, temporary_dataset, columns, preview, row_count, quality, format_metadata = inspect_snapshot_file(
            source["id"],
            filename,
            temporary_download,
            temporary_csv,
        )
        temporary_paths = {temporary_download.resolve(), temporary_dataset.resolve()}
        snapshot_bytes = sum(path.stat().st_size for path in temporary_paths if path.is_file())
        if snapshot_bytes > max_snapshot_bytes:
            raise ValueError(
                f"Workspace storage limit exceeded: refreshed snapshot needs {snapshot_bytes} bytes, {max_snapshot_bytes} available."
            )
        if extension == ".xlsx":
            temporary_download.replace(original_path)
            temporary_dataset.replace(dataset_path)
            format_metadata["original_path"] = str(original_path)
        else:
            temporary_download.replace(dataset_path)
    except Exception:
        temporary_download.unlink(missing_ok=True)
        if temporary_csv is not None:
            temporary_csv.unlink(missing_ok=True)
        raise
    return S3Snapshot(
        filename=filename,
        original_path=original_path,
        dataset_path=dataset_path,
        runtime_format=runtime_format,
        columns=columns,
        preview=preview,
        row_count=row_count,
        quality=quality,
        object_metadata=object_metadata,
        format_metadata=format_metadata,
    )
