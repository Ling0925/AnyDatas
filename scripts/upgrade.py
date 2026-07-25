#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
HealthChecker = Callable[[str], bool]


class UpgradeError(RuntimeError):
    def __init__(self, message: str, backup_path: str):
        super().__init__(message)
        self.backup_path = backup_path


def compose_command(compose_files: Sequence[Path], *arguments: str) -> list[str]:
    command = ["docker", "compose"]
    for compose_file in compose_files:
        command.extend(["-f", str(compose_file)])
    command.extend(arguments)
    return command


def run_checked(
    command: Sequence[str],
    runner: CommandRunner,
    cwd: Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(command)}", flush=True)
    return runner(command, cwd=cwd, check=True, capture_output=capture_output, text=True)


def default_health_checker(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def wait_for_health(url: str, timeout_seconds: int, checker: HealthChecker, sleep: Callable[[float], None]) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if checker(url):
            return
        sleep(2)
    raise TimeoutError(f"Service did not become ready within {timeout_seconds} seconds: {url}")


def run_upgrade(
    compose_files: Sequence[Path],
    retention_days: int = 30,
    health_url: str = "http://127.0.0.1:8000/readyz",
    health_timeout_seconds: int = 120,
    runner: CommandRunner = subprocess.run,
    health_checker: HealthChecker = default_health_checker,
    sleep: Callable[[float], None] = time.sleep,
    root: Path = ROOT,
) -> str:
    normalized_files = [Path(path).expanduser().resolve() for path in compose_files]
    for compose_file in normalized_files:
        if not compose_file.is_file():
            raise FileNotFoundError(f"Compose file not found: {compose_file}")
    if retention_days < 0:
        raise ValueError("Backup retention days cannot be negative.")
    if health_timeout_seconds <= 0:
        raise ValueError("Health timeout must be positive.")

    run_checked(["docker", "compose", "version"], runner, root)
    run_checked(compose_command(normalized_files, "config", "-q"), runner, root)
    backup = run_checked(
        compose_command(
            normalized_files,
            "exec",
            "-T",
            "anydatas",
            "python",
            "scripts/backup.py",
            "--retention-days",
            str(retention_days),
        ),
        runner,
        root,
        capture_output=True,
    )
    backup_path = backup.stdout.strip().splitlines()[-1] if backup.stdout.strip() else "unknown"
    print(f"Backup created: {backup_path}", flush=True)

    try:
        run_checked(compose_command(normalized_files, "build"), runner, root)
        run_checked(compose_command(normalized_files, "up", "-d", "--remove-orphans"), runner, root)
        wait_for_health(health_url, health_timeout_seconds, health_checker, sleep)
    except (OSError, subprocess.CalledProcessError, TimeoutError) as exc:
        raise UpgradeError(f"Upgrade stopped after backup: {exc}", backup_path) from exc
    return backup_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Upgrade an AnyDatas single-server Docker Compose deployment.")
    parser.add_argument(
        "--compose-file",
        action="append",
        type=Path,
        dest="compose_files",
        help="Compose file to include, in order; may be repeated",
    )
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--health-url", default="http://127.0.0.1:8000/readyz")
    parser.add_argument("--health-timeout-seconds", type=int, default=120)
    args = parser.parse_args()
    compose_files = args.compose_files or [ROOT / "docker-compose.yml"]
    backup_path = "not created"
    try:
        backup_path = run_upgrade(
            compose_files,
            retention_days=args.retention_days,
            health_url=args.health_url,
            health_timeout_seconds=args.health_timeout_seconds,
        )
    except UpgradeError as exc:
        print(f"Upgrade failed: {exc}", file=sys.stderr)
        print(f"Latest upgrade backup: {exc.backup_path}", file=sys.stderr)
        raise SystemExit(1) from exc
    except (FileNotFoundError, OSError, subprocess.CalledProcessError, TimeoutError, ValueError) as exc:
        print(f"Upgrade failed before a backup was created: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Upgrade complete. Readiness passed at {args.health_url}. Backup: {backup_path}")


if __name__ == "__main__":
    main()
