from __future__ import annotations

import json
import re
import uuid
from urllib.parse import parse_qs, urlparse

import pytest

from app.auth import create_api_token, create_session, hash_password, password_auth_enabled, session_token_hash, verify_password
from app.db import DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID, connect, record_notification
from app.runner import create_run, now_iso


def login(client, email: str, name: str = "Analyst"):
    response = client.post("/login", data={"email": email, "name": name}, follow_redirects=False)
    assert response.status_code == 303
    assert client.cookies.get("anydatas_user_id")
    assert client.cookies.get("anydatas_workspace_id")
    return response


def test_login_page_renders_frontend(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert "Sign in" in response.text
    assert 'name="email"' in response.text
    assert 'autocomplete="username"' in response.text
    assert 'name="name"' in response.text


def test_login_creates_user_workspace_and_updates_topbar(client):
    login(client, "alice@example.com", "Alice")

    response = client.get("/")

    assert response.status_code == 200
    assert "Alice Workspace" in response.text
    assert "alice@example.com" in response.text
    assert "owner" in response.text


def test_password_auth_uses_opaque_expiring_session_and_ignores_forged_identity_cookies(client, monkeypatch):
    password = "correct horse battery staple"
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), DEFAULT_USER_ID))

    client.cookies.clear()
    client.cookies.set("anydatas_user_id", DEFAULT_USER_ID)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    unauthenticated = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert unauthenticated.status_code == 303
    assert unauthenticated.headers["location"] == "/login"

    login_page = client.get("/login")
    assert 'name="password"' in login_page.text
    assert 'name="name"' not in login_page.text
    failed = client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": "wrong password"},
        follow_redirects=False,
    )
    assert failed.status_code == 401
    assert "Invalid email or password" in failed.text
    assert client.cookies.get("anydatas_session") is None

    authenticated = client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": password},
        follow_redirects=False,
    )
    assert authenticated.status_code == 303
    session_token = client.cookies.get("anydatas_session")
    assert session_token
    assert "HttpOnly" in authenticated.headers["set-cookie"]
    assert "SameSite=lax" in authenticated.headers["set-cookie"]
    with connect() as conn:
        session = conn.execute("SELECT * FROM auth_sessions").fetchone()
    assert session["token_hash"] == session_token_hash(session_token)
    assert session_token not in session["token_hash"]
    assert client.get("/").status_code == 200

    with connect() as conn:
        conn.execute("UPDATE auth_sessions SET expires_at = ?", ("2000-01-01T00:00:00+00:00",))
    assert client.get("/", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 303
    authenticated = client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": password},
        follow_redirects=False,
    )
    assert authenticated.status_code == 303

    logout = client.post("/logout", follow_redirects=False)
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM auth_sessions").fetchone()["count"] == 0
    assert client.get("/", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 303


def test_self_signup_is_disabled_by_default_and_creates_isolated_owner_workspace_when_enabled(client, monkeypatch):
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    monkeypatch.delenv("ANYDATAS_ALLOW_SIGNUP", raising=False)
    client.cookies.clear()

    assert client.get("/register").status_code == 404
    assert client.post(
        "/register",
        data={
            "name": "New Analyst",
            "email": "new@example.com",
            "password": "new account password",
            "password_confirmation": "new account password",
        },
        follow_redirects=False,
    ).status_code == 404
    assert 'href="/register"' not in client.get("/login").text

    monkeypatch.setenv("ANYDATAS_ALLOW_SIGNUP", "1")
    registration_page = client.get("/register")
    assert registration_page.status_code == 200
    assert 'action="/register"' in registration_page.text

    mismatch = client.post(
        "/register",
        data={
            "name": "New Analyst",
            "email": "new@example.com",
            "password": "new account password",
            "password_confirmation": "different password",
        },
        follow_redirects=False,
    )
    assert mismatch.status_code == 400
    assert "Password confirmation does not match" in mismatch.text
    too_short = client.post(
        "/register",
        data={
            "name": "New Analyst",
            "email": "new@example.com",
            "password": "short",
            "password_confirmation": "short",
        },
        follow_redirects=False,
    )
    assert too_short.status_code == 400
    assert "at least 12 characters" in too_short.text

    created = client.post(
        "/register",
        data={
            "name": "New Analyst",
            "email": "NEW@example.com",
            "password": "new account password",
            "password_confirmation": "new account password",
        },
        follow_redirects=False,
    )

    assert created.status_code == 303
    assert created.headers["location"] == "/?notice=Account%20created"
    session_token = client.cookies.get("anydatas_session")
    assert session_token
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = 'new@example.com'").fetchone()
        membership = conn.execute("SELECT * FROM memberships WHERE user_id = ?", (user["id"],)).fetchone()
        workspace = conn.execute("SELECT * FROM workspaces WHERE id = ?", (membership["workspace_id"],)).fetchone()
        session = conn.execute("SELECT * FROM auth_sessions WHERE user_id = ?", (user["id"],)).fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_events WHERE action = 'user.registered' AND resource_id = ?",
            (user["id"],),
        ).fetchone()
    assert user["password_hash"] != "new account password"
    assert verify_password("new account password", user["password_hash"])
    assert membership["role"] == "owner"
    assert workspace["name"] == "New Analyst Workspace"
    assert session["token_hash"] == session_token_hash(session_token)
    assert session_token not in session["token_hash"]
    assert json.loads(audit["detail_json"]) == {"email": "new@example.com"}
    home = client.get("/")
    assert home.status_code == 200
    assert "New Analyst Workspace" in home.text
    assert "new@example.com" in home.text

    duplicate = client.post(
        "/register",
        data={
            "name": "Duplicate",
            "email": "new@example.com",
            "password": "another account password",
            "password_confirmation": "another account password",
        },
        follow_redirects=False,
    )
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.text
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE email = 'new@example.com'").fetchone()[0] == 1


def test_password_auth_rate_limits_repeated_failures_and_recovers_after_lock_expiry(client, monkeypatch):
    password = "correct horse battery staple"
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), DEFAULT_USER_ID))
    client.cookies.clear()

    responses = [
        client.post(
            "/login",
            data={"email": "demo@anydatas.local", "password": "wrong password"},
            follow_redirects=False,
        )
        for _ in range(5)
    ]

    assert [response.status_code for response in responses] == [401, 401, 401, 401, 429]
    assert responses[-1].headers["retry-after"] == "900"
    locked = client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": password},
        follow_redirects=False,
    )
    assert locked.status_code == 429
    with connect() as conn:
        conn.execute("UPDATE auth_login_attempts SET locked_until = '2000-01-01T00:00:00+00:00'")
    recovered = client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": password},
        follow_redirects=False,
    )
    assert recovered.status_code == 303
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM auth_login_attempts").fetchone()["count"] == 0


