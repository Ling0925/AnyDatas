from __future__ import annotations

import os
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlparse

import httpx

from .db import connect, decode_json, encode_json, record_audit
from .secret_tools import redact_text, resolve_secret_reference_value


NOTIFICATION_CHANNEL_TYPES = {"email", "webhook", "slack", "teams"}
NOTIFICATION_EVENT_TYPES = {
    "run.failed",
    "report.refresh_succeeded",
    "report.refresh_failed",
}
MAX_NOTIFICATION_RETRIES = 10
MAX_NOTIFICATION_DELIVERIES_PER_PASS = 10


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_notification_event_types(raw_event_types: str | list[str]) -> list[str]:
    values = raw_event_types.split(",") if isinstance(raw_event_types, str) else [
        part for value in raw_event_types for part in str(value).split(",")
    ]
    normalized = []
    for value in values:
        event_type = str(value).strip().lower()
        if not event_type:
            continue
        if event_type not in NOTIFICATION_EVENT_TYPES:
            raise ValueError("Notification event types are not supported.")
        if event_type not in normalized:
            normalized.append(event_type)
    if not normalized:
        raise ValueError("Select at least one notification event type.")
    return normalized


def parse_email_recipients(value: str) -> list[str]:
    recipients = []
    for raw_recipient in value.split(","):
        recipient = raw_recipient.strip()
        _display_name, parsed_address = parseaddr(recipient)
        if not parsed_address or parsed_address != recipient or "@" not in parsed_address:
            raise ValueError("Email recipients must be comma-separated email addresses.")
        local_part, _, domain = parsed_address.partition("@")
        if not local_part or not domain or "." not in domain or any(character.isspace() for character in parsed_address):
            raise ValueError("Email recipients must be comma-separated email addresses.")
        if parsed_address not in recipients:
            recipients.append(parsed_address)
    if not recipients:
        raise ValueError("Add at least one email recipient.")
    if len(recipients) > 20:
        raise ValueError("A notification channel can have at most 20 email recipients.")
    return recipients


def parse_notification_channel(
    name: str,
    channel_type: str,
    destination: str,
    secret_id: str,
    raw_event_types: str | list[str],
    max_retries: int,
) -> tuple[str, str, str, str, list[str], int]:
    normalized_name = name.strip()
    normalized_type = channel_type.strip().lower()
    normalized_secret_id = secret_id.strip()
    if not normalized_name or len(normalized_name) > 80:
        raise ValueError("Notification channel names must be between 1 and 80 characters.")
    if normalized_type not in NOTIFICATION_CHANNEL_TYPES:
        raise ValueError("Unsupported notification channel type.")
    if max_retries < 0 or max_retries > MAX_NOTIFICATION_RETRIES:
        raise ValueError(f"Notification retries must be between 0 and {MAX_NOTIFICATION_RETRIES}.")
    event_types = parse_notification_event_types(raw_event_types)
    if normalized_type == "email":
        recipients = parse_email_recipients(destination)
        return normalized_name, normalized_type, ",".join(recipients), "", event_types, max_retries
    if not normalized_secret_id:
        raise ValueError("Select a Secret Reference containing the webhook URL.")
    return normalized_name, normalized_type, "", normalized_secret_id, event_types, max_retries


def parse_webhook_url(value: str) -> str:
    webhook_url = value.strip()
    parsed = urlparse(webhook_url)
    allowed_schemes = {"https"}
    if os.getenv("ANYDATAS_ALLOW_INSECURE_WEBHOOKS") == "1":
        allowed_schemes.add("http")
    if parsed.scheme not in allowed_schemes or not parsed.hostname:
        raise ValueError("Webhook Secret References must contain a valid HTTPS URL.")
    return webhook_url


def notification_payload(notification: Any) -> dict[str, Any]:
    return {
        "event_id": str(notification["id"]),
        "event_type": str(notification["event_type"]),
        "title": str(notification["title"]),
        "message": str(notification["message"]),
        "severity": str(notification["severity"]),
        "workspace_id": str(notification["workspace_id"]),
        "resource": {
            "type": str(notification["resource_type"]),
            "id": str(notification["resource_id"]),
        },
        "occurred_at": str(notification["created_at"]),
    }


