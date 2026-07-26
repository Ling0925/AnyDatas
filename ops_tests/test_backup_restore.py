from __future__ import annotations

import io
import json
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from backup import create_backup, restore_backup  # noqa: E402
from retention import apply_retention  # noqa: E402


ACTIVE_SCHEMA = """
CREATE TABLE data_sources (stored_path TEXT NOT NULL);
CREATE TABLE source_tables (
    cache_key TEXT,
    cache_status TEXT NOT NULL,
    cache_error TEXT
);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    result_json TEXT,
    result_artifact_key TEXT,
    result_artifact_format TEXT,
    result_size_bytes INTEGER,
    result_expires_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE staged_imports (id TEXT PRIMARY KEY);
CREATE TABLE workspace_ai_settings (api_key_ciphertext TEXT);
"""


class BackupRestoreTests(unittest.TestCase):
    """验证当前 Rust 数据模型的备份、恢复及手动保留策略。"""

    def setUp(self) -> None:
        """为每个测试建立独立数据卷，避免归档和恢复状态相互污染。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "data"
        self.backup_dir = self.root / "backups"
        (self.data_dir / "uploads").mkdir(parents=True)
        (self.data_dir / "table-cache").mkdir()
        (self.data_dir / "job-results").mkdir()
        (self.data_dir / "staging").mkdir()
        (self.data_dir / "query-work").mkdir()
        self.database = self.data_dir / "anydatas.db"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(ACTIVE_SCHEMA)

    def tearDown(self) -> None:
        """释放临时目录，确保测试不会向工作区写入运行数据。"""
        self.temporary.cleanup()

    def seed_referenced_files(self) -> None:
        """写入上传、缓存和任务产物，用于验证只备份不可重建数据。"""
        upload = self.data_dir / "uploads" / "source.xlsx"
        result = self.data_dir / "job-results" / "job-one.duckdb"
        upload.write_bytes(b"immutable-upload")
        result.write_bytes(b"complete-result")
        (self.data_dir / "table-cache" / "cache-one.duckdb").write_bytes(b"rebuildable")
        (self.data_dir / "staging" / "pending.xlsx").write_bytes(b"unfinished")
        (self.data_dir / "query-work" / "partial.duckdb").write_bytes(b"temporary")
        (self.data_dir / ".secret-key").write_bytes(b"encrypted-key-material")
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO data_sources (stored_path) VALUES (?)",
                (str(upload),),
            )
            connection.execute(
                """
                INSERT INTO source_tables (cache_key, cache_status, cache_error)
                VALUES ('cache-one', 'ready', NULL)
                """
            )
            connection.execute(
                """
                INSERT INTO jobs (
                    id, status, result_json, result_artifact_key,
                    result_artifact_format, result_size_bytes,
                    result_expires_at, finished_at, updated_at
                )
                VALUES (
                    'job-one', 'succeeded', '{}', 'job-one',
                    'duckdb', 15, '2026-08-01T00:00:00+00:00',
                    '2026-07-01T00:00:00+00:00',
                    '2026-07-01T00:00:00+00:00'
                )
                """
            )
            connection.execute("INSERT INTO staged_imports (id) VALUES ('pending')")
            connection.execute(
                "INSERT INTO workspace_ai_settings (api_key_ciphertext) VALUES ('ciphertext')"
            )

    def test_backup_contains_only_consistent_referenced_payload(self) -> None:
        """备份应保留原文件和任务结果，同时清除暂存导入及可重建缓存状态。"""
        self.seed_referenced_files()
        archive = create_backup(
            self.data_dir,
            self.backup_dir,
            now=datetime(2026, 7, 26, tzinfo=timezone.utc),
        )
        extracted = self.root / "extracted"
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(extracted)

        manifest = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertEqual(manifest["format_version"], 2)
        self.assertIn("anydatas.db", paths)
        self.assertIn("uploads/source.xlsx", paths)
        self.assertIn("job-results/job-one.duckdb", paths)
        self.assertIn(".secret-key", paths)
        self.assertFalse(any(path.startswith("table-cache/") for path in paths))
        self.assertFalse(any(path.startswith("staging/") for path in paths))
        self.assertFalse(any(path.startswith("query-work/") for path in paths))
        with sqlite3.connect(extracted / "anydatas.db") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM staged_imports").fetchone()[0], 0)
            cache = connection.execute(
                "SELECT cache_key, cache_status, cache_error FROM source_tables"
            ).fetchone()
        self.assertEqual(cache, (None, "pending", None))

    def test_restore_round_trip_supports_atomic_and_in_place_modes(self) -> None:
        """普通目录和 Docker 卷模式都应恢复同一快照，且卷模式保留挂载点 inode。"""
        self.seed_referenced_files()
        archive = create_backup(
            self.data_dir,
            self.backup_dir,
            now=datetime(2026, 7, 26, tzinfo=timezone.utc),
        )
        (self.data_dir / "uploads" / "source.xlsx").write_bytes(b"changed")
        restore_backup(archive, self.data_dir, force=True)
        self.assertEqual(
            (self.data_dir / "uploads" / "source.xlsx").read_bytes(),
            b"immutable-upload",
        )

        inode = self.data_dir.stat().st_ino
        (self.data_dir / "unexpected.tmp").write_bytes(b"remove-me")
        restore_backup(archive, self.data_dir, force=True, in_place=True)
        self.assertEqual(self.data_dir.stat().st_ino, inode)
        self.assertFalse((self.data_dir / "unexpected.tmp").exists())
        self.assertTrue((self.data_dir / "job-results" / "job-one.duckdb").is_file())

    def test_restore_rejects_unsafe_archive_paths(self) -> None:
        """恢复不得让归档通过父级路径在目标目录外写文件。"""
        archive = self.root / "unsafe.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            member = tarfile.TarInfo("../outside.txt")
            payload = b"blocked"
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            restore_backup(archive, self.data_dir, force=True)

    def test_missing_referenced_upload_aborts_backup(self) -> None:
        """数据库引用缺失时必须失败，不能留下无法恢复却返回成功的压缩包。"""
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO data_sources (stored_path) VALUES ('/data/uploads/missing.xlsx')"
            )
        with self.assertRaises(FileNotFoundError):
            create_backup(self.data_dir, self.backup_dir)

    def test_retention_previews_then_removes_only_expired_results(self) -> None:
        """保留脚本默认只预览，强制执行后仅清理超过天数的完整结果产物。"""
        old_result = self.data_dir / "job-results" / "old-job.duckdb"
        recent_result = self.data_dir / "job-results" / "recent-job.duckdb"
        old_result.write_bytes(b"old-result")
        recent_result.write_bytes(b"recent-result")
        with sqlite3.connect(self.database) as connection:
            connection.executemany(
                """
                INSERT INTO jobs (
                    id, status, result_json, result_artifact_key,
                    result_artifact_format, result_size_bytes,
                    result_expires_at, finished_at, updated_at
                )
                VALUES (?, 'succeeded', '{}', ?, 'duckdb', ?, ?, ?, ?)
                """,
                [
                    (
                        "old-job",
                        "old-job",
                        old_result.stat().st_size,
                        "2026-05-01T00:00:00+00:00",
                        "2026-05-01T00:00:00+00:00",
                        "2026-05-01T00:00:00+00:00",
                    ),
                    (
                        "recent-job",
                        "recent-job",
                        recent_result.stat().st_size,
                        "2026-08-01T00:00:00+00:00",
                        "2026-07-20T00:00:00+00:00",
                        "2026-07-20T00:00:00+00:00",
                    ),
                ],
            )
        now = datetime(2026, 7, 26, tzinfo=timezone.utc)
        preview = apply_retention(self.data_dir, 30, now=now)
        self.assertEqual(preview["mode"], "preview")
        self.assertEqual(preview["job_results"], 1)
        self.assertTrue(old_result.exists())

        applied = apply_retention(self.data_dir, 30, force=True, now=now)
        self.assertEqual(applied["failed_artifacts"], [])
        self.assertFalse(old_result.exists())
        self.assertTrue(recent_result.exists())
        with sqlite3.connect(self.database) as connection:
            old_key = connection.execute(
                "SELECT result_artifact_key FROM jobs WHERE id = 'old-job'"
            ).fetchone()[0]
            recent_key = connection.execute(
                "SELECT result_artifact_key FROM jobs WHERE id = 'recent-job'"
            ).fetchone()[0]
        self.assertIsNone(old_key)
        self.assertEqual(recent_key, "recent-job")


if __name__ == "__main__":
    unittest.main()