def test_authenticated_user_can_rotate_password_and_revoke_old_sessions(client, monkeypatch):
    old_password = "old account password 2026"
    new_password = "new account password 2026"
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(old_password), DEFAULT_USER_ID))
    client.cookies.clear()
    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": old_password},
        follow_redirects=False,
    ).status_code == 303
    old_session_token = client.cookies.get("anydatas_session")
    account_page = client.get("/")
    assert 'id="account-security"' in account_page.text
    assert 'action="/account/password"' in account_page.text

    wrong_current = client.post(
        "/account/password",
        data={
            "current_password": "incorrect password",
            "new_password": new_password,
            "new_password_confirmation": new_password,
        },
        follow_redirects=False,
    )
    mismatch = client.post(
        "/account/password",
        data={
            "current_password": old_password,
            "new_password": new_password,
            "new_password_confirmation": "different confirmation",
        },
        follow_redirects=False,
    )
    unchanged = client.post(
        "/account/password",
        data={
            "current_password": old_password,
            "new_password": old_password,
            "new_password_confirmation": old_password,
        },
        follow_redirects=False,
    )
    assert "Current%20password%20is%20incorrect" in wrong_current.headers["location"]
    assert "New%20passwords%20do%20not%20match" in mismatch.headers["location"]
    assert "must%20differ" in unchanged.headers["location"]

    changed = client.post(
        "/account/password",
        data={
            "current_password": old_password,
            "new_password": new_password,
            "new_password_confirmation": new_password,
        },
        follow_redirects=False,
    )

    assert changed.status_code == 303
    new_session_token = client.cookies.get("anydatas_session")
    assert new_session_token and new_session_token != old_session_token
    with connect() as conn:
        sessions = conn.execute("SELECT * FROM auth_sessions WHERE user_id = ?", (DEFAULT_USER_ID,)).fetchall()
        audit = conn.execute("SELECT * FROM audit_events WHERE action = 'user.password_changed'").fetchone()
    assert len(sessions) == 1
    assert sessions[0]["token_hash"] == session_token_hash(new_session_token)
    assert audit is not None

    client.cookies.clear()
    client.cookies.set("anydatas_session", old_session_token)
    assert client.get("/", headers={"Accept": "text/html"}, follow_redirects=False).status_code == 303
    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": old_password},
        follow_redirects=False,
    ).status_code == 401
    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": new_password},
        follow_redirects=False,
    ).status_code == 303


def test_owner_password_reset_link_is_one_time_and_revokes_sessions_and_tokens(client, monkeypatch):
    owner_password = "password reset owner password"
    old_password = "password reset old password"
    new_password = "password reset new password"
    target_user_id = uuid.uuid4().hex
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    monkeypatch.setenv("ANYDATAS_PASSWORD_RESET_TTL_HOURS", "2")
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(owner_password), DEFAULT_USER_ID))
        conn.execute(
            "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (target_user_id, "reset@example.com", "Reset User", hash_password(old_password), now_iso()),
        )
        conn.execute(
            "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'analyst', ?)",
            (target_user_id, DEFAULT_WORKSPACE_ID, now_iso()),
        )
        old_session = create_session(conn, target_user_id, DEFAULT_WORKSPACE_ID)
        _, old_api_token = create_api_token(
            conn,
            target_user_id,
            DEFAULT_WORKSPACE_ID,
            "reset test token",
            30,
            "full",
        )
    client.cookies.clear()
    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": owner_password},
        follow_redirects=False,
    ).status_code == 303

    first_created = client.post(
        f"/workspace/members/{target_user_id}/password-reset",
        follow_redirects=False,
    )
    assert first_created.status_code == 201
    assert first_created.headers["cache-control"] == "no-store"
    assert first_created.headers["referrer-policy"] == "no-referrer"
    first_token = re.search(r"/reset-password/([A-Za-z0-9_-]+)", first_created.text).group(1)
    with connect() as conn:
        first_record = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (target_user_id,),
        ).fetchone()
    assert first_record["token_hash"] == session_token_hash(first_token)
    assert first_token not in first_record["token_hash"]

    second_created = client.post(
        f"/workspace/members/{target_user_id}/password-reset",
        follow_redirects=False,
    )
    second_token = re.search(r"/reset-password/([A-Za-z0-9_-]+)", second_created.text).group(1)
    assert second_token != first_token
    assert client.get(f"/reset-password/{first_token}").status_code == 404
    reset_page = client.get(f"/reset-password/{second_token}")
    assert reset_page.status_code == 200
    assert reset_page.headers["cache-control"] == "no-store"
    assert "Reset Password" in reset_page.text

    mismatch = client.post(
        f"/reset-password/{second_token}",
        data={"password": new_password, "password_confirmation": "different password"},
        follow_redirects=False,
    )
    assert mismatch.status_code == 400
    weak = client.post(
        f"/reset-password/{second_token}",
        data={"password": "short", "password_confirmation": "short"},
        follow_redirects=False,
    )
    assert weak.status_code == 400

    reset = client.post(
        f"/reset-password/{second_token}",
        data={"password": new_password, "password_confirmation": new_password},
        follow_redirects=False,
    )

    assert reset.status_code == 303
    assert reset.headers["location"] == "/login?notice=Password%20reset"
    assert "Password reset" in client.get(reset.headers["location"]).text
    with connect() as conn:
        target = conn.execute("SELECT * FROM users WHERE id = ?", (target_user_id,)).fetchone()
        remaining_session = conn.execute(
            "SELECT * FROM auth_sessions WHERE token_hash = ?",
            (session_token_hash(old_session),),
        ).fetchone()
        api_token = conn.execute(
            "SELECT * FROM api_tokens WHERE token_hash = ?",
            (session_token_hash(old_api_token),),
        ).fetchone()
        used_reset = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE token_hash = ?",
            (session_token_hash(second_token),),
        ).fetchone()
        events = conn.execute(
            "SELECT action FROM audit_events WHERE resource_id = ? ORDER BY created_at",
            (target_user_id,),
        ).fetchall()
    assert verify_password(new_password, target["password_hash"])
    assert remaining_session is None
    assert api_token["revoked_at"] is not None
    assert used_reset["used_at"] is not None
    assert client.get(f"/reset-password/{second_token}").status_code == 404
    assert client.get(
        "/api/workspace/quota",
        headers={"Authorization": f"Bearer {old_api_token}", "Accept": "application/json"},
    ).status_code == 401
    client.cookies.clear()
    assert client.post(
        "/login",
        data={"email": "reset@example.com", "password": old_password},
        follow_redirects=False,
    ).status_code == 401
    assert client.post(
        "/login",
        data={"email": "reset@example.com", "password": new_password},
        follow_redirects=False,
    ).status_code == 303
    assert [event["action"] for event in events] == [
        "user.password_reset_created",
        "user.password_reset_created",
        "user.password_reset",
    ]


