#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
DATABASE_FILENAME = "anydatas.sqlite3"
MANIFEST_FILENAME = "manifest.json"
ARCHIVE_PREFIX = "anydatas-backup-"


def default_data_dir() -> Path:
    return Path(os.getenv("ANYDATAS_DATA_DIR", str(ROOT / "var"))).expanduser().resolve()


def default_backup_dir() -> Path:
    return Path(os.getenv("ANYDATAS_BACKUP_DIR", str(ROOT / "backups"))).expanduser().resolve()


def copy_sqlite_database(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    database_uri = f"{source.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as source_conn:
        with sqlite3.connect(destination) as destination_conn:
            source_conn.backup(destination_conn)


def ensure_backup_dir_is_external(data_dir: Path, backup_dir: Path) -> None:
    try:
        backup_dir.resolve().relative_to(data_dir.resolve())
    except ValueError:
        return
    raise ValueError("Backup directory must not be inside ANYDATAS_DATA_DIR.")


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(archive: Path) -> None:
    checksum_path = archive.with_suffix(f"{archive.suffix}.sha256")
    if not checksum_path.is_file():
        return
    expected = checksum_path.read_text(encoding="utf-8").split(maxsplit=1)[0].strip()
    if not expected or expected != checksum(archive):
        raise ValueError("Backup archive checksum does not match its SHA-256 file.")


def create_backup(data_dir: Path, backup_dir: Path, now: datetime | None = None) -> Path:
    data_dir = data_dir.expanduser().resolve()
    backup_dir = backup_dir.expanduser().resolve()
    ensure_backup_dir_is_external(data_dir, backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    archive = backup_dir / f"{ARCHIVE_PREFIX}{timestamp}.tar.gz"
    if archive.exists():
        raise FileExistsError(f"Backup archive already exists: {archive}")
    temporary_archive = backup_dir / f".{archive.name}.tmp"
    staging = Path(tempfile.mkdtemp(prefix=".anydatas-backup-", dir=backup_dir))
    try:
        copy_sqlite_database(data_dir / DATABASE_FILENAME, staging / DATABASE_FILENAME)
        ignored = {DATABASE_FILENAME, f"{DATABASE_FILENAME}-wal", f"{DATABASE_FILENAME}-shm"}
        for entry in data_dir.iterdir():
            if entry.name in ignored:
                continue
            target = staging / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target, symlinks=False)
            elif entry.is_file():
                shutil.copy2(entry, target)
        manifest = {
            "format_version": 1,
            "created_at": (now or datetime.now(timezone.utc)).isoformat(),
            "database": DATABASE_FILENAME,
        }
        (staging / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        with tarfile.open(temporary_archive, "w:gz") as bundle:
            for entry in sorted(staging.iterdir(), key=lambda item: item.name):
                bundle.add(entry, arcname=entry.name, recursive=True)
        temporary_archive.replace(archive)
        checksum_path = archive.with_suffix(f"{archive.suffix}.sha256")
        checksum_path.write_text(f"{checksum(archive)}  {archive.name}\n", encoding="utf-8")
    finally:
        temporary_archive.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
    return archive


def prune_backups(backup_dir: Path, retention_days: int) -> list[Path]:
    if retention_days <= 0:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = []
    for archive in backup_dir.glob(f"{ARCHIVE_PREFIX}*.tar.gz"):
        modified_at = datetime.fromtimestamp(archive.stat().st_mtime, timezone.utc)
        if modified_at < cutoff:
            archive.unlink()
            archive.with_suffix(f"{archive.suffix}.sha256").unlink(missing_ok=True)
            removed.append(archive)
    return removed


def safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            member_path = PurePosixPath(member.name)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or not (member.isdir() or member.isfile())
            ):
                raise ValueError("Backup archive contains an unsafe path.")
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError("Backup archive contains an unreadable file.")
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)


def restore_backup(archive: Path | str, data_dir: Path | str, force: bool = False) -> Path:
    if not force:
        raise ValueError("Restore requires --force after the service has been stopped.")
    archive = Path(archive).expanduser().resolve()
    data_dir = Path(data_dir).expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"Backup archive not found: {archive}")
    verify_checksum(archive)
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".anydatas-restore-", dir=data_dir.parent))
    previous_data = data_dir.parent / f".anydatas-before-restore-{uuid.uuid4().hex}"
    payload = staging / "payload"
    payload.mkdir()
    try:
        safe_extract(archive, payload)
        manifest_path = payload / MANIFEST_FILENAME
        database_path = payload / DATABASE_FILENAME
        if not manifest_path.is_file() or not database_path.is_file():
            raise ValueError("Backup archive is missing its manifest or SQLite database.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format_version") != 1 or manifest.get("database") != DATABASE_FILENAME:
            raise ValueError("Backup archive format is unsupported.")
        if data_dir.exists():
            data_dir.replace(previous_data)
        try:
            payload.replace(data_dir)
        except Exception:
            if previous_data.exists():
                previous_data.replace(data_dir)
            raise
        shutil.rmtree(previous_data, ignore_errors=True)
        return data_dir
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a consistent AnyDatas single-server backup.")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--output-dir", type=Path, default=default_backup_dir())
    parser.add_argument("--retention-days", type=int, default=0)
    args = parser.parse_args()
    try:
        archive = create_backup(args.data_dir, args.output_dir)
        prune_backups(args.output_dir.expanduser().resolve(), args.retention_days)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    print(archive)


if __name__ == "__main__":
    main()
