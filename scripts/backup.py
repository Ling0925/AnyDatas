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
DATABASE_FILENAME = "anydatas.db"
LEGACY_DATABASE_FILENAMES = {"anydatas.sqlite3"}
MANIFEST_FILENAME = "manifest.json"
ARCHIVE_PREFIX = "anydatas-backup-"
FORMAT_VERSION = 2
ACTIVE_SCHEMA_TABLES = {"data_sources", "source_tables", "jobs", "staged_imports"}


def default_data_dir() -> Path:
    """返回当前 Rust 服务的数据目录，避免运维脚本继续误操作旧 Python 目录。"""
    return Path(os.getenv("ANYDATAS_DATA_DIR", str(ROOT / "var-rust"))).expanduser().resolve()


def default_backup_dir() -> Path:
    """返回卷外备份目录，确保数据卷损坏时压缩包仍可独立恢复。"""
    return Path(os.getenv("ANYDATAS_BACKUP_DIR", str(ROOT / "backups"))).expanduser().resolve()


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    """通过 SQLite 元数据检查表，兼容空工作区但拒绝误用旧版数据库。"""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def column_exists(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    """检查迁移可能新增的列，使新版备份器也能保护升级前的旧数据库。"""
    row = connection.execute(
        "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
        (table_name, column_name),
    ).fetchone()
    return row is not None


def validate_active_schema(connection: sqlite3.Connection) -> None:
    """确认快照属于当前 Rust 架构，避免生成看似成功但无法启动的旧版备份。"""
    missing = sorted(table for table in ACTIVE_SCHEMA_TABLES if not table_exists(connection, table))
    if missing:
        raise ValueError(f"SQLite database is not the active Rust schema; missing: {', '.join(missing)}")


def normalize_leaf(value: str, label: str) -> str:
    """只接受单层文件名，防止数据库中的异常路径把备份读取范围扩展到数据卷之外。"""
    leaf = Path(value).name
    if not leaf or leaf in {".", ".."} or Path(leaf).name != leaf:
        raise ValueError(f"Unsafe {label} filename in SQLite metadata.")
    return leaf


def artifact_filename(value: str, label: str) -> str:
    """将受限产物键转换为 DuckDB 文件名，避免任务元数据形成目录穿越路径。"""
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    if not value or any(character not in allowed for character in value):
        raise ValueError(f"Unsafe {label} key in SQLite metadata.")
    return f"{value}.duckdb"


def snapshot_database(source: Path, destination: Path) -> dict[str, set[str]]:
    """创建在线一致性快照并移除可重建状态，让备份更小且不会恢复半成品导入。"""
    if not source.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    database_uri = f"{source.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as source_connection:
        with sqlite3.connect(destination) as snapshot_connection:
            source_connection.backup(snapshot_connection)
            validate_active_schema(snapshot_connection)

            uploads = {
                normalize_leaf(row[0], "upload")
                for row in snapshot_connection.execute("SELECT stored_path FROM data_sources")
            }
            job_results = set()
            if column_exists(snapshot_connection, "jobs", "result_artifact_key"):
                job_results = {
                    artifact_filename(row[0], "job result")
                    for row in snapshot_connection.execute(
                        "SELECT result_artifact_key FROM jobs WHERE result_artifact_key IS NOT NULL"
                    )
                }
            encrypted_ai_keys = 0
            if table_exists(snapshot_connection, "workspace_ai_settings"):
                encrypted_ai_keys = snapshot_connection.execute(
                    "SELECT COUNT(*) FROM workspace_ai_settings WHERE api_key_ciphertext IS NOT NULL"
                ).fetchone()[0]

            # 暂存上传和表缓存都可以重新生成；不恢复它们可避免脏任务及巨型重复文件进入备份。
            snapshot_connection.execute("DELETE FROM staged_imports")
            snapshot_connection.execute(
                """
                UPDATE source_tables
                SET cache_key = NULL, cache_status = 'pending', cache_error = NULL
                """
            )
            snapshot_connection.commit()
    return {
        "uploads": uploads,
        "job-results": job_results,
        "encrypted-ai-key": {"required"} if encrypted_ai_keys else set(),
    }


def copy_referenced_payload(
    data_dir: Path,
    staging: Path,
    references: dict[str, set[str]],
) -> None:
    """只复制数据库快照实际引用的不可重建文件，保证备份边界稳定且缺失文件会立即报错。"""
    for directory_name in ("uploads", "job-results"):
        for filename in sorted(references[directory_name]):
            source = data_dir / directory_name / filename
            if not source.is_file() or source.is_symlink():
                raise FileNotFoundError(f"Referenced backup file not found: {source}")
            destination = staging / directory_name / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    secret_key = data_dir / ".secret-key"
    if secret_key.is_file() and not secret_key.is_symlink():
        shutil.copy2(secret_key, staging / secret_key.name)
    elif references["encrypted-ai-key"]:
        raise FileNotFoundError("Encrypted AI settings exist but /data/.secret-key is missing.")


def ensure_backup_dir_is_external(data_dir: Path, backup_dir: Path) -> None:
    """拒绝把备份放回数据卷，否则卷损坏或恢复替换时会同时丢失备份。"""
    try:
        backup_dir.resolve().relative_to(data_dir.resolve())
    except ValueError:
        return
    raise ValueError("Backup directory must not be inside ANYDATAS_DATA_DIR.")


def checksum(path: Path) -> str:
    """流式计算 SHA-256，既支持大文件又不会把压缩包整体读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksum(archive: Path) -> None:
    """存在旁路校验文件时先验证归档，尽早阻止损坏备份覆盖当前数据。"""
    checksum_path = archive.with_suffix(f"{archive.suffix}.sha256")
    if not checksum_path.is_file():
        return
    expected = checksum_path.read_text(encoding="utf-8").split(maxsplit=1)[0].strip()
    if not expected or expected != checksum(archive):
        raise ValueError("Backup archive checksum does not match its SHA-256 file.")


def manifest_files(staging: Path) -> list[dict[str, str | int]]:
    """记录每个载荷文件的大小与摘要，使恢复能发现归档内部的局部损坏。"""
    files = []
    for path in sorted(staging.rglob("*")):
        if path.is_file() and path.name != MANIFEST_FILENAME:
            files.append(
                {
                    "path": path.relative_to(staging).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": checksum(path),
                }
            )
    return files


def create_backup(data_dir: Path, backup_dir: Path, now: datetime | None = None) -> Path:
    """生成可校验的单机备份，在线读取 SQLite 且仅收集一致性快照引用的文件。"""
    data_dir = data_dir.expanduser().resolve()
    backup_dir = backup_dir.expanduser().resolve()
    ensure_backup_dir_is_external(data_dir, backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    effective_now = now or datetime.now(timezone.utc)
    timestamp = effective_now.strftime("%Y%m%dT%H%M%SZ")
    archive = backup_dir / f"{ARCHIVE_PREFIX}{timestamp}.tar.gz"
    if archive.exists():
        raise FileExistsError(f"Backup archive already exists: {archive}")
    temporary_archive = backup_dir / f".{archive.name}.{uuid.uuid4().hex}.tmp"
    staging = Path(tempfile.mkdtemp(prefix=".anydatas-backup-", dir=backup_dir))
    try:
        references = snapshot_database(data_dir / DATABASE_FILENAME, staging / DATABASE_FILENAME)
        copy_referenced_payload(data_dir, staging, references)
        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": effective_now.isoformat(),
            "database": DATABASE_FILENAME,
            "files": manifest_files(staging),
            "excluded_rebuildable_data": ["staged_imports", "table-cache", "query-work"],
        }
        (staging / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
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
    """按归档修改时间清理旧备份，并同步删除对应校验文件。"""
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
    """逐项解压普通文件和目录，拒绝路径穿越、链接及设备节点。"""
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


def validate_manifest(payload: Path, manifest: dict) -> Path:
    """校验归档版本、载荷摘要与 SQLite 完整性，安装前排除错误或截断备份。"""
    format_version = manifest.get("format_version")
    database_name = manifest.get("database")
    if format_version not in {1, FORMAT_VERSION}:
        raise ValueError("Backup archive format is unsupported.")
    if database_name not in {DATABASE_FILENAME, *LEGACY_DATABASE_FILENAMES}:
        raise ValueError("Backup archive database name is unsupported.")
    database_path = payload / database_name
    if not database_path.is_file():
        raise ValueError("Backup archive is missing its SQLite database.")

    if format_version == FORMAT_VERSION:
        declared_files = manifest.get("files")
        if not isinstance(declared_files, list):
            raise ValueError("Backup archive manifest has no file inventory.")
        seen = set()
        for entry in declared_files:
            if not isinstance(entry, dict):
                raise ValueError("Backup archive manifest contains an invalid file entry.")
            relative = PurePosixPath(str(entry.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts or str(relative) in {"", "."}:
                raise ValueError("Backup archive manifest contains an unsafe path.")
            relative_name = relative.as_posix()
            if relative_name in seen:
                raise ValueError("Backup archive manifest contains a duplicate file.")
            seen.add(relative_name)
            path = payload.joinpath(*relative.parts)
            if (
                not path.is_file()
                or path.stat().st_size != entry.get("size_bytes")
                or checksum(path) != entry.get("sha256")
            ):
                raise ValueError(f"Backup payload verification failed: {relative_name}")
        actual_files = {
            path.relative_to(payload).as_posix()
            for path in payload.rglob("*")
            if path.is_file() and path.name != MANIFEST_FILENAME
        }
        if actual_files != seen:
            raise ValueError("Backup payload contains files not declared by the manifest.")

    if database_name != DATABASE_FILENAME:
        normalized_database = payload / DATABASE_FILENAME
        database_path.replace(normalized_database)
        database_path = normalized_database
    with sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Backup SQLite database failed integrity validation.")
        validate_active_schema(connection)
    return database_path


def chown_tree(root: Path, owner_uid: int | None, owner_gid: int | None) -> None:
    """可选修正容器卷所有者，使 root 恢复后的文件仍可被非 root 应用进程写入。"""
    if owner_uid is None and owner_gid is None:
        return
    uid = -1 if owner_uid is None else owner_uid
    gid = -1 if owner_gid is None else owner_gid
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            os.chown(path, uid, gid)


def remove_children(root: Path, excluded: set[str] | None = None) -> None:
    """删除目录内容但保留挂载点本身，供 Docker 命名卷的原位恢复使用。"""
    excluded = excluded or set()
    for path in list(root.iterdir()):
        if path.name in excluded:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def install_in_place(
    payload: Path,
    data_dir: Path,
    owner_uid: int | None,
    owner_gid: int | None,
) -> None:
    """在不能重命名挂载点时逐项安装，并保留同卷回滚副本直到全部复制完成。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    rollback = data_dir / f".anydatas-before-restore-{uuid.uuid4().hex}"
    rollback.mkdir()
    for current in list(data_dir.iterdir()):
        if current != rollback:
            current.replace(rollback / current.name)
    try:
        for source in payload.iterdir():
            destination = data_dir / source.name
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        chown_tree(data_dir, owner_uid, owner_gid)
    except Exception:
        remove_children(data_dir, {rollback.name})
        for previous in list(rollback.iterdir()):
            previous.replace(data_dir / previous.name)
        rollback.rmdir()
        raise
    shutil.rmtree(rollback)


def install_atomically(
    payload: Path,
    data_dir: Path,
    owner_uid: int | None,
    owner_gid: int | None,
) -> None:
    """在普通目录上通过同文件系统重命名安装，失败时恢复原目录。"""
    previous_data = data_dir.parent / f".anydatas-before-restore-{uuid.uuid4().hex}"
    if data_dir.exists():
        data_dir.replace(previous_data)
    try:
        payload.replace(data_dir)
        chown_tree(data_dir, owner_uid, owner_gid)
    except Exception:
        if data_dir.exists():
            shutil.rmtree(data_dir, ignore_errors=True)
        if previous_data.exists():
            previous_data.replace(data_dir)
        raise
    shutil.rmtree(previous_data, ignore_errors=True)


def restore_backup(
    archive: Path | str,
    data_dir: Path | str,
    force: bool = False,
    in_place: bool = False,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> Path:
    """验证并恢复完整数据目录；Docker 卷可选择保留挂载点的原位模式。"""
    if not force:
        raise ValueError("Restore requires --force after the service has been stopped.")
    archive = Path(archive).expanduser().resolve()
    data_dir = Path(data_dir).expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"Backup archive not found: {archive}")
    verify_checksum(archive)
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = None if in_place else data_dir.parent
    staging = Path(tempfile.mkdtemp(prefix=".anydatas-restore-", dir=staging_parent))
    payload = staging / "payload"
    payload.mkdir()
    try:
        safe_extract(archive, payload)
        manifest_path = payload / MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise ValueError("Backup archive is missing its manifest.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_manifest(payload, manifest)
        manifest_path.unlink()
        if in_place:
            install_in_place(payload, data_dir, owner_uid, owner_gid)
        else:
            install_atomically(payload, data_dir, owner_uid, owner_gid)
        return data_dir
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    """解析 CLI 参数并创建备份，错误通过 argparse 以非零状态返回给自动化脚本。"""
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