def test_admin_cannot_create_password_reset_for_owner_or_admin(client, monkeypatch):
    password = "admin reset boundary password"
    admin_user_id = uuid.uuid4().hex
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (admin_user_id, "reset-admin@example.com", "Reset Admin", hash_password(password), now_iso()),
        )
        conn.execute(
            "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'admin', ?)",
            (admin_user_id, DEFAULT_WORKSPACE_ID, now_iso()),
        )
    client.cookies.clear()
    assert client.post(
        "/login",
        data={"email": "reset-admin@example.com", "password": password},
        follow_redirects=False,
    ).status_code == 303

    response = client.post(
        f"/workspace/members/{DEFAULT_USER_ID}/password-reset",
        follow_redirects=False,
    )

    assert response.status_code == 403
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM password_reset_tokens").fetchone()[0] == 0


def test_personal_api_token_is_hashed_expiring_revocable_and_cannot_mint_tokens(client, monkeypatch):
    password = "api token owner password"
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), DEFAULT_USER_ID))
    client.cookies.clear()
    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": password},
        follow_redirects=False,
    ).status_code == 303

    invalid = client.post(
        "/service-accounts",
        data={"name": "partial bot", "role": "analyst", "scope": "owner", "expires_days": 30},
        follow_redirects=False,
    )
    assert invalid.status_code == 303
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM service_accounts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM users WHERE email LIKE '%@service.anydatas.invalid'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memberships").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 0

    created = client.post(
        "/account/api-tokens",
        data={"name": "automation", "expires_days": 30, "scope": "full"},
        follow_redirects=False,
    )

    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert created.headers["referrer-policy"] == "no-referrer"
    token_match = re.search(r'value="(anydatas_[A-Za-z0-9_-]+)"', created.text)
    assert token_match is not None
    token = token_match.group(1)
    assert "Full access" in created.text
    with connect() as conn:
        token_record = conn.execute("SELECT * FROM api_tokens WHERE name = 'automation'").fetchone()
    assert token_record["token_hash"] == session_token_hash(token)
    assert token_record["scope"] == "full"
    assert token not in token_record["token_hash"]
    account_page = client.get("/")
    assert "Active API Tokens" in account_page.text
    assert "automation" in account_page.text
    assert token not in account_page.text
    invalid_scope = client.post(
        "/account/api-tokens",
        data={"name": "invalid scope", "expires_days": 30, "scope": "owner"},
        follow_redirects=False,
    )
    assert invalid_scope.status_code == 303
    assert parse_qs(urlparse(invalid_scope.headers["location"]).query)["notice"] == [
        "API token scope must be read or full."
    ]
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 1

    client.cookies.clear()
    bearer_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    notifications = client.get("/api/notifications", headers=bearer_headers)
    assert notifications.status_code == 200
    with connect() as conn:
        used_token = conn.execute("SELECT * FROM api_tokens WHERE id = ?", (token_record["id"],)).fetchone()
    assert used_token["last_used_at"] is not None
    assert client.post(
        "/account/api-tokens",
        data={"name": "forbidden", "expires_days": 30},
        headers=bearer_headers,
        follow_redirects=False,
    ).status_code == 403

    with connect() as conn:
        conn.execute(
            "UPDATE memberships SET role = 'viewer' WHERE user_id = ? AND workspace_id = ?",
            (DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID),
        )
    role_limited = client.post(
        "/workspace/quotas",
        data={
            "max_data_sources": 1,
            "max_projects": 1,
            "max_schedules": 1,
            "max_reports": 1,
            "max_concurrent_runs": 1,
        },
        headers=bearer_headers,
        follow_redirects=False,
    )
    assert role_limited.status_code == 403
    with connect() as conn:
        conn.execute(
            "UPDATE memberships SET role = 'owner' WHERE user_id = ? AND workspace_id = ?",
            (DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID),
        )

    with connect() as conn:
        conn.execute("UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (token_record["id"],))
    expired = client.get("/api/notifications", headers=bearer_headers)
    assert expired.status_code == 401
    assert expired.headers["www-authenticate"] == "Bearer"

    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": password},
        follow_redirects=False,
    ).status_code == 303
    second_created = client.post(
        "/account/api-tokens",
        data={"name": "revocable", "expires_days": 7},
        follow_redirects=False,
    )
    second_token = re.search(r'value="(anydatas_[A-Za-z0-9_-]+)"', second_created.text).group(1)
    with connect() as conn:
        second_record = conn.execute("SELECT * FROM api_tokens WHERE name = 'revocable'").fetchone()
    assert client.post(f"/account/api-tokens/{second_record['id']}/revoke", follow_redirects=False).status_code == 303
    revoked = client.get(
        "/api/notifications",
        headers={"Authorization": f"Bearer {second_token}", "Accept": "application/json"},
    )
    assert revoked.status_code == 401

    read_created = client.post(
        "/account/api-tokens",
        data={"name": "read automation", "expires_days": 14, "scope": "read"},
        follow_redirects=False,
    )
    assert read_created.status_code == 201
    assert "Read only" in read_created.text
    read_token = re.search(r'value="(anydatas_[A-Za-z0-9_-]+)"', read_created.text).group(1)
    with connect() as conn:
        read_record = conn.execute("SELECT * FROM api_tokens WHERE name = 'read automation'").fetchone()
    assert read_record["scope"] == "read"

    client.cookies.clear()
    read_headers = {"Authorization": f"Bearer {read_token}", "Accept": "application/json"}
    assert client.get("/api/workspace/quota", headers=read_headers).status_code == 200
    read_write = client.post(
        "/workspace/quotas",
        data={
            "max_data_sources": 1,
            "max_projects": 1,
            "max_schedules": 1,
            "max_reports": 1,
            "max_concurrent_runs": 1,
        },
        headers=read_headers,
        follow_redirects=False,
    )
    assert read_write.status_code == 403
    assert read_write.json()["detail"] == "API token requires full scope for write requests"

    with connect() as conn:
        events = conn.execute(
            "SELECT action, detail_json FROM audit_events WHERE action LIKE 'user.api_token_%' ORDER BY created_at"
        ).fetchall()
    assert [event["action"] for event in events] == [
        "user.api_token_created",
        "user.api_token_created",
        "user.api_token_revoked",
        "user.api_token_created",
    ]
    assert json.loads(events[0]["detail_json"])["scope"] == "full"
    assert json.loads(events[-1]["detail_json"])["scope"] == "read"