def enqueue_notification_deliveries(conn, notification_id: str, delivery_key: str | None = None) -> int:
    notification = conn.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    if notification is None:
        return 0
    channels = conn.execute(
        """
        SELECT *
        FROM notification_channels
        WHERE workspace_id = ? AND is_active = 1
        ORDER BY created_at ASC
        """,
        (notification["workspace_id"],),
    ).fetchall()
    if notification["recipient_user_id"] and notification["resource_type"] == "report":
        selected_channel_ids = {
            row["channel_id"]
            for row in conn.execute(
                """
                SELECT channel_id
                FROM report_subscription_channels
                WHERE report_id = ? AND user_id = ? AND workspace_id = ?
                """,
                (
                    notification["resource_id"],
                    notification["recipient_user_id"],
                    notification["workspace_id"],
                ),
            ).fetchall()
        }
        channels = [channel for channel in channels if channel["id"] in selected_channel_ids]
    payload = encode_json(notification_payload(notification))
    dedupe_key = (delivery_key or notification_id)[:255]
    queued = 0
    for channel in channels:
        event_types = decode_json(channel["event_types_json"], [])
        if not isinstance(event_types, list) or notification["event_type"] not in event_types:
            continue
        delivery_id = os.urandom(16).hex()
        insert = conn.execute(
            """
            INSERT OR IGNORE INTO notification_deliveries (
                id, workspace_id, channel_id, notification_id, channel_name, channel_type,
                destination, secret_id, dedupe_key, status, attempt, max_attempts,
                next_attempt_at, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                notification["workspace_id"],
                channel["id"],
                notification_id,
                channel["name"],
                channel["channel_type"],
                channel["destination"],
                channel["secret_id"],
                dedupe_key,
                int(channel["max_retries"]) + 1,
                now_iso(),
                payload,
                now_iso(),
            ),
        )
        if insert.rowcount != 1:
            continue
        queued += 1
        record_audit(
            conn,
            "notification.delivery_queued",
            "notification_delivery",
            delivery_id,
            {
                "notification_id": notification_id,
                "channel_id": channel["id"],
                "channel_type": channel["channel_type"],
                "event_type": notification["event_type"],
            },
            notification["workspace_id"],
        )
    return queued


def notification_retry_delay_seconds(attempt: int) -> int:
    try:
        base_delay = int(os.getenv("ANYDATAS_NOTIFICATION_RETRY_DELAY_SECONDS", "60"))
    except ValueError:
        base_delay = 60
    base_delay = min(max(base_delay, 1), 3600)
    return min(base_delay * (2 ** max(attempt - 1, 0)), 86400)


def claim_due_notification_deliveries(limit: int = MAX_NOTIFICATION_DELIVERIES_PER_PASS) -> list[dict[str, Any]]:
    bounded_limit = min(max(limit, 1), MAX_NOTIFICATION_DELIVERIES_PER_PASS)
    timestamp = now_iso()
    claimed: list[dict[str, Any]] = []
    with connect() as conn:
        candidates = conn.execute(
            """
            SELECT
                delivery.*,
                channel.is_active AS channel_is_active,
                channel.id AS current_channel_id
            FROM notification_deliveries delivery
            LEFT JOIN notification_channels channel ON channel.id = delivery.channel_id
            WHERE delivery.status = 'queued' AND delivery.next_attempt_at <= ?
            ORDER BY delivery.next_attempt_at ASC, delivery.created_at ASC
            LIMIT ?
            """,
            (timestamp, bounded_limit),
        ).fetchall()
        for delivery in candidates:
            update = conn.execute(
                """
                UPDATE notification_deliveries
                SET status = 'sending', attempt = attempt + 1, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (timestamp, delivery["id"]),
            )
            if update.rowcount != 1:
                continue
            claimed_delivery = dict(delivery)
            claimed_delivery["status"] = "sending"
            claimed_delivery["attempt"] = int(delivery["attempt"]) + 1
            claimed.append(claimed_delivery)
    return claimed


