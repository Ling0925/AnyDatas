from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

from scripts.upgrade import UpgradeError, run_upgrade
from app.db import DATA_DIR, connect


ROOT = Path(__file__).resolve().parents[1]


def test_ci_workflow_enforces_release_gates():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "actions/checkout@v4" in workflow
    assert "actions/setup-python@v5" in workflow
    assert 'python-version: "3.11"' in workflow
    assert "python -m compileall -q app scripts tests" in workflow
    assert "python -m pytest" in workflow
    assert "python tests/smoke_test.py" in workflow
    assert "docker compose -f docker-compose.yml config -q" in workflow


def test_control_plane_image_installs_the_docker_client():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "--no-install-recommends docker-cli fonts-droid-fallback" in dockerfile
    assert "--no-install-recommends docker.io" not in dockerfile


def test_compose_passes_explicit_authentication_configuration():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "ANYDATAS_AUTH_MODE: ${ANYDATAS_AUTH_MODE:-demo}" in compose
    assert "ANYDATAS_SESSION_TTL_DAYS: ${ANYDATAS_SESSION_TTL_DAYS:-7}" in compose
    assert "ANYDATAS_COOKIE_SECURE: ${ANYDATAS_COOKIE_SECURE:-0}" in compose
    assert "ANYDATAS_ALLOW_SIGNUP: ${ANYDATAS_ALLOW_SIGNUP:-0}" in compose


def test_password_management_cli_hashes_password_and_revokes_sessions(client):
    password = "operator supplied password"
    environment = os.environ.copy()
    environment["ANYDATAS_TEST_PASSWORD"] = password
    command = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "set_password.py"),
            "demo@anydatas.local",
            "--data-dir",
            str(DATA_DIR),
            "--password-env",
            "ANYDATAS_TEST_PASSWORD",
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert command.returncode == 0, command.stderr
    assert "Password updated and existing sessions revoked" in command.stdout
    assert password not in command.stdout
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = 'demo@anydatas.local'").fetchone()
    assert user["password_hash"].startswith("pbkdf2_sha256$")
    assert password not in user["password_hash"]


def test_single_server_backup_and_restore_scripts_round_trip_data(tmp_path):
    data_dir = tmp_path / "var"
    uploads_dir = data_dir / "uploads"
    uploads_dir.mkdir(parents=True)
    database_path = data_dir / "anydatas.sqlite3"
    with sqlite3.connect(database_path) as conn:
        conn.execute("CREATE TABLE markers (value TEXT NOT NULL)")
        conn.execute("INSERT INTO markers (value) VALUES ('before backup')")
    uploaded_file = uploads_dir / "sales.csv"
    uploaded_file.write_text("region,revenue\nEast,120\n", encoding="utf-8")
    backup_dir = tmp_path / "backups"

    backup = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "backup.py"),
            "--data-dir",
            str(data_dir),
            "--output-dir",
            str(backup_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert backup.returncode == 0, backup.stderr
    archive = Path(backup.stdout.strip())
    assert archive.is_file()
    checksum_path = archive.with_suffix(".gz.sha256")
    assert checksum_path.is_file()

    with sqlite3.connect(database_path) as conn:
        conn.execute("UPDATE markers SET value = 'after backup'")
    uploaded_file.write_text("region,revenue\nWest,999\n", encoding="utf-8")

    missing_force = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "restore.py"),
            str(archive),
            "--data-dir",
            str(data_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_force.returncode != 0
    assert "Restore requires --force" in missing_force.stderr

    original_checksum = checksum_path.read_text(encoding="utf-8")
    checksum_path.write_text(f"0{'0' * 63}  {archive.name}\n", encoding="utf-8")
    invalid_checksum = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "restore.py"),
            str(archive),
            "--data-dir",
            str(data_dir),
            "--force",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert invalid_checksum.returncode != 0
    assert "checksum does not match" in invalid_checksum.stderr
    checksum_path.write_text(original_checksum, encoding="utf-8")

    restore = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "restore.py"),
            str(archive),
            "--data-dir",
            str(data_dir),
            "--force",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert restore.returncode == 0, restore.stderr
    with sqlite3.connect(database_path) as conn:
        marker = conn.execute("SELECT value FROM markers").fetchone()
    assert marker[0] == "before backup"
    assert uploaded_file.read_text(encoding="utf-8") == "region,revenue\nEast,120\n"


def test_restore_rejects_archives_with_unsafe_paths(tmp_path):
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("../outside.txt")
        payload = b"blocked"
        member.size = len(payload)
        bundle.addfile(member, BytesIO(payload))

    restore = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "restore.py"),
            str(archive),
            "--data-dir",
            str(tmp_path / "var"),
            "--force",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert restore.returncode != 0
    assert "unsafe path" in restore.stderr