def test_service_account_has_independent_role_rotatable_tokens_and_revocation(client, monkeypatch, sample_csv_bytes):
    password = "service account owner password"
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), DEFAULT_USER_ID))
    client.cookies.clear()
    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": password},
        follow_redirects=False,
    ).status_code == 303

    created = client.post(
        "/service-accounts",
        data={"name": "daily report bot", "role": "analyst", "scope": "read", "expires_days": 30},
        follow_redirects=False,
    )

    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert "daily report bot credential" in created.text
    read_token = re.search(r'value="(anydatas_[A-Za-z0-9_-]+)"', created.text).group(1)
    with connect() as conn:
        service_account = conn.execute("SELECT * FROM service_accounts WHERE name = 'daily report bot'").fetchone()
        service_user = conn.execute("SELECT * FROM users WHERE id = ?", (service_account["user_id"],)).fetchone()
        membership = conn.execute(
            "SELECT * FROM memberships WHERE user_id = ? AND workspace_id = ?",
            (service_account["user_id"], DEFAULT_WORKSPACE_ID),
        ).fetchone()
        initial_token = conn.execute("SELECT * FROM api_tokens WHERE user_id = ?", (service_account["user_id"],)).fetchone()
    assert service_account["role"] == "analyst"
    assert service_user["password_hash"] is None
    assert service_user["email"].endswith("@service.anydatas.invalid")
    assert membership["role"] == "analyst"
    assert initial_token["scope"] == "read"
    assert initial_token["token_hash"] == session_token_hash(read_token)
    assert read_token not in initial_token["token_hash"]
    account_page = client.get("/")
    assert "daily report bot" in account_page.text
    assert service_user["email"] not in account_page.text
    assert "1 members" in account_page.text
    assert read_token not in account_page.text

    client.cookies.clear()
    read_headers = {"Authorization": f"Bearer {read_token}", "Accept": "application/json"}
    assert client.get("/api/workspace/quota", headers=read_headers).status_code == 200
    assert client.post(
        "/data-sources",
        data={"name": "blocked service upload"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        headers=read_headers,
        follow_redirects=False,
    ).status_code == 403
    assert client.post(
        f"/service-accounts/{service_account['id']}/tokens",
        data={"scope": "full", "expires_days": 30},
        headers=read_headers,
        follow_redirects=False,
    ).status_code == 403

    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": password},
        follow_redirects=False,
    ).status_code == 303
    rotated = client.post(
        f"/service-accounts/{service_account['id']}/tokens",
        data={"scope": "full", "expires_days": 14},
        follow_redirects=False,
    )
    assert rotated.status_code == 201
    full_token = re.search(r'value="(anydatas_[A-Za-z0-9_-]+)"', rotated.text).group(1)

    client.cookies.clear()
    full_headers = {"Authorization": f"Bearer {full_token}"}
    uploaded = client.post(
        "/data-sources",
        data={"name": "service upload"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        headers=full_headers,
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'service upload'").fetchone()
    assert source["created_by_user_id"] == service_account["user_id"]

    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": password},
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        conn.execute(
            "UPDATE memberships SET role = 'viewer' WHERE user_id = ? AND workspace_id = ?",
            (DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID),
        )
    assert client.post(
        "/service-accounts",
        data={"name": "blocked bot", "role": "viewer", "scope": "read", "expires_days": 7},
        follow_redirects=False,
    ).status_code == 403
    with connect() as conn:
        conn.execute(
            "UPDATE memberships SET role = 'owner' WHERE user_id = ? AND workspace_id = ?",
            (DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID),
        )

    revoked = client.post(f"/service-accounts/{service_account['id']}/revoke", follow_redirects=False)
    assert revoked.status_code == 303
    client.cookies.clear()
    for token in (read_token, full_token):
        response = client.get(
            "/api/workspace/quota",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        assert response.status_code == 401
    with connect() as conn:
        revoked_account = conn.execute("SELECT * FROM service_accounts WHERE id = ?", (service_account["id"],)).fetchone()
        remaining_membership = conn.execute(
            "SELECT * FROM memberships WHERE user_id = ? AND workspace_id = ?",
            (service_account["user_id"], DEFAULT_WORKSPACE_ID),
        ).fetchone()
        tokens = conn.execute("SELECT * FROM api_tokens WHERE user_id = ?", (service_account["user_id"],)).fetchall()
        events = conn.execute(
            "SELECT action FROM audit_events WHERE resource_id = ? ORDER BY created_at",
            (service_account["id"],),
        ).fetchall()
    assert revoked_account["revoked_at"] is not None
    assert remaining_membership is None
    assert all(token["revoked_at"] is not None for token in tokens)
    assert [event["action"] for event in events] == [
        "service_account.created",
        "service_account.token_created",
        "service_account.revoked",
    ]


def test_unknown_authentication_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "passwrod")

    with pytest.raises(RuntimeError, match="must be demo or password"):
        password_auth_enabled()


def test_password_mode_workspace_invitation_is_hashed_expiring_one_time_and_role_scoped(client, monkeypatch):
    owner_password = "owner password for invites"
    invited_password = "invited member password"
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(owner_password), DEFAULT_USER_ID))
    client.cookies.clear()
    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": owner_password},
        follow_redirects=False,
    ).status_code == 303

    created = client.post(
        "/workspace/invitations",
        data={"email": "invited@example.com", "name": "Invited", "role": "viewer"},
        follow_redirects=False,
    )

    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert created.headers["referrer-policy"] == "no-referrer"
    match = re.search(r"/accept-invitation/([A-Za-z0-9_-]+)", created.text)
    assert match is not None
    token = match.group(1)
    with connect() as conn:
        invitation = conn.execute("SELECT * FROM workspace_invitations WHERE email = 'invited@example.com'").fetchone()
        invited_user = conn.execute("SELECT * FROM users WHERE email = 'invited@example.com'").fetchone()
    assert invitation["token_hash"] == session_token_hash(token)
    assert token not in invitation["token_hash"]
    assert invited_user is None
    workspace_page = client.get("/")
    assert "Pending Invitations" in workspace_page.text
    assert "invited@example.com" in workspace_page.text
    assert token not in workspace_page.text

    invitation_page = client.get(f"/accept-invitation/{token}")
    assert invitation_page.status_code == 200
    assert invitation_page.headers["cache-control"] == "no-store"
    assert invitation_page.headers["referrer-policy"] == "no-referrer"
    assert "invited@example.com" in invitation_page.text
    mismatch = client.post(
        f"/accept-invitation/{token}",
        data={"password": invited_password, "password_confirmation": "different password"},
        follow_redirects=False,
    )
    assert mismatch.status_code == 400
    assert "Passwords do not match" in mismatch.text

    accepted = client.post(
        f"/accept-invitation/{token}",
        data={"password": invited_password, "password_confirmation": invited_password},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    member_home = client.get("/")
    assert member_home.status_code == 200
    assert "invited@example.com" in member_home.text
    assert "viewer" in member_home.text
    assert client.get(f"/accept-invitation/{token}").status_code == 404
    assert client.post(
        "/workspace/invitations",
        data={"email": "blocked@example.com", "name": "Blocked", "role": "viewer"},
        follow_redirects=False,
    ).status_code == 403
    with connect() as conn:
        accepted_invitation = conn.execute("SELECT * FROM workspace_invitations WHERE id = ?", (invitation["id"],)).fetchone()
        events = conn.execute(
            "SELECT action FROM audit_events WHERE action LIKE 'workspace.invitation_%' ORDER BY created_at"
        ).fetchall()
    assert accepted_invitation["accepted_at"] is not None
    assert [event["action"] for event in events] == [
        "workspace.invitation_created",
        "workspace.invitation_accepted",
    ]


def test_password_mode_owner_can_revoke_pending_invitation(client, monkeypatch):
    owner_password = "owner password for revocation"
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(owner_password), DEFAULT_USER_ID))
    client.cookies.clear()
    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": owner_password},
        follow_redirects=False,
    ).status_code == 303
    created = client.post(
        "/workspace/invitations",
        data={"email": "revoked@example.com", "name": "Revoked", "role": "analyst"},
        follow_redirects=False,
    )
    token = re.search(r"/accept-invitation/([A-Za-z0-9_-]+)", created.text).group(1)
    with connect() as conn:
        invitation = conn.execute("SELECT * FROM workspace_invitations WHERE email = 'revoked@example.com'").fetchone()

    revoked = client.post(f"/workspace/invitations/{invitation['id']}/revoke", follow_redirects=False)

    assert revoked.status_code == 303
    assert client.get(f"/accept-invitation/{token}").status_code == 404
    with connect() as conn:
        updated = conn.execute("SELECT * FROM workspace_invitations WHERE id = ?", (invitation["id"],)).fetchone()
        audit = conn.execute("SELECT * FROM audit_events WHERE action = 'workspace.invitation_revoked'").fetchone()
    assert updated["revoked_at"] is not None
    assert audit is not None

    expired_created = client.post(
        "/workspace/invitations",
        data={"email": "expired@example.com", "name": "Expired", "role": "viewer"},
        follow_redirects=False,
    )
    expired_token = re.search(r"/accept-invitation/([A-Za-z0-9_-]+)", expired_created.text).group(1)
    with connect() as conn:
        conn.execute(
            "UPDATE workspace_invitations SET expires_at = '2000-01-01T00:00:00+00:00' WHERE email = 'expired@example.com'"
        )
    assert client.get(f"/accept-invitation/{expired_token}").status_code == 404
    assert "expired@example.com" not in client.get("/").text