def bool_environment(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def smtp_settings() -> tuple[str, int, str, str, str, bool, bool]:
    host = os.getenv("ANYDATAS_SMTP_HOST", "").strip()
    sender = os.getenv("ANYDATAS_SMTP_FROM", "").strip()
    username = os.getenv("ANYDATAS_SMTP_USERNAME", "").strip()
    password = os.getenv("ANYDATAS_SMTP_PASSWORD", "")
    try:
        port = int(os.getenv("ANYDATAS_SMTP_PORT", "587"))
    except ValueError as exc:
        raise ValueError("ANYDATAS_SMTP_PORT must be an integer.") from exc
    if not host or not sender:
        raise ValueError("SMTP delivery requires ANYDATAS_SMTP_HOST and ANYDATAS_SMTP_FROM.")
    if port < 1 or port > 65535:
        raise ValueError("ANYDATAS_SMTP_PORT must be between 1 and 65535.")
    use_ssl = bool_environment("ANYDATAS_SMTP_USE_SSL", False)
    use_starttls = bool_environment("ANYDATAS_SMTP_STARTTLS", not use_ssl)
    if use_ssl and use_starttls:
        raise ValueError("SMTP SSL and STARTTLS cannot both be enabled.")
    return host, port, sender, username, password, use_ssl, use_starttls


def safe_header_value(value: str) -> str:
    return " ".join(value.splitlines())[:160]


def delivery_email_body(payload: dict[str, Any]) -> str:
    resource = payload.get("resource", {})
    if not isinstance(resource, dict):
        resource = {}
    return "\n".join(
        (
            str(payload.get("message", "")),
            "",
            f"Event: {payload.get('event_type', '')}",
            f"Severity: {payload.get('severity', '')}",
            f"Resource: {resource.get('type', '')} {resource.get('id', '')}",
            f"Occurred: {payload.get('occurred_at', '')}",
        )
    )


def send_email(destination: str, payload: dict[str, Any]) -> None:
    recipients = parse_email_recipients(destination)
    host, port, sender, username, password, use_ssl, use_starttls = smtp_settings()
    message = EmailMessage()
    message["Subject"] = safe_header_value(f"[AnyDatas] {payload.get('title', 'Notification')}")
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(delivery_email_body(payload))
    context = ssl.create_default_context()
    smtp_client: smtplib.SMTP
    if use_ssl:
        smtp_client = smtplib.SMTP_SSL(host, port, timeout=10, context=context)
    else:
        smtp_client = smtplib.SMTP(host, port, timeout=10)
    with smtp_client as smtp:
        smtp.ehlo()
        if use_starttls:
            smtp.starttls(context=context)
            smtp.ehlo()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)


def send_webhook(webhook_url: str, payload: dict[str, Any]) -> None:
    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        response = client.post(
            webhook_url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "AnyDatas-Notification/1.0",
                "X-AnyDatas-Event": str(payload.get("event_type", "")),
            },
        )
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"Webhook returned HTTP {response.status_code}.")


def chat_message_text(payload: dict[str, Any]) -> str:
    resource = payload.get("resource", {})
    if not isinstance(resource, dict):
        resource = {}
    return "\n".join(
        (
            f"{payload.get('title', 'AnyDatas notification')}",
            str(payload.get("message", "")),
            f"Event: {payload.get('event_type', '')}",
            f"Severity: {payload.get('severity', '')}",
            f"Resource: {resource.get('type', '')} {resource.get('id', '')}",
        )
    )


def slack_payload(payload: dict[str, Any]) -> dict[str, Any]:
    text = chat_message_text(payload)
    return {
        "text": text,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": str(payload.get("title", "AnyDatas"))[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
        ],
    }


def teams_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": str(payload.get("title", "AnyDatas notification"))[:300],
                            "weight": "Bolder",
                            "wrap": True,
                        },
                        {"type": "TextBlock", "text": chat_message_text(payload)[:3000], "wrap": True},
                    ],
                },
            }
        ],
    }


