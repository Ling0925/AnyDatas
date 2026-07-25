from __future__ import annotations

import uuid

import pytest

from app import notification_delivery as delivery_module
from app.db import DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID, connect, encode_json, record_notification
from app.notification_delivery import dispatch_due_notification_deliveries, parse_notification_channel, parse_webhook_url
from app.runner import now_iso
from app.secret_tools import REDACTED_VALUE


def create_secret_reference(name: str, environment_variable: str) -> str:
    secret_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO secret_references (
                id, workspace_id, name, environment_variable, description,
                created_by_user_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, '', ?, ?, ?)
            """,
            (secret_id, DEFAULT_WORKSPACE_ID, name, environment_variable, DEFAULT_USER_ID, timestamp, timestamp),
        )
    return secret_id


def create_channel(
    *,
    name: str,
    channel_type: str,
    destination: str = "",
    secret_id: str | None = None,
    event_types: list[str] | None = None,
    max_retries: int = 3,
) -> str:
    channel_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO notification_channels (
                id, workspace_id, name, channel_type, destination, secret_id,
                event_types_json, max_retries, is_active, created_by_user_id,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                channel_id,
                DEFAULT_WORKSPACE_ID,
                name,
                channel_type,
                destination,
                secret_id,
                encode_json(event_types or ["run.failed"]),
                max_retries,
                DEFAULT_USER_ID,
                timestamp,
                timestamp,
            ),
        )
    return channel_id


def queue_failed_run_notification(delivery_key: str | None = None) -> str:
    with connect() as conn:
        return record_notification(
            conn,
            DEFAULT_WORKSPACE_ID,
            "run.failed",
            "Run failed: nightly revenue",
            "The source query failed.",
            "error",
            "run",
            "run-123",
            delivery_key=delivery_key,
        )


def test_notification_channel_validation_requires_a_real_target():
    parsed = parse_notification_channel(
        "operations",
        "email",
        "ops@example.com, analyst@example.com",
        "",
        "run.failed,report.refresh_failed",
        2,
    )

    assert parsed == (
        "operations",
        "email",
        "ops@example.com,analyst@example.com",
        "",
        ["run.failed", "report.refresh_failed"],
        2,
    )
    with pytest.raises(ValueError, match="Secret Reference"):
        parse_notification_channel("hook", "webhook", "", "", "run.failed", 0)
    with pytest.raises(ValueError, match="Email recipients"):
        parse_notification_channel("ops", "email", "not-an-email", "", "run.failed", 0)
    with pytest.raises(ValueError, match="HTTPS"):
        parse_webhook_url("http://hooks.example.test/notify")


def test_admin_can_create_a_slack_channel_with_a_secret_reference(client, monkeypatch):
    monkeypatch.setenv("ANYDATAS_SECRET_SLACK_WEBHOOK", "https://hooks.slack.test/services/secret")
    secret_id = create_secret_reference("slack-webhook", "ANYDATAS_SECRET_SLACK_WEBHOOK")

    response = client.post(
        "/notification-channels",
        data={
            "name": "analytics slack",
            "channel_type": "slack",
            "secret_id": secret_id,
            "event_types": ["run.failed", "report.refresh_failed"],
            "max_retries": 2,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with connect() as conn:
        channel = conn.execute("SELECT * FROM notification_channels WHERE name = 'analytics slack'").fetchone()
    assert channel["channel_type"] == "slack"
    assert channel["secret_id"] == secret_id
    assert channel["destination"] == ""


@pytest.mark.parametrize("channel_type", ["slack", "teams"])
def test_chat_channel_deliveries_use_native_webhook_payloads(monkeypatch, channel_type):
    webhook_url = f"https://hooks.example.test/{channel_type}/secret"
    environment_name = f"ANYDATAS_SECRET_{channel_type.upper()}_WEBHOOK"
    monkeypatch.setenv(environment_name, webhook_url)
    secret_id = create_secret_reference(f"{channel_type}-webhook", environment_name)
    create_channel(name=f"analytics {channel_type}", channel_type=channel_type, secret_id=secret_id)
    queue_failed_run_notification()
    captured = {}

    def fake_send_webhook(url: str, payload: dict[str, object]) -> None:
        captured["url"] = url
        captured["payload"] = payload

    monkeypatch.setattr(delivery_module, "send_webhook", fake_send_webhook)

    summary = dispatch_due_notification_deliveries()

    assert summary["sent"] == 1
    assert captured["url"] == webhook_url
    if channel_type == "slack":
        assert captured["payload"]["text"].startswith("Run failed: nightly revenue")
        assert captured["payload"]["blocks"][0]["type"] == "header"
    else:
        assert captured["payload"]["type"] == "message"
        content = captured["payload"]["attachments"][0]["content"]
        assert content["type"] == "AdaptiveCard"
        assert content["body"][0]["text"] == "Run failed: nightly revenue"


def test_webhook_delivery_uses_secret_reference_without_persisting_url(monkeypatch):
    webhook_url = "https://hooks.example.test/notify?token=super-secret"
    monkeypatch.setenv("ANYDATAS_SECRET_ALERT_WEBHOOK", webhook_url)
    secret_id = create_secret_reference("alert-webhook", "ANYDATAS_SECRET_ALERT_WEBHOOK")
    create_channel(name="operations webhook", channel_type="webhook", secret_id=secret_id)
    queue_failed_run_notification()
    captured: dict[str, object] = {}

    def fake_send_webhook(url: str, payload: dict[str, object]) -> None:
        captured["url"] = url
        captured["payload"] = payload

    monkeypatch.setattr(delivery_module, "send_webhook", fake_send_webhook)

    summary = dispatch_due_notification_deliveries()

    assert summary == {"claimed": 1, "sent": 1, "retried": 0, "failed": 0, "canceled": 0}
    assert captured["url"] == webhook_url
    assert captured["payload"] == {
        "event_id": captured["payload"]["event_id"],
        "event_type": "run.failed",
        "title": "Run failed: nightly revenue",
        "message": "The source query failed.",
        "severity": "error",
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "resource": {"type": "run", "id": "run-123"},
        "occurred_at": captured["payload"]["occurred_at"],
    }
    with connect() as conn:
        channel = conn.execute("SELECT * FROM notification_channels").fetchone()
        delivery = conn.execute("SELECT * FROM notification_deliveries").fetchone()
        audit_rows = conn.execute("SELECT detail_json FROM audit_events ORDER BY created_at ASC").fetchall()
    assert channel["secret_id"] == secret_id
    assert channel["destination"] == ""
    assert delivery["status"] == "sent"
    assert webhook_url not in delivery["payload_json"]
    assert all(webhook_url not in row["detail_json"] for row in audit_rows)


def test_failed_webhook_delivery_retries_and_redacts_the_reference_value(monkeypatch):
    webhook_url = "https://hooks.example.test/notify?token=super-secret"
    monkeypatch.setenv("ANYDATAS_SECRET_RETRY_WEBHOOK", webhook_url)
    monkeypatch.setenv("ANYDATAS_NOTIFICATION_RETRY_DELAY_SECONDS", "1")
    secret_id = create_secret_reference("retry-webhook", "ANYDATAS_SECRET_RETRY_WEBHOOK")
    create_channel(name="retry webhook", channel_type="webhook", secret_id=secret_id, max_retries=1)
    queue_failed_run_notification()

    def failing_send_webhook(url: str, _payload: dict[str, object]) -> None:
        raise RuntimeError(f"Could not reach {url}")

    monkeypatch.setattr(delivery_module, "send_webhook", failing_send_webhook)

    first_summary = dispatch_due_notification_deliveries()
    with connect() as conn:
        first_delivery = conn.execute("SELECT * FROM notification_deliveries").fetchone()
        conn.execute("UPDATE notification_deliveries SET next_attempt_at = ? WHERE id = ?", (now_iso(), first_delivery["id"]))
    second_summary = dispatch_due_notification_deliveries()
    with connect() as conn:
        delivery = conn.execute("SELECT * FROM notification_deliveries").fetchone()
        audit_rows = conn.execute("SELECT detail_json FROM audit_events ORDER BY created_at ASC").fetchall()

    assert first_summary == {"claimed": 1, "sent": 0, "retried": 1, "failed": 0, "canceled": 0}
    assert second_summary == {"claimed": 1, "sent": 0, "retried": 0, "failed": 1, "canceled": 0}
    assert delivery["status"] == "failed"
    assert delivery["attempt"] == 2
    assert REDACTED_VALUE in delivery["last_error"]
    assert webhook_url not in delivery["last_error"]
    assert all(webhook_url not in row["detail_json"] for row in audit_rows)


def test_admin_can_requeue_a_failed_delivery_with_the_current_channel_policy(client):
    channel_id = create_channel(
        name="requeue email",
        channel_type="email",
        destination="ops@example.com",
        max_retries=0,
    )
    queue_failed_run_notification()
    with connect() as conn:
        delivery = conn.execute("SELECT * FROM notification_deliveries").fetchone()
        conn.execute("UPDATE notification_channels SET max_retries = 2 WHERE id = ?", (channel_id,))
        conn.execute(
            """
            UPDATE notification_deliveries
            SET status = 'failed', attempt = 1, max_attempts = 1, last_error = 'SMTP unavailable',
                finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now_iso(), now_iso(), delivery["id"]),
        )

    response = client.post(f"/notification-deliveries/{delivery['id']}/requeue", follow_redirects=False)

    assert response.status_code == 303
    assert "Notification%20delivery%20requeued" in response.headers["location"]
    with connect() as conn:
        requeued = conn.execute("SELECT * FROM notification_deliveries WHERE id = ?", (delivery["id"],)).fetchone()
        events = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'notification.delivery_requeued' AND resource_id = ?",
            (delivery["id"],),
        ).fetchall()
    assert requeued["status"] == "queued"
    assert requeued["attempt"] == 0
    assert requeued["max_attempts"] == 3
    assert requeued["last_error"] == ""
    assert requeued["finished_at"] is None
    assert len(events) == 1


