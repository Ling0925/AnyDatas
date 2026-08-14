from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "with-duckdb-prebuilt.py"


def sha256_bytes(payload: bytes) -> str:
    """计算测试制品摘要，使用固定字节作为独立预期值来源。"""
    return hashlib.sha256(payload).hexdigest()


def load_downloader_module():
    """从命令行脚本加载公共解析接口，避免测试依赖文件名实现细节。"""
    spec = importlib.util.spec_from_file_location("with_duckdb_prebuilt", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本：{SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CountingFileHandler(SimpleHTTPRequestHandler):
    """提供带请求计数和轻微延迟的本地 Release 服务，用于复现并发冷缓存。"""

    def do_GET(self) -> None:  # noqa: N802
        """记录请求后再返回文件，让两个解析进程稳定进入竞争窗口。"""
        server = self.server
        with server.count_lock:  # type: ignore[attr-defined]
            server.request_count += 1  # type: ignore[attr-defined]
        time.sleep(0.05)
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        """关闭测试 HTTP 访问日志，失败输出只保留断言相关信息。"""
        return


class DuckDbPrebuiltTests(unittest.TestCase):
    """验证预编译 DuckDB 下载器对外暴露的解析与缓存行为。"""

    def setUp(self) -> None:
        """为每个用例建立隔离 Release 与缓存目录，避免共享制品污染结果。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.release_dir = self.root / "release"
        self.cache_dir = self.root / "cache"
        self.release_dir.mkdir()

    def tearDown(self) -> None:
        """清理测试制品，确保安全用例不会在工作区留下缓存。"""
        self.temporary.cleanup()

    def create_release(
        self,
        *,
        extra_entries: dict[str, bytes] | None = None,
        metadata_overrides: dict[str, object] | None = None,
        declared_library_sha256: str | None = None,
    ) -> tuple[object, bytes]:
        """创建一个结构与正式 macOS ARM64 包一致的最小 Release。"""
        library = b"verified-duckdb-static-library"
        library_sha256 = declared_library_sha256 or sha256_bytes(library)
        metadata = {
            "schemaVersion": 1,
            "duckdbVersion": "1.5.4",
            "duckdbRsVersion": "1.10504.0",
            "buildRevision": 1,
            "releaseTag": "duckdb-v1.5.4-anydatas.1",
            "platform": "macos-arm64",
            "targetTriple": "aarch64-apple-darwin",
            "library": "libduckdb_static.a",
            "librarySha256": library_sha256,
            "linkLibraries": ["c++"],
            "windowsCrt": None,
            "duckdbRsFeatures": ["bundled", "chrono", "serde_json"],
            "nativeFeatures": ["core_functions"],
            "source": "duckdb-rs bundled native build",
        }
        metadata.update(metadata_overrides or {})
        archive_path = self.release_dir / "duckdb-static-v1.5.4-macos-arm64.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("LICENSE-DuckDB", "DuckDB license")
            archive.writestr("include/duckdb.h", "/* duckdb.h */")
            archive.writestr("include/duckdb.hpp", "/* duckdb.hpp */")
            archive.writestr("lib/libduckdb_static.a", library)
            archive.writestr(
                "lib/pkgconfig/duckdb.pc",
                "Libs: -L${libdir} -lduckdb_static\nLibs.private: -lc++\n",
            )
            archive.writestr("metadata.json", json.dumps(metadata))
            for name, payload in (extra_entries or {}).items():
                archive.writestr(name, payload)

        archive_payload = archive_path.read_bytes()
        manifest = {
            "schemaVersion": 1,
            "releaseTag": "duckdb-v1.5.4-anydatas.1",
            "duckdbVersion": "1.5.4",
            "duckdbRsVersion": "1.10504.0",
            "rustToolchain": "1.97.0",
            "buildRevision": 1,
            "duckdbRsFeatures": ["bundled", "chrono", "serde_json"],
            "nativeFeatures": ["core_functions"],
            "assets": [
                {
                    "platform": "macos-arm64",
                    "targetTriple": "aarch64-apple-darwin",
                    "fileName": archive_path.name,
                    "size": len(archive_payload),
                    "sha256": sha256_bytes(archive_payload),
                    "library": "libduckdb_static.a",
                    "librarySha256": library_sha256,
                    "linkLibraries": ["c++"],
                    "windowsCrt": None,
                }
            ],
        }
        manifest_payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        (self.release_dir / "duckdb-prebuilt-manifest.json").write_bytes(manifest_payload)

        module = load_downloader_module()
        pin = module.ReleasePin(
            release_tag="duckdb-v1.5.4-anydatas.1",
            duckdb_version="1.5.4",
            duckdb_rs_version="1.10504.0",
            manifest_sha256=sha256_bytes(manifest_payload),
            release_base_url=self.release_dir.as_uri(),
        )
        return pin, library

    def test_prepare_prebuilt_downloads_and_verifies_static_library(self) -> None:
        """有效 Release 应解析为可直接传给 libduckdb-sys 的 include/lib 目录。"""
        pin, expected_library = self.create_release()
        module = load_downloader_module()

        prepared = module.prepare_prebuilt(
            pin=pin,
            target_triple="aarch64-apple-darwin",
            cache_root=self.cache_dir,
        )

        self.assertEqual(
            (prepared.lib_dir / "libduckdb_static.a").read_bytes(),
            expected_library,
        )
        self.assertTrue((prepared.include_dir / "duckdb.h").is_file())
        self.assertEqual(prepared.platform, "macos-arm64")

    def test_download_bytes_retries_a_transient_connection_failure(self) -> None:
        """临时网络故障应在当前任务内有限重试，避免重新运行整套 Action。"""
        module = load_downloader_module()
        transient_error = urllib.error.URLError("temporary failure")
        with patch.object(
            module.urllib.request,
            "urlopen",
            side_effect=[transient_error, io.BytesIO(b"verified")],
        ) as open_url:
            with patch.object(module.time, "sleep") as sleep:
                payload = module.download_bytes("https://example.invalid/asset", 1024)

        self.assertEqual(payload, b"verified")
        self.assertEqual(open_url.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_prepare_prebuilt_rejects_unexpected_archive_entries(self) -> None:
        """静态包出现未声明文件时必须失败，避免 Release ZIP 扩大写入边界。"""
        pin, _ = self.create_release(extra_entries={"unexpected.txt": b"blocked"})
        module = load_downloader_module()

        with self.assertRaisesRegex(module.PrebuiltError, "ZIP.*unexpected"):
            module.prepare_prebuilt(
                pin=pin,
                target_triple="aarch64-apple-darwin",
                cache_root=self.cache_dir,
            )

    def test_prepare_prebuilt_reuses_verified_cache_without_network(self) -> None:
        """首次成功后应从完整缓存工作，避免每条 Cargo 命令重复下载 Release。"""
        pin, expected_library = self.create_release()
        module = load_downloader_module()
        module.prepare_prebuilt(pin, "aarch64-apple-darwin", self.cache_dir)
        for path in self.release_dir.iterdir():
            path.unlink()

        prepared = module.prepare_prebuilt(
            pin=pin,
            target_triple="aarch64-apple-darwin",
            cache_root=self.cache_dir,
        )

        self.assertEqual(
            (prepared.lib_dir / "libduckdb_static.a").read_bytes(),
            expected_library,
        )

    def test_prepare_prebuilt_rejects_metadata_build_revision_mismatch(self) -> None:
        """包内构建修订号必须与固定配方一致，不能只比较 DuckDB 版本。"""
        pin, _ = self.create_release(metadata_overrides={"buildRevision": 2})
        module = load_downloader_module()

        with self.assertRaisesRegex(module.PrebuiltError, "metadata.buildRevision"):
            module.prepare_prebuilt(
                pin=pin,
                target_triple="aarch64-apple-darwin",
                cache_root=self.cache_dir,
            )

    def test_prepare_prebuilt_rejects_manifest_hash_mismatch(self) -> None:
        """Release manifest 字节被替换时必须在解析前失败。"""
        pin, _ = self.create_release()
        module = load_downloader_module()

        with self.assertRaisesRegex(module.PrebuiltError, "manifest SHA-256"):
            module.prepare_prebuilt(
                pin=replace(pin, manifest_sha256="0" * 64),
                target_triple="aarch64-apple-darwin",
                cache_root=self.cache_dir,
            )

    def test_prepare_prebuilt_rejects_library_hash_mismatch(self) -> None:
        """ZIP 与 metadata 即使相互一致，静态库字节不符声明时仍必须失败。"""
        pin, _ = self.create_release(declared_library_sha256="1" * 64)
        module = load_downloader_module()

        with self.assertRaisesRegex(module.PrebuiltError, "library SHA-256"):
            module.prepare_prebuilt(
                pin=pin,
                target_triple="aarch64-apple-darwin",
                cache_root=self.cache_dir,
            )

    def test_prepare_prebuilt_rejects_unsupported_target_before_download(self) -> None:
        """未知 target 应明确失败且不尝试下载其他架构资产。"""
        pin, _ = self.create_release()
        module = load_downloader_module()
        for path in self.release_dir.iterdir():
            path.unlink()

        with self.assertRaisesRegex(module.PrebuiltError, "不支持 Rust target"):
            module.prepare_prebuilt(
                pin=pin,
                target_triple="aarch64-unknown-linux-gnu",
                cache_root=self.cache_dir,
            )

    def test_prepare_prebuilt_serializes_concurrent_cold_cache(self) -> None:
        """并发冷缓存只能下载一份 Release，避免进程互相删除或混合缓存。"""
        pin, expected_library = self.create_release()
        module = load_downloader_module()
        handler = partial(CountingFileHandler, directory=str(self.release_dir))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.request_count = 0  # type: ignore[attr-defined]
        server.count_lock = threading.Lock()  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        http_pin = replace(
            pin,
            release_base_url=f"http://127.0.0.1:{server.server_port}",
        )
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                prepared = list(
                    executor.map(
                        lambda _: module.prepare_prebuilt(
                            http_pin,
                            "aarch64-apple-darwin",
                            self.cache_dir,
                        ),
                        range(2),
                    )
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(server.request_count, 2)  # type: ignore[attr-defined]
        for item in prepared:
            self.assertEqual(
                (item.lib_dir / "libduckdb_static.a").read_bytes(),
                expected_library,
            )

    def test_cli_returns_command_not_found_exit_code(self) -> None:
        """子命令不存在时返回约定的 127，便于 CI 区分下载错误与命令缺失。"""
        pin, _ = self.create_release()
        module = load_downloader_module()
        module.PRODUCTION_PIN = pin

        exit_code = module.main(
            [
                "--cache-dir",
                str(self.cache_dir),
                "--target",
                "aarch64-apple-darwin",
                "--",
                "definitely-missing-anydatas-command",
            ]
        )

        self.assertEqual(exit_code, 127)

    def test_cli_injects_link_environment_and_preserves_exit_code(self) -> None:
        """Cargo 子进程应看到固定链接环境，且失败状态不能被包装器吞掉。"""
        pin, _ = self.create_release()
        module = load_downloader_module()
        module.PRODUCTION_PIN = pin
        observed_path = self.root / "observed-env.json"
        child = (
            "import json, os, pathlib, sys; "
            f"pathlib.Path({str(observed_path)!r}).write_text(json.dumps({{"
            "'DUCKDB_LIB_DIR': os.environ.get('DUCKDB_LIB_DIR'), "
            "'DUCKDB_INCLUDE_DIR': os.environ.get('DUCKDB_INCLUDE_DIR'), "
            "'DUCKDB_NO_PKG_CONFIG': os.environ.get('DUCKDB_NO_PKG_CONFIG'), "
            "'DUCKDB_STATIC': os.environ.get('DUCKDB_STATIC')}, sort_keys=True)); "
            "sys.exit(23)"
        )

        exit_code = module.main(
            [
                "--cache-dir",
                str(self.cache_dir),
                "--target",
                "aarch64-apple-darwin",
                "--",
                sys.executable,
                "-c",
                child,
            ]
        )

        observed = json.loads(observed_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 23)
        self.assertEqual(observed["DUCKDB_NO_PKG_CONFIG"], "1")
        self.assertEqual(observed["DUCKDB_STATIC"], "1")
        self.assertTrue(observed["DUCKDB_LIB_DIR"].endswith("/macos-arm64/lib"))
        self.assertTrue(observed["DUCKDB_INCLUDE_DIR"].endswith("/macos-arm64/include"))


if __name__ == "__main__":
    unittest.main()