def webhook_payload(channel_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if channel_type == "slack":
        return slack_payload(payload)
    if channel_type == "teams":
        return teams_payload(payload)
    return payload


def send_notification_delivery(delivery: dict[str, Any]) -> None:
    payload = decode_json(delivery["payload_json"], {})
    if not isinstance(payload, dict):
        raise ValueError("Notification delivery payload is invalid.")
    channel_type = str(delivery["channel_type"])
    if channel_type == "email":
        send_email(str(delivery["destination"]), payload)
        return
    if channel_type not in {"webhook", "slack", "teams"}:
        raise ValueError("Notification delivery channel type is invalid.")
    secret_value = ""
    try:
        with connect() as conn:
            secret_value, _reference = resolve_secret_reference_value(
                conn,
                str(delivery["workspace_id"]),
                str(delivery["secret_id"]),
            )
        webhook_url = parse_webhook_url(secret_value)
        send_webhook(webhook_url, webhook_payload(channel_type, payload))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(redact_text(str(exc), [secret_value])) from None


def finish_notification_delivery(delivery: dict[str, Any], status: str, error: str = "") -> None:
    timestamp = now_iso()
    with connect() as conn:
        update = conn.execute(
            """
            UPDATE notification_deliveries
            SET status = ?, last_error = ?, finished_at = ?, updated_at = ?
            WHERE id = ? AND status = 'sending'
            """,
            (status, error[:1000], timestamp, timestamp, delivery["id"]),
        )
        if update.rowcount != 1:
            return
        record_audit(
            conn,
            "notification.delivery_sent" if status == "sent" else "notification.delivery_canceled",
            "notification_delivery",
            str(delivery["id"]),
            {
                "channel_id": delivery.get("channel_id"),
                "channel_type": delivery["channel_type"],
                "event_type": decode_json(delivery["payload_json"], {}).get("event_type", ""),
                "attempt": delivery["attempt"],
            },
            str(delivery["workspace_id"]),
        )


def retry_or_fail_notification_delivery(delivery: dict[str, Any], error: str) -> None:
    attempt = int(delivery["attempt"])
    max_attempts = int(delivery["max_attempts"])
    timestamp = now_iso()
    final_failure = attempt >= max_attempts
    delay_seconds = notification_retry_delay_seconds(attempt)
    next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
    with connect() as conn:
        if final_failure:
            update = conn.execute(
                """
                UPDATE notification_deliveries
                SET status = 'failed', last_error = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'sending'
                """,
                (error[:1000], timestamp, timestamp, delivery["id"]),
            )
        else:
            update = conn.execute(
                """
                UPDATE notification_deliveries
                SET status = 'queued', last_error = ?, next_attempt_at = ?, updated_at = ?
                WHERE id = ? AND status = 'sending'
                """,
                (error[:1000], next_attempt_at, timestamp, delivery["id"]),
            )
        if update.rowcount != 1:
            return
        record_audit(
            conn,
            "notification.delivery_failed" if final_failure else "notification.delivery_retry_queued",
            "notification_delivery",
            str(delivery["id"]),
            {
                "channel_id": delivery.get("channel_id"),
                "channel_type": delivery["channel_type"],
                "event_type": decode_json(delivery["payload_json"], {}).get("event_type", ""),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "next_attempt_at": None if final_failure else next_attempt_at,
            },
            str(delivery["workspace_id"]),
        )


def dispatch_due_notification_deliveries(limit: int = MAX_NOTIFICATION_DELIVERIES_PER_PASS) -> dict[str, int]:
    summary = {"claimed": 0, "sent": 0, "retried": 0, "failed": 0, "canceled": 0}
    for delivery in claim_due_notification_deliveries(limit):
        summary["claimed"] += 1
        if delivery.get("current_channel_id") is None or int(delivery.get("channel_is_active") or 0) != 1:
            finish_notification_delivery(delivery, "canceled", "Notification channel is no longer active.")
            summary["canceled"] += 1
            continue
        try:
            send_notification_delivery(delivery)
        except Exception as exc:  # noqa: BLE001
            error = redact_text(str(exc) or "Notification delivery failed.", [os.getenv("ANYDATAS_SMTP_PASSWORD", "")])
            retry_or_fail_notification_delivery(delivery, error)
            if int(delivery["attempt"]) >= int(delivery["max_attempts"]):
                summary["failed"] += 1
            else:
                summary["retried"] += 1
        else:
            finish_notification_delivery(delivery, "sent")
            summary["sent"] += 1
    return summary