def test_failed_delivery_cannot_be_requeued_after_its_channel_is_disabled(client):
    channel_id = create_channel(
        name="disabled requeue email",
        channel_type="email",
        destination="ops@example.com",
    )
    queue_failed_run_notification()
    with connect() as conn:
        delivery = conn.execute("SELECT * FROM notification_deliveries").fetchone()
        conn.execute("UPDATE notification_channels SET is_active = 0 WHERE id = ?", (channel_id,))
        conn.execute("UPDATE notification_deliveries SET status = 'failed', attempt = 1 WHERE id = ?", (delivery["id"],))

    response = client.post(f"/notification-deliveries/{delivery['id']}/requeue", follow_redirects=False)

    assert response.status_code == 303
    assert "Enable%20the%20notification%20channel" in response.headers["location"]
    with connect() as conn:
        unchanged = conn.execute("SELECT * FROM notification_deliveries WHERE id = ?", (delivery["id"],)).fetchone()
    assert unchanged["status"] == "failed"
    assert unchanged["attempt"] == 1


def test_email_delivery_uses_configured_recipients_without_smtp_in_tests(monkeypatch):
    create_channel(name="operations email", channel_type="email", destination="ops@example.com, analyst@example.com")
    queue_failed_run_notification()
    captured: dict[str, object] = {}

    def fake_send_email(destination: str, payload: dict[str, object]) -> None:
        captured["destination"] = destination
        captured["payload"] = payload

    monkeypatch.setattr(delivery_module, "send_email", fake_send_email)

    summary = dispatch_due_notification_deliveries()

    assert summary == {"claimed": 1, "sent": 1, "retried": 0, "failed": 0, "canceled": 0}
    assert captured["destination"] == "ops@example.com, analyst@example.com"
    assert captured["payload"]["event_type"] == "run.failed"
    with connect() as conn:
        delivery = conn.execute("SELECT * FROM notification_deliveries").fetchone()
    assert delivery["status"] == "sent"


