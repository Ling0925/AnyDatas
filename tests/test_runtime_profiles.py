from __future__ import annotations

import json
import subprocess

import pytest

from app import runner as runner_module
from app.runner import DockerRunner, LocalSubprocessRunner
from app.runtime_profiles import get_runtime_profiles, normalize_runtime_profile


def configured_profiles() -> str:
    return json.dumps(
        {
            "science": {
                "label": "Data Science",
                "image": "registry.example.com/anydatas/science@sha256:" + "a" * 64,
            }
        }
    )


def test_runtime_profiles_are_operator_configured_and_strictly_validated(monkeypatch):
    monkeypatch.setenv("ANYDATAS_RUNTIME_IMAGE", "anydatas-runtime:v2")
    monkeypatch.setenv("ANYDATAS_RUNTIME_PROFILES_JSON", configured_profiles())
    profiles = get_runtime_profiles()

    assert profiles["standard"] == {
        "id": "standard",
        "label": "Standard",
        "image": "anydatas-runtime:v2",
    }
    assert profiles["science"]["label"] == "Data Science"
    assert normalize_runtime_profile("science") == "science"
    with pytest.raises(ValueError, match="available"):
        normalize_runtime_profile("user-supplied-image")

    monkeypatch.setenv("ANYDATAS_RUNTIME_PROFILES_JSON", '{"science":{"label":"Data Science","image":"--privileged"}}')
    with pytest.raises(ValueError, match="image is invalid"):
        get_runtime_profiles()
    monkeypatch.setenv("ANYDATAS_RUNTIME_PROFILES_JSON", '{"standard":{"label":"Other","image":"other:latest"}}')
    with pytest.raises(ValueError, match="cannot be overridden"):
        get_runtime_profiles()


def test_docker_runner_uses_the_versioned_runtime_profile_image(monkeypatch, tmp_path):
    captured = {}
    source_path = tmp_path / "sales.csv"
    source_path.write_text("region,revenue\nEast,120\n", encoding="utf-8")

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("ANYDATAS_RUNTIME_PROFILES_JSON", configured_profiles())
    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(runner_module.subprocess, "run", fake_run)
    monkeypatch.setattr(DockerRunner, "host_data_dir", staticmethod(lambda _docker: None))
    monkeypatch.setattr(runner_module, "read_runner_result", lambda *_args: ({"columns": [], "rows": []}, ""))
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path / "runs")

    DockerRunner().run(
        {"language": "python", "script": "result = load_data()", "runtime_profile": "science"},
        {"source_type": "file", "path": str(source_path), "connection_json": "{}"},
        "science-docker-run",
        {},
        {},
    )

    command = captured["command"]
    configured_image = get_runtime_profiles()["science"]["image"]
    assert command[-3:] == [configured_image, "python", "/work/wrapper.py"]


def test_local_runner_rejects_nonstandard_runtime_profiles(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYDATAS_RUNTIME_PROFILES_JSON", configured_profiles())
    monkeypatch.setattr(runner_module, "RUN_DIR", tmp_path / "runs")

    with pytest.raises(RuntimeError, match="requires ANYDATAS_RUNNER=docker"):
        LocalSubprocessRunner().run(
            {"language": "python", "script": "result = []", "runtime_profile": "science"},
            {"source_type": "file", "path": str(tmp_path / "sales.csv"), "connection_json": "{}"},
            "science-local-run",
            {},
            {},
        )