def test_single_server_upgrade_backs_up_builds_restarts_and_checks_readiness(tmp_path):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    commands = []

    def runner(command, **kwargs):
        commands.append(list(command))
        stdout = "/app/backups/anydatas-backup-test.tar.gz\n" if "scripts/backup.py" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    backup_path = run_upgrade(
        [compose_file],
        retention_days=14,
        runner=runner,
        health_checker=lambda url: url.endswith("/readyz"),
        sleep=lambda seconds: None,
        root=tmp_path,
    )

    assert backup_path == "/app/backups/anydatas-backup-test.tar.gz"
    assert commands[0] == ["docker", "compose", "version"]
    assert commands[1][-2:] == ["config", "-q"]
    assert commands[2][-3:] == ["scripts/backup.py", "--retention-days", "14"]
    assert commands[3][-1] == "build"
    assert commands[4][-3:] == ["up", "-d", "--remove-orphans"]


def test_single_server_upgrade_stops_before_restart_when_build_fails(tmp_path):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    commands = []

    def runner(command, **kwargs):
        commands.append(list(command))
        if command[-1] == "build":
            raise subprocess.CalledProcessError(1, command, stderr="build failed")
        stdout = "/app/backups/anydatas-backup-test.tar.gz\n" if "scripts/backup.py" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    with pytest.raises(UpgradeError) as raised:
        run_upgrade([compose_file], runner=runner, health_checker=lambda url: True, root=tmp_path)

    assert raised.value.backup_path == "/app/backups/anydatas-backup-test.tar.gz"
    assert not any("up" in command for command in commands)


def test_runtime_retention_previews_then_prunes_payloads_and_old_snapshots(client, sample_csv_bytes):
    source_response = client.post(
        "/data-sources",
        data={"name": "retention source"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert source_response.status_code == 303
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'retention source'").fetchone()
    project_response = client.post(
        "/projects",
        data={
            "name": "retention project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT * FROM data;",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert project_response.status_code == 303
    with connect() as conn:
        project = conn.execute("SELECT * FROM projects WHERE name = 'retention project'").fetchone()
    assert client.post(f"/projects/{project['id']}/run", follow_redirects=False).status_code == 303
    report_response = client.post(
        "/reports",
        data={"project_id": project["id"], "title": "Retention report", "description": ""},
        follow_redirects=False,
    )
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    assert client.post(f"/reports/{report_id}/refresh", follow_redirects=False).status_code == 303
    assert client.post(f"/reports/{report_id}/refresh", follow_redirects=False).status_code == 303
    old_time = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    newer_old_time = (datetime.now(timezone.utc) - timedelta(days=119)).isoformat()
    with connect() as conn:
        runs = conn.execute("SELECT * FROM runs WHERE project_id = ? ORDER BY started_at", (project["id"],)).fetchall()
        snapshots = conn.execute(
            "SELECT * FROM report_snapshots WHERE report_id = ? ORDER BY created_at",
            (report_id,),
        ).fetchall()
        conn.execute(
            "UPDATE runs SET started_at = ?, finished_at = ? WHERE project_id = ?",
            (old_time, old_time, project["id"]),
        )
        conn.execute("UPDATE report_snapshots SET created_at = ? WHERE id = ?", (old_time, snapshots[0]["id"]))
        conn.execute("UPDATE report_snapshots SET created_at = ? WHERE id = ?", (newer_old_time, snapshots[1]["id"]))
        conn.execute(
            "UPDATE report_snapshots SET status = 'failed', result_json = NULL, error = 'latest failed' WHERE id = ?",
            (snapshots[2]["id"],),
        )
    assert len(runs) == 3
    assert len(snapshots) == 3
    assert all((DATA_DIR / "runs" / run["id"]).is_dir() for run in runs)

    preview = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "retention.py"),
            "--data-dir",
            str(DATA_DIR),
            "--keep-days",
            "90",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert preview.returncode == 0, preview.stderr
    preview_result = json.loads(preview.stdout)
    assert preview_result["mode"] == "preview"
    assert preview_result["run_payloads"] == 3
    assert preview_result["report_snapshots"] == 1
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM report_snapshots").fetchone()["count"] == 3
        assert conn.execute("SELECT COUNT(result_json) AS count FROM runs").fetchone()["count"] == 3

    applied = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "retention.py"),
            "--data-dir",
            str(DATA_DIR),
            "--keep-days",
            "90",
            "--force",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert applied.returncode == 0, applied.stderr
    applied_result = json.loads(applied.stdout)
    assert applied_result["mode"] == "applied"
    assert applied_result["failed_run_directories"] == []
    with connect() as conn:
        retained_runs = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchall()
        retained_snapshots = conn.execute("SELECT * FROM report_snapshots WHERE report_id = ?", (report_id,)).fetchall()
        audit = conn.execute("SELECT * FROM audit_events WHERE action = 'system.retention_applied'").fetchone()
    assert len(retained_runs) == 3
    assert all(run["result_json"] is None and run["error"] is None for run in retained_runs)
    assert all(run["logs"] == "Run payload pruned by retention policy." for run in retained_runs)
    assert {snapshot["id"] for snapshot in retained_snapshots} == {snapshots[1]["id"], snapshots[2]["id"]}
    assert audit is not None
    assert all(not (DATA_DIR / "runs" / run["id"]).exists() for run in runs)