def test_existing_account_invitation_requires_current_password_and_revokes_after_failures(client, monkeypatch):
    owner_password = "owner password existing invite"
    existing_password = "existing account password"
    monkeypatch.setenv("ANYDATAS_AUTH_MODE", "password")
    monkeypatch.setenv("ANYDATAS_COOKIE_SECURE", "0")
    existing_user_id = uuid.uuid4().hex
    passwordless_user_id = uuid.uuid4().hex
    locked_user_id = uuid.uuid4().hex
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(owner_password), DEFAULT_USER_ID))
        conn.execute(
            "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (existing_user_id, "existing@example.com", "Existing", hash_password(existing_password), now_iso()),
        )
        conn.execute(
            "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (locked_user_id, "locked@example.com", "Locked", hash_password(existing_password), now_iso()),
        )
        conn.execute(
            "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, NULL, ?)",
            (passwordless_user_id, "passwordless@example.com", "Passwordless", now_iso()),
        )
    client.cookies.clear()
    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": owner_password},
        follow_redirects=False,
    ).status_code == 303

    existing_created = client.post(
        "/workspace/invitations",
        data={"email": "existing@example.com", "name": "Existing", "role": "analyst"},
        follow_redirects=False,
    )
    existing_token = re.search(r"/accept-invitation/([A-Za-z0-9_-]+)", existing_created.text).group(1)
    assert "Current password" in client.get(f"/accept-invitation/{existing_token}").text
    assert client.post(
        f"/accept-invitation/{existing_token}",
        data={"password": existing_password},
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        membership = conn.execute(
            "SELECT * FROM memberships WHERE user_id = ? AND workspace_id = ?",
            (existing_user_id, DEFAULT_WORKSPACE_ID),
        ).fetchone()
    assert membership["role"] == "analyst"

    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": owner_password},
        follow_redirects=False,
    ).status_code == 303
    passwordless_created = client.post(
        "/workspace/invitations",
        data={"email": "passwordless@example.com", "name": "Passwordless", "role": "viewer"},
        follow_redirects=False,
    )
    passwordless_token = re.search(r"/accept-invitation/([A-Za-z0-9_-]+)", passwordless_created.text).group(1)
    passwordless_page = client.get(f"/accept-invitation/{passwordless_token}")
    assert "Create password" in passwordless_page.text
    assert client.post(
        f"/accept-invitation/{passwordless_token}",
        data={"password": existing_password, "password_confirmation": existing_password},
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        passwordless_user = conn.execute("SELECT * FROM users WHERE id = ?", (passwordless_user_id,)).fetchone()
        passwordless_membership = conn.execute(
            "SELECT * FROM memberships WHERE user_id = ? AND workspace_id = ?",
            (passwordless_user_id, DEFAULT_WORKSPACE_ID),
        ).fetchone()
    assert passwordless_user["password_hash"].startswith("pbkdf2_sha256$")
    assert passwordless_membership["role"] == "viewer"

    assert client.post(
        "/login",
        data={"email": "demo@anydatas.local", "password": owner_password},
        follow_redirects=False,
    ).status_code == 303
    locked_created = client.post(
        "/workspace/invitations",
        data={"email": "locked@example.com", "name": "Locked", "role": "viewer"},
        follow_redirects=False,
    )
    locked_token = re.search(r"/accept-invitation/([A-Za-z0-9_-]+)", locked_created.text).group(1)

    failed = [
        client.post(
            f"/accept-invitation/{locked_token}",
            data={"password": "incorrect existing password"},
            follow_redirects=False,
        )
        for _ in range(5)
    ]

    assert [response.status_code for response in failed] == [401, 401, 401, 401, 401]
    assert "Invitation is invalid or expired" in failed[-1].text
    assert client.get(f"/accept-invitation/{locked_token}").status_code == 404
    with connect() as conn:
        locked_invitation = conn.execute(
            "SELECT * FROM workspace_invitations WHERE email = 'locked@example.com'"
        ).fetchone()
    assert locked_invitation["failed_attempts"] == 5
    assert locked_invitation["revoked_at"] is not None


def test_workspace_data_isolation_between_logged_in_users(client, sample_csv_bytes):
    login(client, "alice@example.com", "Alice")
    response = client.post(
        "/data-sources",
        data={"name": "alice_sales"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "alice_sales" in client.get("/").text

    login(client, "bob@example.com", "Bob")
    bob_home = client.get("/")

    assert bob_home.status_code == 200
    assert "Bob Workspace" in bob_home.text
    assert "alice_sales" not in bob_home.text


def test_viewer_role_cannot_create_data_source(client, sample_csv_bytes):
    viewer_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
            (viewer_id, "viewer@example.com", "Viewer", timestamp),
        )
        conn.execute(
            "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'viewer', ?)",
            (viewer_id, DEFAULT_WORKSPACE_ID, timestamp),
        )

    client.cookies.set("anydatas_user_id", viewer_id)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    response = client.post(
        "/data-sources",
        data={"name": "blocked"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    postgres_response = client.post(
        "/data-sources/postgres",
        data={"name": "blocked postgres", "secret_id": "not-authorized", "schema_name": "public", "table_name": "sales"},
        follow_redirects=False,
    )
    mysql_response = client.post(
        "/data-sources/mysql",
        data={"name": "blocked mysql", "secret_id": "not-authorized", "database_name": "warehouse", "table_name": "sales"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert postgres_response.status_code == 403
    assert mysql_response.status_code == 403
    assert "Connect PostgreSQL" not in client.get("/").text
    assert "Connect MySQL" not in client.get("/").text
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'blocked'").fetchone()
    assert source is None


def test_viewer_role_cannot_update_data_source_schema(client, sample_csv_bytes):
    source_response = client.post(
        "/data-sources",
        data={"name": "schema viewer sales"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert source_response.status_code == 303
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'schema viewer sales'").fetchone()

    viewer_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
            (viewer_id, "schema-viewer@example.com", "Schema Viewer", timestamp),
        )
        conn.execute(
            "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'viewer', ?)",
            (viewer_id, DEFAULT_WORKSPACE_ID, timestamp),
        )

    client.cookies.set("anydatas_user_id", viewer_id)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    assert client.get(f"/data-sources/{source['id']}").status_code == 200
    response = client.post(
        f"/data-sources/{source['id']}/schema",
        data={
            "field_names": ["date", "revenue", "region"],
            "field_types": ["date", "number", "text"],
            "descriptions": ["Day", "Revenue", "Region"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_viewer_role_cannot_manage_or_bind_secret_references(client, sample_csv_bytes):
    source_response = client.post(
        "/data-sources",
        data={"name": "secret viewer sales"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert source_response.status_code == 303
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'secret viewer sales'").fetchone()
    project_response = client.post(
        "/projects",
        data={
            "name": "secret viewer project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT * FROM data LIMIT 1;",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert project_response.status_code == 303
    reference_response = client.post(
        "/secrets",
        data={"name": "viewer-test", "environment_variable": "ANYDATAS_SECRET_VIEWER_TEST", "description": ""},
        follow_redirects=False,
    )
    assert reference_response.status_code == 303
    with connect() as conn:
        project = conn.execute("SELECT * FROM projects WHERE name = 'secret viewer project'").fetchone()
        reference = conn.execute("SELECT * FROM secret_references WHERE name = 'viewer-test'").fetchone()

    viewer_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
            (viewer_id, "secret-viewer@example.com", "Secret Viewer", timestamp),
        )
        conn.execute(
            "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'viewer', ?)",
            (viewer_id, DEFAULT_WORKSPACE_ID, timestamp),
        )

    client.cookies.set("anydatas_user_id", viewer_id)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    assert client.post(
        "/secrets",
        data={"name": "blocked", "environment_variable": "ANYDATAS_SECRET_BLOCKED", "description": ""},
        follow_redirects=False,
    ).status_code == 403
    assert client.post(
        f"/projects/{project['id']}/secrets",
        data={"secret_id": reference["id"], "environment_name": "ANYDATAS_USER_SECRET_VIEWER_TEST"},
        follow_redirects=False,
    ).status_code == 403
    assert client.post(
        "/notification-channels",
        data={
            "name": "blocked delivery",
            "channel_type": "email",
            "destination": "ops@example.com",
            "secret_id": "",
            "event_types": "run.failed",
            "max_retries": 1,
        },
        follow_redirects=False,
    ).status_code == 403
    assert "Secret Bindings" not in client.get("/").text
    assert "Delivery Channels" not in client.get("/").text


def test_viewer_role_cannot_cancel_workspace_run(client, sample_csv_bytes):
    source_response = client.post(
        "/data-sources",
        data={"name": "cancel access sales"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert source_response.status_code == 303
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'cancel access sales'").fetchone()
    project_response = client.post(
        "/projects",
        data={
            "name": "cancel access project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT * FROM data LIMIT 1;",
        },
        follow_redirects=False,
    )
    assert project_response.status_code == 303
    with connect() as conn:
        project = conn.execute("SELECT * FROM projects WHERE name = 'cancel access project'").fetchone()
    run_id = create_run(project["id"], "manual")

    viewer_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
            (viewer_id, "run-viewer@example.com", "Run Viewer", timestamp),
        )
        conn.execute(
            "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'viewer', ?)",
            (viewer_id, DEFAULT_WORKSPACE_ID, timestamp),
        )

    client.cookies.set("anydatas_user_id", viewer_id)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    response = client.post(f"/runs/{run_id}/cancel", follow_redirects=False)

    assert response.status_code == 403
    with connect() as conn:
        run = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "queued"


def test_owner_can_add_member_and_member_enters_shared_workspace(client, sample_csv_bytes):
    response = client.post(
        "/workspace/members",
        data={"email": "teammate@example.com", "name": "Teammate", "role": "viewer"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Member%20updated" in response.headers["location"]

    home = client.get("/")
    assert "teammate@example.com" in home.text
    assert "viewer" in home.text

    login(client, "teammate@example.com", "Teammate")
    member_home = client.get("/")
    assert "Demo Workspace" in member_home.text
    assert "teammate@example.com" in member_home.text
    assert "viewer" in member_home.text


def test_viewer_role_cannot_add_workspace_member(client):
    viewer_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
            (viewer_id, "viewer@example.com", "Viewer", timestamp),
        )
        conn.execute(
            "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'viewer', ?)",
            (viewer_id, DEFAULT_WORKSPACE_ID, timestamp),
        )

    client.cookies.set("anydatas_user_id", viewer_id)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    response = client.post(
        "/workspace/members",
        data={"email": "new@example.com", "name": "New", "role": "analyst"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    with connect() as conn:
        member = conn.execute("SELECT * FROM users WHERE email = 'new@example.com'").fetchone()
    assert member is None


def test_viewer_role_cannot_update_workspace_quotas(client):
    viewer_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
            (viewer_id, "quota-viewer@example.com", "Quota Viewer", timestamp),
        )
        conn.execute(
            "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'viewer', ?)",
            (viewer_id, DEFAULT_WORKSPACE_ID, timestamp),
        )

    client.cookies.set("anydatas_user_id", viewer_id)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    response = client.post(
        "/workspace/quotas",
        data={"max_data_sources": 1, "max_projects": 1, "max_schedules": 1, "max_reports": 1, "max_concurrent_runs": 1},
        follow_redirects=False,
    )

    assert response.status_code == 403
    with connect() as conn:
        quota = conn.execute("SELECT * FROM workspace_quotas WHERE workspace_id = ?", (DEFAULT_WORKSPACE_ID,)).fetchone()
    assert quota is None


def test_viewer_cannot_see_workspace_runtime_usage(client):
    viewer_id = uuid.uuid4().hex
    timestamp = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
            (viewer_id, "usage-viewer@example.com", "Usage Viewer", timestamp),
        )
        conn.execute(
            "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'viewer', ?)",
            (viewer_id, DEFAULT_WORKSPACE_ID, timestamp),
        )

    client.cookies.set("anydatas_user_id", viewer_id)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    response = client.get("/")

    assert response.status_code == 200
    assert 'id="run-usage"' not in response.text
    assert "Runtime Usage" not in response.text


def test_viewer_role_cannot_requeue_failed_notification_delivery(client):
    assert client.post(
        "/notification-channels",
        data={
            "name": "viewer retry email",
            "channel_type": "email",
            "destination": "ops@example.com",
            "secret_id": "",
            "event_types": "run.failed",
            "max_retries": 0,
        },
        follow_redirects=False,
    ).status_code == 303
    with connect() as conn:
        record_notification(
            conn,
            DEFAULT_WORKSPACE_ID,
            "run.failed",
            "Run failed: viewer retry",
            "The source query failed.",
            "error",
            "run",
            "viewer-retry-run",
        )
        delivery = conn.execute("SELECT * FROM notification_deliveries").fetchone()
        conn.execute("UPDATE notification_deliveries SET status = 'failed', attempt = 1 WHERE id = ?", (delivery["id"],))
        viewer_id = uuid.uuid4().hex
        timestamp = now_iso()
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
            (viewer_id, "delivery-viewer@example.com", "Delivery Viewer", timestamp),
        )
        conn.execute(
            "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'viewer', ?)",
            (viewer_id, DEFAULT_WORKSPACE_ID, timestamp),
        )

    client.cookies.set("anydatas_user_id", viewer_id)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    response = client.post(f"/notification-deliveries/{delivery['id']}/requeue", follow_redirects=False)

    assert response.status_code == 403
    with connect() as conn:
        unchanged = conn.execute("SELECT * FROM notification_deliveries WHERE id = ?", (delivery["id"],)).fetchone()
    assert unchanged["status"] == "failed"


def test_private_report_is_only_visible_to_its_creator_and_administrators(client, sample_csv_bytes):
    source_response = client.post(
        "/data-sources",
        data={"name": "report sales"},
        files={"file": ("sales.csv", sample_csv_bytes, "text/csv")},
        follow_redirects=False,
    )
    assert source_response.status_code == 303
    with connect() as conn:
        source = conn.execute("SELECT * FROM data_sources WHERE name = 'report sales'").fetchone()

    project_response = client.post(
        "/projects",
        data={
            "name": "shared reporting project",
            "language": "sql",
            "data_source_id": source["id"],
            "script": "SELECT * FROM data LIMIT 1;",
            "parameters_json": "{}",
        },
        follow_redirects=False,
    )
    assert project_response.status_code == 303
    with connect() as conn:
        project = conn.execute("SELECT * FROM projects WHERE name = 'shared reporting project'").fetchone()

    for email, name, role in (
        ("report-author@example.com", "Report Author", "analyst"),
        ("report-viewer@example.com", "Report Viewer", "viewer"),
    ):
        response = client.post(
            "/workspace/members",
            data={"email": email, "name": name, "role": role},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with connect() as conn:
        author = conn.execute("SELECT * FROM users WHERE email = 'report-author@example.com'").fetchone()
        viewer = conn.execute("SELECT * FROM users WHERE email = 'report-viewer@example.com'").fetchone()

    client.cookies.set("anydatas_user_id", author["id"])
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    run_response = client.post(f"/projects/{project['id']}/run", follow_redirects=False)
    assert run_response.status_code == 303
    report_response = client.post(
        "/reports",
        data={
            "project_id": project["id"],
            "title": "Private revenue report",
            "description": "Only its author can read this report.",
            "visibility": "private",
        },
        follow_redirects=False,
    )
    assert report_response.status_code == 303
    report_id = report_response.headers["location"].rsplit("/", 1)[-1]
    with connect() as conn:
        default_widget = conn.execute(
            "SELECT * FROM report_widgets WHERE report_id = ? ORDER BY position LIMIT 1",
            (report_id,),
        ).fetchone()
    assert client.get(report_response.headers["location"]).status_code == 200
    assert client.get(f"/reports/{report_id}/snapshot.csv").status_code == 200

    creator_update = client.post(
        f"/reports/{report_id}/visibility",
        data={"visibility": "private"},
        follow_redirects=False,
    )
    assert creator_update.status_code == 303
    with connect() as conn:
        record_notification(
            conn,
            DEFAULT_WORKSPACE_ID,
            "report.refresh_failed",
            "Report refresh failed: Private revenue report",
            "Private detail",
            "error",
            "report",
            report_id,
        )

    client.cookies.set("anydatas_user_id", viewer["id"])
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    viewer_home = client.get("/")
    assert viewer_home.status_code == 200
    assert "Private revenue report" not in viewer_home.text
    assert client.get(f"/reports/{report_id}").status_code == 404
    assert client.get(f"/reports/{report_id}/snapshot.csv").status_code == 404
    assert client.post(
        f"/reports/{report_id}/visibility",
        data={"visibility": "workspace"},
        follow_redirects=False,
    ).status_code == 403
    assert client.post(
        f"/reports/{report_id}/grants",
        data={"user_id": viewer["id"]},
        follow_redirects=False,
    ).status_code == 403
    assert client.post(f"/reports/{report_id}/subscriptions", follow_redirects=False).status_code == 404
    assert client.get("/api/notifications").json() == []
    viewer_audit_events = client.get("/api/audit-events").json()
    assert not any(event["resource_id"] == report_id for event in viewer_audit_events)

    client.cookies.set("anydatas_user_id", author["id"])
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    grant_response = client.post(
        f"/reports/{report_id}/grants",
        data={"user_id": viewer["id"]},
        follow_redirects=False,
    )
    assert grant_response.status_code == 303

    client.cookies.set("anydatas_user_id", viewer["id"])
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    assert "Private revenue report" in client.get("/").text
    granted_report = client.get(f"/reports/{report_id}")
    assert granted_report.status_code == 200
    assert "Refresh Snapshot" not in granted_report.text
    assert f'action="/reports/{report_id}/widgets"' not in granted_report.text
    assert client.post(
        f"/reports/{report_id}/widgets",
        data={"kind": "metric", "title": "Blocked metric", "aggregate": "row_count"},
        follow_redirects=False,
    ).status_code == 403
    assert client.post(
        f"/reports/{report_id}/widgets/{default_widget['id']}/delete",
        follow_redirects=False,
    ).status_code == 403
    assert client.post(
        f"/reports/{report_id}/widgets/{default_widget['id']}/layout",
        data={"width": "half", "direction": "up"},
        follow_redirects=False,
    ).status_code == 403
    assert client.post(
        f"/reports/{report_id}/widgets/reorder",
        data={"order_json": json.dumps([default_widget["id"]])},
        follow_redirects=False,
    ).status_code == 403
    assert client.get(f"/reports/{report_id}/snapshot.csv").status_code == 200
    assert client.post(f"/reports/{report_id}/subscriptions", follow_redirects=False).status_code == 303
    with connect() as conn:
        subscription = conn.execute(
            "SELECT * FROM report_subscriptions WHERE report_id = ? AND user_id = ?",
            (report_id, viewer["id"]),
        ).fetchone()
    assert subscription is not None
    assert client.get("/api/notifications").json()[0]["resource_id"] == report_id
    granted_audit_events = client.get("/api/audit-events").json()
    assert any(event["action"] == "report.access_granted" for event in granted_audit_events)
    assert client.post(
        f"/reports/{report_id}/grants/{viewer['id']}/delete",
        follow_redirects=False,
    ).status_code == 403

    client.cookies.set("anydatas_user_id", author["id"])
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    revoke_response = client.post(f"/reports/{report_id}/grants/{viewer['id']}/delete", follow_redirects=False)
    assert revoke_response.status_code == 303
    with connect() as conn:
        subscription = conn.execute(
            "SELECT * FROM report_subscriptions WHERE report_id = ? AND user_id = ?",
            (report_id, viewer["id"]),
        ).fetchone()
    assert subscription is None

    client.cookies.set("anydatas_user_id", viewer["id"])
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    assert client.get(f"/reports/{report_id}").status_code == 404
    assert client.get("/api/notifications").json() == []
    assert not any(event["resource_id"] == report_id for event in client.get("/api/audit-events").json())

    client.cookies.set("anydatas_user_id", DEFAULT_USER_ID)
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    owner_update = client.post(
        f"/reports/{report_id}/visibility",
        data={"visibility": "workspace"},
        follow_redirects=False,
    )
    assert owner_update.status_code == 303

    client.cookies.set("anydatas_user_id", viewer["id"])
    client.cookies.set("anydatas_workspace_id", DEFAULT_WORKSPACE_ID)
    assert "Private revenue report" in client.get("/").text
    viewer_report = client.get(f"/reports/{report_id}")
    assert viewer_report.status_code == 200
    assert "Refresh Snapshot" not in viewer_report.text
    assert client.get(f"/reports/{report_id}/snapshot.csv").status_code == 200
    assert client.get("/api/notifications").json()[0]["resource_id"] == report_id
    assert any(event["action"] == "report.visibility_updated" for event in client.get("/api/audit-events").json())
