from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


S3_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
S3_SECRET_KEYS = {
    "access_key_id",
    "secret_access_key",
    "session_token",
    "endpoint_url",
    "region",
    "addressing_style",
}


def parse_s3_bucket(value: str) -> str:
    bucket = value.strip()
    if not S3_BUCKET_PATTERN.fullmatch(bucket) or ".." in bucket or ".-" in bucket or "-." in bucket:
        raise ValueError("S3 bucket names must use 3-63 lowercase letters, numbers, dots, or hyphens.")
    try:
        ipaddress.ip_address(bucket)
    except ValueError:
        return bucket
    raise ValueError("S3 bucket names cannot be formatted as IP addresses.")


def parse_s3_object_key(value: str) -> str:
    key = value.strip()
    if not key or key.endswith("/"):
        raise ValueError("S3 object keys must identify a file, not an empty key or prefix.")
    if len(key.encode("utf-8")) > 1024:
        raise ValueError("S3 object keys cannot exceed 1024 UTF-8 bytes.")
    if any(ord(character) < 32 or ord(character) == 127 for character in key):
        raise ValueError("S3 object keys cannot contain control characters.")
    return key


def parse_s3_secret_config(value: str) -> dict[str, str]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("S3 secret values must be a JSON object.") from exc
    if not isinstance(payload, dict):
        raise ValueError("S3 secret values must be a JSON object.")
    unknown_keys = set(payload) - S3_SECRET_KEYS
    if unknown_keys:
        raise ValueError(f"S3 secret values contain unsupported fields: {', '.join(sorted(unknown_keys))}.")

    access_key = payload.get("access_key_id")
    secret_key = payload.get("secret_access_key")
    if not isinstance(access_key, str) or not access_key.strip():
        raise ValueError("S3 secret values must include access_key_id.")
    if not isinstance(secret_key, str) or not secret_key:
        raise ValueError("S3 secret values must include secret_access_key.")

    endpoint_url = payload.get("endpoint_url", "")
    if not isinstance(endpoint_url, str):
        raise ValueError("S3 endpoint_url must be a string.")
    endpoint_url = endpoint_url.strip().rstrip("/")
    if endpoint_url:
        parsed = urlparse(endpoint_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("S3 endpoint_url must be an HTTP(S) origin without credentials, path, query, or fragment.")

    region = payload.get("region", "us-east-1")
    if not isinstance(region, str) or not region.strip():
        raise ValueError("S3 region must be a non-empty string.")
    session_token = payload.get("session_token", "")
    if not isinstance(session_token, str):
        raise ValueError("S3 session_token must be a string.")
    addressing_style = payload.get("addressing_style", "path" if endpoint_url else "auto")
    if addressing_style not in {"auto", "path", "virtual"}:
        raise ValueError("S3 addressing_style must be auto, path, or virtual.")

    return {
        "access_key_id": access_key.strip(),
        "secret_access_key": secret_key,
        "session_token": session_token,
        "endpoint_url": endpoint_url,
        "region": region.strip(),
        "addressing_style": addressing_style,
    }


def s3_secret_redaction_values(secret_value: str) -> list[str]:
    values = [secret_value]
    try:
        config = parse_s3_secret_config(secret_value)
    except ValueError:
        return values
    values.extend(
        value
        for key, value in config.items()
        if key in {"access_key_id", "secret_access_key", "session_token"} and value
    )
    return values


def create_s3_client(secret_value: str):
    import boto3
    from botocore.config import Config

    config = parse_s3_secret_config(secret_value)
    options: dict[str, Any] = {
        "service_name": "s3",
        "aws_access_key_id": config["access_key_id"],
        "aws_secret_access_key": config["secret_access_key"],
        "region_name": config["region"],
        "config": Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": config["addressing_style"]},
        ),
    }
    if config["session_token"]:
        options["aws_session_token"] = config["session_token"]
    if config["endpoint_url"]:
        options["endpoint_url"] = config["endpoint_url"]
    return boto3.client(**options)


def download_s3_object(
    secret_value: str,
    bucket_name: str,
    object_key: str,
    destination: Path,
    max_bytes: int,
) -> dict[str, Any]:
    bucket = parse_s3_bucket(bucket_name)
    key = parse_s3_object_key(object_key)
    if max_bytes < 1:
        raise ValueError("S3 object size limit must be positive.")

    client = create_s3_client(secret_value)
    body = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        content_length = int(head.get("ContentLength", 0))
        if content_length < 0:
            raise ValueError("S3 object reported an invalid size.")
        if content_length > max_bytes:
            raise ValueError(f"S3 object exceeds the configured {max_bytes}-byte import limit.")

        get_options: dict[str, Any] = {"Bucket": bucket, "Key": key}
        version_id = head.get("VersionId")
        etag = head.get("ETag")
        if isinstance(version_id, str) and version_id and version_id != "null":
            get_options["VersionId"] = version_id
        elif isinstance(etag, str) and etag:
            get_options["IfMatch"] = etag

        response = client.get_object(**get_options)
        body = response["Body"]
        copied = 0
        with destination.open("wb") as handle:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                copied += len(chunk)
                if copied > max_bytes:
                    raise ValueError(f"S3 object exceeds the configured {max_bytes}-byte import limit.")
                handle.write(chunk)
        if copied != content_length:
            raise ValueError("S3 object changed size during import.")
        last_modified = head.get("LastModified")
        return {
            "size_bytes": copied,
            "etag": etag.strip('"') if isinstance(etag, str) else "",
            "version_id": version_id if isinstance(version_id, str) and version_id != "null" else "",
            "last_modified": last_modified.isoformat() if hasattr(last_modified, "isoformat") else "",
        }
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if body is not None and callable(getattr(body, "close", None)):
            body.close()
        if callable(getattr(client, "close", None)):
            client.close()
