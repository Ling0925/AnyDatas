#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


RELEASE_TAG = "duckdb-v1.5.4-anydatas.1"
DUCKDB_VERSION = "1.5.4"
DUCKDB_RS_VERSION = "1.10504.0"
MANIFEST_SHA256 = "74530fcce3336bf930db6da273a2938845dc6ccea169800e6bea209a6bcbd8fa"
RELEASE_BASE_URL = (
    "https://github.com/Ling0925/duckdb-prebuilt/releases/download/"
    f"{RELEASE_TAG}"
)
MANIFEST_NAME = "duckdb-prebuilt-manifest.json"
CACHED_MANIFEST_NAME = ".duckdb-prebuilt-manifest.json"
DOWNLOAD_ATTEMPTS = 3
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class TargetSpec:
    """描述一个受支持 Rust 目标与预编译资产之间的固定映射。"""

    platform: str
    archive: str
    library: str
    link_libraries: tuple[str, ...]
    windows_crt: str | None


TARGETS = {
    "x86_64-unknown-linux-gnu": TargetSpec(
        platform="linux-x64",
        archive="duckdb-static-v1.5.4-linux-x64.zip",
        library="libduckdb_static.a",
        link_libraries=("stdc++", "m", "dl", "pthread"),
        windows_crt=None,
    ),
    "x86_64-pc-windows-msvc": TargetSpec(
        platform="windows-x64-msvc-static-crt",
        archive="duckdb-static-v1.5.4-windows-x64-msvc-static-crt.zip",
        library="duckdb_static.lib",
        link_libraries=("ws2_32", "rstrtmgr", "bcrypt"),
        windows_crt="static",
    ),
    "aarch64-apple-darwin": TargetSpec(
        platform="macos-arm64",
        archive="duckdb-static-v1.5.4-macos-arm64.zip",
        library="libduckdb_static.a",
        link_libraries=("c++",),
        windows_crt=None,
    ),
    "x86_64-apple-darwin": TargetSpec(
        platform="macos-x64",
        archive="duckdb-static-v1.5.4-macos-x64.zip",
        library="libduckdb_static.a",
        link_libraries=("c++",),
        windows_crt=None,
    ),
}


@dataclass(frozen=True)
class ReleasePin:
    """保存消费端信任锚，确保下载地址不能改变固定版本与摘要。"""

    release_tag: str
    duckdb_version: str
    duckdb_rs_version: str
    manifest_sha256: str
    release_base_url: str
    build_revision: int = 1
    rust_toolchain: str = "1.97.0"
    duckdb_rs_features: tuple[str, ...] = ("bundled", "chrono", "serde_json")
    native_features: tuple[str, ...] = ("core_functions",)


PRODUCTION_PIN = ReleasePin(
    release_tag=RELEASE_TAG,
    duckdb_version=DUCKDB_VERSION,
    duckdb_rs_version=DUCKDB_RS_VERSION,
    manifest_sha256=MANIFEST_SHA256,
    release_base_url=RELEASE_BASE_URL,
)


@dataclass(frozen=True)
class PreparedPrebuilt:
    """返回经过校验的静态库目录及其平台身份。"""

    root: Path
    lib_dir: Path
    include_dir: Path
    platform: str
    target_triple: str


class PrebuiltError(RuntimeError):
    """表示下载、清单、缓存或平台契约不满足固定要求。"""