def test_delivery_key_deduplicates_external_report_notifications():
    secret_id = create_secret_reference("dedupe-webhook", "ANYDATAS_SECRET_DEDUPE_WEBHOOK")
    create_channel(
        name="dedupe webhook",
        channel_type="webhook",
        secret_id=secret_id,
        event_types=["report.refresh_succeeded"],
    )
    with connect() as conn:
        first_id = record_notification(
            conn,
            DEFAULT_WORKSPACE_ID,
            "report.refresh_succeeded",
            "Report refreshed: revenue",
            "Run 123 refreshed the report successfully.",
            "success",
            "report",
            "report-123",
            None,
            "report.refresh_succeeded:report:report-123:run:run-123",
        )
        second_id = record_notification(
            conn,
            DEFAULT_WORKSPACE_ID,
            "report.refresh_succeeded",
            "Report refreshed: revenue",
            "Run 123 refreshed the report successfully.",
            "success",
            "report",
            "report-123",
            None,
            "report.refresh_succeeded:report:report-123:run:run-123",
        )
        deliveries = conn.execute("SELECT * FROM notification_deliveries").fetchall()

    assert first_id != second_id
    assert len(deliveries) == 1
    assert deliveries[0]["notification_id"] == first_id


def test_admin_creates_webhook_channel_and_secret_cannot_be_deleted_while_bound(client):
    response = client.post(
        "/secrets",
        data={
            "name": "configured-webhook",
            "environment_variable": "ANYDATAS_SECRET_CONFIGURED_WEBHOOK",
            "description": "Webhook URL",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        reference = conn.execute("SELECT * FROM secret_references WHERE name = 'configured-webhook'").fetchone()

    response = client.post(
        "/notification-channels",
        data={
            "name": "configured delivery",
            "channel_type": "webhook",
            "destination": "",
            "secret_id": reference["id"],
            "event_types": "run.failed,report.refresh_failed",
            "max_retries": 2,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with connect() as conn:
        channel = conn.execute("SELECT * FROM notification_channels WHERE name = 'configured delivery'").fetchone()

    assert channel["secret_id"] == reference["id"]
    assert channel["destination"] == ""
    delete_response = client.post(f"/secrets/{reference['id']}/delete", follow_redirects=False)

    assert delete_response.status_code == 303
    assert "notification%20channel" in delete_response.headers["location"]


def test_failed_project_run_queues_a_matching_delivery_channel(client, sample_csv_bytes):
    response = client.post(
        "/notification-channels",
        data={
            "name": "run failure email",
            "channel_type": "email",
            "destination": "ops@example.com",
            "secret_id": "",
            "event_types": "run.failed",
            "max_retries": 0,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    source_response = client.post(
        "/data-sources",
        data={"name": "delivery source"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert source_response.status_code == 303
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'delivery source'").fetchone()
    project_response = client.post(
        "/projects",
        data={
            "name": "delivery failing project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT missing_column FROM data;",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert project_response.status_code == 303
    with connect() as conn:
        project = conn.execute("SELECT * FROM projects WHERE name = 'delivery failing project'").fetchone()

    run_response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)

    assert run_response.status_code == 303
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE project_id = ?", (project["id"],)).fetchone()
        delivery = conn.execute(
            """
            SELECT delivery.*, notification.resource_id
            FROM notification_deliveries delivery
            JOIN notifications notification ON notification.id = delivery.notification_id
            WHERE notification.event_type = 'run.failed'
            """
        ).fetchone()
    assert run["status"] == "failed"
    assert delivery["status"] == "queued"
    assert delivery["max_attempts"] == 1
    assert delivery["resource_id"] == run["id"]
