from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from app import s3_tools


def secret_value(**overrides) -> str:
    payload = {
        "endpoint_url": "http://minio:9000",
        "access_key_id": "readonly-key",
        "secret_access_key": "private-secret",
        "region": "us-east-1",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parse_s3_secret_bucket_and_key_validation():
    assert s3_tools.parse_s3_secret_config(secret_value()) == {
        "access_key_id": "readonly-key",
        "secret_access_key": "private-secret",
        "session_token": "",
        "endpoint_url": "http://minio:9000",
        "region": "us-east-1",
        "addressing_style": "path",
    }
    assert s3_tools.parse_s3_bucket("analytics-data") == "analytics-data"
    assert s3_tools.parse_s3_object_key("exports/2026/sales.csv") == "exports/2026/sales.csv"
    with pytest.raises(ValueError, match="unsupported"):
        s3_tools.parse_s3_secret_config(secret_value(profile="default"))
    with pytest.raises(ValueError, match="origin"):
        s3_tools.parse_s3_secret_config(secret_value(endpoint_url="https://user:pass@example.com/path"))
    with pytest.raises(ValueError, match="IP addresses"):
        s3_tools.parse_s3_bucket("192.168.1.1")
    with pytest.raises(ValueError, match="file"):
        s3_tools.parse_s3_object_key("exports/")


def test_download_s3_object_is_version_bound_limited_and_closed(monkeypatch, tmp_path):
    calls = []

    class Body:
        closed = False

        def iter_chunks(self, chunk_size):
            calls.append(("chunk_size", chunk_size))
            yield b"region,revenue\n"
            yield b"East,120\n"

        def close(self):
            self.closed = True

    class Client:
        closed = False

        def head_object(self, **options):
            calls.append(("head", options))
            return {
                "ContentLength": 24,
                "ETag": '"abc123"',
                "VersionId": "version-7",
                "LastModified": datetime(2026, 7, 11, tzinfo=timezone.utc),
            }

        def get_object(self, **options):
            calls.append(("get", options))
            return {"Body": body}

        def close(self):
            self.closed = True

    body = Body()
    client = Client()
    monkeypatch.setattr(s3_tools, "create_s3_client", lambda _value: client)
    destination = tmp_path / "sales.csv"

    metadata = s3_tools.download_s3_object(
        secret_value(),
        "analytics-data",
        "exports/sales.csv",
        destination,
        100,
    )

    assert destination.read_bytes() == b"region,revenue\nEast,120\n"
    assert metadata == {
        "size_bytes": 24,
        "etag": "abc123",
        "version_id": "version-7",
        "last_modified": "2026-07-11T00:00:00+00:00",
    }
    assert calls[1] == (
        "get",
        {"Bucket": "analytics-data", "Key": "exports/sales.csv", "VersionId": "version-7"},
    )
    assert body.closed is True
    assert client.closed is True


def test_download_s3_object_removes_partial_files_when_stream_exceeds_limit(monkeypatch, tmp_path):
    class Body(io.BytesIO):
        def iter_chunks(self, chunk_size):
            yield self.read(chunk_size)

    class Client:
        def head_object(self, **_options):
            return {"ContentLength": 4, "ETag": '"etag"'}

        def get_object(self, **_options):
            return {"Body": Body(b"12345")}

        def close(self):
            pass

    monkeypatch.setattr(s3_tools, "create_s3_client", lambda _value: Client())
    destination = tmp_path / "too-large.csv"

    with pytest.raises(ValueError, match="exceeds"):
        s3_tools.download_s3_object(secret_value(), "analytics-data", "sales.csv", destination, 4)
    assert not destination.exists()