class CacheLock:
    """使用操作系统文件锁串行化同一平台缓存的下载与原子替换。"""

    def __init__(self, path: Path) -> None:
        """记录锁文件路径；真正打开文件延迟到进入上下文时完成。"""
        self.path = path
        self.handle = None

    def __enter__(self) -> "CacheLock":
        """获取跨进程排他锁，Windows 与 Unix 使用各自标准库实现。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"\0")
                self.handle.flush()
            self.handle.seek(0)
            while True:
                try:
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """释放排他锁并关闭文件句柄，让等待进程重新验证缓存。"""
        if self.handle is None:
            return
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def sha256_bytes(payload: bytes) -> str:
    """计算内存数据 SHA-256，便于在写入缓存前完成完整性检查。"""
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，避免大型静态库被整体读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_retryable_download_error(error: BaseException) -> bool:
    """只重试连接类错误和临时 HTTP 状态，永久失败应立即暴露。"""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRYABLE_HTTP_STATUS
    return isinstance(error, (urllib.error.URLError, http.client.HTTPException, OSError))


def download_bytes(url: str, maximum_bytes: int) -> bytes:
    """有限退避下载固定 Release，减少瞬时网络波动浪费整次 CI 任务。"""
    request = urllib.request.Request(url, headers={"User-Agent": "AnyDatas-duckdb-prebuilt/1"})
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read(maximum_bytes + 1)
            if len(payload) > maximum_bytes:
                raise PrebuiltError(f"下载内容超过限制：{url}")
            return payload
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            if attempt == DOWNLOAD_ATTEMPTS or not is_retryable_download_error(error):
                raise
            time.sleep(attempt)
    raise AssertionError("下载重试循环必须返回或抛出异常")


def require_equal(actual: object, expected: object, label: str) -> None:
    """比较外部元数据与固定期望值，并给出可定位的失败字段。"""
    if actual != expected:
        raise PrebuiltError(f"{label} 不匹配：期望 {expected!r}，实际 {actual!r}")


def parse_json_object(payload: bytes, label: str) -> dict:
    """解析不允许重复键的 JSON 对象，避免校验器与消费者看到不同字段值。"""

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        """在对象构建时拒绝重复键，保留清单字段的唯一语义。"""
        result = {}
        for key, value in pairs:
            if key in result:
                raise PrebuiltError(f"{label} 包含重复键：{key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrebuiltError(f"{label} 不是有效 UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise PrebuiltError(f"{label} 根节点必须是对象")
    return parsed


def validate_manifest(manifest: dict, pin: ReleasePin, target_triple: str) -> tuple[dict, TargetSpec]:
    """校验 Release 清单并返回当前目标唯一对应的资产。"""
    spec = TARGETS.get(target_triple)
    if spec is None:
        supported = ", ".join(sorted(TARGETS))
        raise PrebuiltError(f"不支持 Rust target {target_triple!r}；支持：{supported}")

    require_equal(manifest.get("schemaVersion"), 1, "manifest.schemaVersion")
    require_equal(manifest.get("releaseTag"), pin.release_tag, "manifest.releaseTag")
    require_equal(manifest.get("duckdbVersion"), pin.duckdb_version, "manifest.duckdbVersion")
    require_equal(
        manifest.get("duckdbRsVersion"),
        pin.duckdb_rs_version,
        "manifest.duckdbRsVersion",
    )
    require_equal(manifest.get("buildRevision"), pin.build_revision, "manifest.buildRevision")
    require_equal(manifest.get("rustToolchain"), pin.rust_toolchain, "manifest.rustToolchain")
    require_equal(
        manifest.get("duckdbRsFeatures"),
        list(pin.duckdb_rs_features),
        "manifest.duckdbRsFeatures",
    )
    require_equal(
        manifest.get("nativeFeatures"),
        list(pin.native_features),
        "manifest.nativeFeatures",
    )
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        raise PrebuiltError("manifest.assets 必须是数组")
    matches = [asset for asset in assets if asset.get("targetTriple") == target_triple]
    if len(matches) != 1:
        raise PrebuiltError(f"target {target_triple!r} 必须且只能有一个资产")
    asset = matches[0]
    require_equal(asset.get("platform"), spec.platform, "asset.platform")
    require_equal(asset.get("fileName"), spec.archive, "asset.fileName")
    require_equal(asset.get("library"), spec.library, "asset.library")
    require_equal(asset.get("linkLibraries"), list(spec.link_libraries), "asset.linkLibraries")
    require_equal(asset.get("windowsCrt"), spec.windows_crt, "asset.windowsCrt")
    return asset, spec


def validate_metadata(metadata: dict, asset: dict, pin: ReleasePin, target_triple: str) -> None:
    """比较包内 metadata 与 manifest，防止 ZIP 内容和外层清单发生替换。"""
    for key, expected in (
        ("schemaVersion", 1),
        ("releaseTag", pin.release_tag),
        ("duckdbVersion", pin.duckdb_version),
        ("duckdbRsVersion", pin.duckdb_rs_version),
        ("buildRevision", pin.build_revision),
        ("platform", asset["platform"]),
        ("targetTriple", target_triple),
        ("library", asset["library"]),
        ("librarySha256", asset["librarySha256"]),
        ("linkLibraries", asset["linkLibraries"]),
        ("windowsCrt", asset["windowsCrt"]),
        ("duckdbRsFeatures", list(pin.duckdb_rs_features)),
        ("nativeFeatures", list(pin.native_features)),
        ("source", "duckdb-rs bundled native build"),
    ):
        require_equal(metadata.get(key), expected, f"metadata.{key}")


def expected_archive_entries(spec: TargetSpec) -> set[str]:
    """返回目标包允许的精确文件集合，避免解压与链接无关的内容。"""
    entries = {
        "LICENSE-DuckDB",
        "include/duckdb.h",
        "include/duckdb.hpp",
        f"lib/{spec.library}",
        "metadata.json",
    }
    if spec.windows_crt is None:
        entries.add("lib/pkgconfig/duckdb.pc")
    return entries


def validate_zip_member(info: zipfile.ZipInfo) -> None:
    """拒绝可能逃逸目录或创建非普通文件的 ZIP 条目。"""
    name = info.filename
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise PrebuiltError(f"ZIP 包含不安全路径：{name!r}")
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if info.is_dir() or (file_type and not stat.S_ISREG(mode)):
        raise PrebuiltError(f"ZIP 只允许普通文件：{name!r}")


def extract_archive(archive_path: Path, destination: Path, spec: TargetSpec) -> None:
    """按严格白名单逐项解压，防止 zip-slip、符号链接及隐藏额外载荷。"""
    expected = expected_archive_entries(spec)
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise PrebuiltError("ZIP 包含重复文件名")
        for info in infos:
            validate_zip_member(info)
        actual = set(names)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise PrebuiltError(
                f"ZIP 文件集合不匹配；missing={missing!r}, unexpected={unexpected!r}"
            )
        total_size = sum(info.file_size for info in infos)
        if total_size > 1024 * 1024 * 1024:
            raise PrebuiltError("ZIP 解压后大小超过 1 GiB 限制")
        for info in infos:
            output = destination.joinpath(*PurePosixPath(info.filename).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, output.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)


def destination_for(pin: ReleasePin, spec: TargetSpec, cache_root: Path) -> Path:
    """生成包含 Tag、manifest 摘要和平台的缓存键，隔离配方与架构。"""
    return (
        cache_root.resolve()
        / pin.release_tag
        / pin.manifest_sha256
        / spec.platform
    )


def prepared_result(destination: Path, spec: TargetSpec, target_triple: str) -> PreparedPrebuilt:
    """把已验证缓存转换为调用方需要的 include/lib 路径。"""
    return PreparedPrebuilt(
        root=destination,
        lib_dir=destination / "lib",
        include_dir=destination / "include",
        platform=spec.platform,
        target_triple=target_triple,
    )


def validate_cached_package(
    destination: Path,
    pin: ReleasePin,
    target_triple: str,
    spec: TargetSpec,
) -> PreparedPrebuilt | None:
    """重新验证缓存中的信任链；损坏或不完整缓存按未命中处理。"""
    try:
        manifest_path = destination / CACHED_MANIFEST_NAME
        if manifest_path.is_symlink():
            return None
        manifest_payload = manifest_path.read_bytes()
        if sha256_bytes(manifest_payload) != pin.manifest_sha256:
            return None
        manifest = parse_json_object(manifest_payload, "cached manifest")
        asset, cached_spec = validate_manifest(manifest, pin, target_triple)
        if cached_spec != spec:
            return None
        metadata_path = destination / "metadata.json"
        library_path = destination / "lib" / spec.library
        header_paths = [
            destination / "include" / "duckdb.h",
            destination / "include" / "duckdb.hpp",
        ]
        required_paths = [metadata_path, library_path, *header_paths]
        if any(path.is_symlink() or not path.is_file() for path in required_paths):
            return None
        metadata = parse_json_object(metadata_path.read_bytes(), "cached metadata")
        validate_metadata(metadata, asset, pin, target_triple)
        if sha256_file(library_path) != asset.get("librarySha256"):
            return None
        return prepared_result(destination, spec, target_triple)
    except (OSError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, PrebuiltError):
        return None


def download_and_install(
    pin: ReleasePin,
    target_triple: str,
    spec: TargetSpec,
    destination: Path,
) -> PreparedPrebuilt:
    """在持有平台缓存锁时完成下载、验证、解压和原子安装。"""
    manifest_url = f"{pin.release_base_url.rstrip('/')}/{MANIFEST_NAME}"
    manifest_payload = download_bytes(manifest_url, 1024 * 1024)
    require_equal(sha256_bytes(manifest_payload), pin.manifest_sha256, "manifest SHA-256")
    manifest = parse_json_object(manifest_payload, "manifest")
    asset, spec = validate_manifest(manifest, pin, target_triple)

    archive_url = f"{pin.release_base_url.rstrip('/')}/{asset['fileName']}"
    archive_payload = download_bytes(archive_url, 1024 * 1024 * 1024)
    require_equal(len(archive_payload), asset.get("size"), "asset.size")
    require_equal(sha256_bytes(archive_payload), asset.get("sha256"), "asset SHA-256")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{spec.platform}-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        archive_path = staging / "archive.zip"
        archive_path.write_bytes(archive_payload)
        package = staging / "package"
        package.mkdir()
        extract_archive(archive_path, package, spec)
        try:
            metadata = parse_json_object((package / "metadata.json").read_bytes(), "metadata")
        except FileNotFoundError as exc:
            raise PrebuiltError("包内 metadata.json 缺失或无效") from exc
        validate_metadata(metadata, asset, pin, target_triple)
        library_path = package / "lib" / spec.library
        if not library_path.is_file():
            raise PrebuiltError(f"静态库不存在：lib/{spec.library}")
        require_equal(sha256_file(library_path), asset.get("librarySha256"), "library SHA-256")
        (package / CACHED_MANIFEST_NAME).write_bytes(manifest_payload)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(package, destination)

    prepared = validate_cached_package(destination, pin, target_triple, spec)
    if prepared is None:
        raise PrebuiltError("写入后的 DuckDB 预编译缓存校验失败")
    return prepared


def prepare_prebuilt(pin: ReleasePin, target_triple: str, cache_root: Path) -> PreparedPrebuilt:
    """下载并验证目标静态库，然后原子放入按版本隔离的本地缓存。"""
    spec = TARGETS.get(target_triple)
    if spec is None:
        supported = ", ".join(sorted(TARGETS))
        raise PrebuiltError(f"不支持 Rust target {target_triple!r}；支持：{supported}")
    destination = destination_for(pin, spec, cache_root)
    cached = validate_cached_package(destination, pin, target_triple, spec)
    if cached is not None:
        return cached

    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / f".{spec.platform}.lock"
    with CacheLock(lock_path):
        cached = validate_cached_package(destination, pin, target_triple, spec)
        if cached is not None:
            return cached
        return download_and_install(pin, target_triple, spec, destination)


def detect_target_triple(command: list[str]) -> str:
    """优先读取 Cargo 显式 target，否则使用 rustc host，避免按 Python 架构猜测。"""
    for index, argument in enumerate(command):
        if argument == "--target" and index + 1 < len(command):
            return command[index + 1]
        if argument.startswith("--target="):
            return argument.split("=", 1)[1]
    configured = os.environ.get("CARGO_BUILD_TARGET")
    if configured:
        return configured
    result = subprocess.run(
        ["rustc", "-vV"],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("host: "):
            return line.removeprefix("host: ").strip()
    raise PrebuiltError("rustc -vV 未返回 host target")


def default_cache_root() -> Path:
    """返回仓库内可被 CI 与 Docker 显式缓存的默认目录。"""
    return Path(__file__).resolve().parents[1] / ".cache" / "duckdb-prebuilt"


def execute_command(command: list[str], prepared: PreparedPrebuilt) -> int:
    """注入 linked DuckDB 环境并原样返回子命令退出状态。"""
    child_env = os.environ.copy()
    child_env.update(
        {
            "DUCKDB_LIB_DIR": str(prepared.lib_dir),
            "DUCKDB_INCLUDE_DIR": str(prepared.include_dir),
            # 包装器已经按目标白名单补齐系统库；关闭 pkg-config 可避免缓存中的
            # duckdb.pc 在后续构建里重新扩大链接参数边界。
            "DUCKDB_NO_PKG_CONFIG": "1",
            "DUCKDB_STATIC": "1",
        }
    )
    try:
        return subprocess.run(command, env=child_env, check=False).returncode
    except FileNotFoundError:
        print(f"找不到要执行的命令：{command[0]}", file=sys.stderr)
        return 127


def main(argv: list[str] | None = None) -> int:
    """准备预编译库并使用固定链接环境执行用户提供的 Cargo 命令。"""
    parser = argparse.ArgumentParser(description="使用校验后的 DuckDB 预编译静态库运行 Cargo")
    parser.add_argument("--cache-dir", type=Path, default=default_cache_root())
    parser.add_argument("--target", dest="target_triple")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("必须在 -- 后提供要执行的 Cargo 命令")
    try:
        target_triple = args.target_triple or detect_target_triple(command)
        prepared = prepare_prebuilt(PRODUCTION_PIN, target_triple, args.cache_dir)
        print(f"DuckDB {DUCKDB_VERSION} prebuilt ready: {prepared.platform}")
        return execute_command(command, prepared)
    except (OSError, PrebuiltError, subprocess.CalledProcessError) as exc:
        print(f"DuckDB prebuilt 准备失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
