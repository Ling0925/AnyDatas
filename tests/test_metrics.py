from __future__ import annotations

import json
from pathlib import Path

from app.db import connect
from app.metrics import metric_line, render_prometheus_metrics, workspace_run_usage


def upload_source(client, name: str, file_bytes: bytes):
    response = client.post(
        "/data-sources",
        data={"name": name},
        files={"file": ("sales.csv", file_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        return conn.execute("SELECT * FROM data_sources WHERE name = ?", (name,)).fetchone()


def create_project(client, name: str, source_id: str, script: str):
    response = client.post(
        "/projects",
        data={
            "name": name,
            "language": "sql",
            "data_source_id": source_id,
            "script": script,
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        return conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


def test_metrics_endpoint_exposes_aggregate_run_and_delivery_state(client, sample_csv_bytes):
    channel_response = client.post(
        "/notification-channels",
        data={
            "name": "metrics email",
            "channel_type": "email",
            "destination": "ops@example.com",
            "secret_id": "",
            "event_types": "run.failed",
            "max_retries": 1,
        },
        follow_redirects=False,
    )
    assert channel_response.status_code == 303
    source = upload_source(client, "metrics source", sample_csv_bytes)
    project = create_project(client, "metrics failure", source["id"], "SELECT missing_column FROM data;")
    run_response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    assert run_response.status_code == 303

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert "# HELP anydatas_runs Number of retained runs grouped by current status." in response.text
    assert 'anydatas_data_sources{source_type="file"} 1' in response.text
    assert 'anydatas_runs{status="failed"} 1' in response.text
    assert 'anydatas_notification_deliveries{status="queued"} 1' in response.text
    assert "anydatas_scheduler_up 0" in response.text


def test_metrics_endpoint_requires_deployment_bearer_token_when_configured(client, monkeypatch):
    token = "metrics-token-that-must-not-leak"
    monkeypatch.setenv("ANYDATAS_METRICS_TOKEN", token)

    unauthorized = client.get("/metrics")
    authorized = client.get("/metrics", headers={"Authorization": f"Bearer {token}"})

    assert unauthorized.status_code == 401
    assert unauthorized.headers["www-authenticate"] == "Bearer"
    assert authorized.status_code == 200
    assert token not in authorized.text


def test_metrics_endpoint_reads_rotatable_token_file_and_fails_closed(client, monkeypatch, tmp_path):
    token_file = tmp_path / "metrics-token"
    token_file.write_text("first-file-token\n", encoding="utf-8")
    monkeypatch.setenv("ANYDATAS_METRICS_TOKEN", "ignored-environment-token")
    monkeypatch.setenv("ANYDATAS_METRICS_TOKEN_FILE", str(token_file))

    assert client.get("/metrics", headers={"Authorization": "Bearer ignored-environment-token"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer first-file-token"}).status_code == 200

    token_file.write_text("rotated-file-token\n", encoding="utf-8")
    assert client.get("/metrics", headers={"Authorization": "Bearer first-file-token"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer rotated-file-token"}).status_code == 200

    token_file.write_text("", encoding="utf-8")
    assert client.get("/metrics").status_code == 503
    token_file.unlink()
    assert client.get("/metrics").status_code == 503


def test_bundled_monitoring_dashboard_and_provisioning_are_consistent():
    root = Path(__file__).resolve().parents[1]
    dashboard = json.loads(
        (root / "monitoring/grafana/dashboards/anydatas-single-server.json").read_text(encoding="utf-8")
    )
    expressions = {
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    }
    prometheus_config = (root / "monitoring/prometheus.yml").read_text(encoding="utf-8")
    compose_overlay = (root / "docker-compose.monitoring.yml").read_text(encoding="utf-8")
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert dashboard["uid"] == "anydatas-single-server"
    assert dashboard["title"] == "AnyDatas Single Server"
    assert len(dashboard["panels"]) == 8
    assert "anydatas_up" in expressions
    assert "anydatas_scheduler_up" in expressions
    assert "anydatas_runs" in expressions
    assert "credentials_file: /run/secrets/anydatas_metrics_token" in prometheus_config
    assert 'targets: ["anydatas:8000"]' in prometheus_config
    assert "127.0.0.1:9090:9090" in compose_overlay
    assert "127.0.0.1:3000:3000" in compose_overlay
    assert "ANYDATAS_GRAFANA_ADMIN_PASSWORD:?" in compose_overlay
    assert ".env.secrets" in dockerignore
    assert "monitoring/metrics-token" in dockerignore


def test_prometheus_metric_labels_are_escaped_and_renderer_keeps_values_aggregate_only():
    assert metric_line("anydatas_info", 1, {"runner": 'docker"runner\\test'}) == 'anydatas_info{runner="docker\\"runner\\\\test"} 1'
    with connect() as conn:
        metrics = render_prometheus_metrics(conn, "local", False, False, None)

    assert "anydatas_workspaces 1" in metrics
    assert "demo-workspace" not in metrics
    assert "demo@anydatas.local" not in metrics


def test_workspace_run_usage_aggregates_status_duration_and_estimated_cost(client, sample_csv_bytes):
    source = upload_source(client, "usage source", sample_csv_bytes)
    project = create_project(client, "usage project", source["id"], "SELECT * FROM data;")
    assert client.post(f"/projects/{project['id']}/run", follow_redirects=False).status_code == 303
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET duration_ms = 7200000, status = 'succeeded' WHERE project_id = ?",
            (project["id"],),
        )
        usage = workspace_run_usage(conn, project["workspace_id"], hourly_cost_cny=3.5)

    retained = next(period for period in usage if period["key"] == "retained")
    assert retained["total_runs"] == 1
    assert retained["succeeded_runs"] == 1
    assert retained["success_rate"] == 100.0
    assert retained["duration_hours"] == 2.0
    assert retained["average_duration_seconds"] == 7200.0
    assert retained["estimated_cost_cny"] == 7.0
