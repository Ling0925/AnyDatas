from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request

from .db import DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID


ROLE_RANK = {
    "viewer": 0,
    "analyst": 1,
    "admin": 2,
    "owner": 3,
}
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 1024
PBKDF2_ITERATIONS = 600_000
PBKDF2_MAX_ITERATIONS = 2_000_000
SESSION_COOKIE_NAME = "anydatas_session"
LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW_MINUTES = 15
LOGIN_LOCK_MINUTES = 15
INVITATION_ROLES = {"admin", "analyst", "viewer"}
INVITATION_FAILURE_LIMIT = 5
API_TOKEN_SCOPES = {"read", "full"}
SERVICE_ACCOUNT_ROLES = {"analyst", "viewer"}


@dataclass(frozen=True)
class RequestContext:
    user_id: str
    user_email: str
    user_name: str
    workspace_id: str
    workspace_name: str
    role: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def password_auth_enabled() -> bool:
    mode = os.getenv("ANYDATAS_AUTH_MODE", "demo").strip().lower()
    if mode not in {"demo", "password"}:
        raise RuntimeError("ANYDATAS_AUTH_MODE must be demo or password.")
    return mode == "password"


def session_ttl_days() -> int:
    try:
        return max(int(os.getenv("ANYDATAS_SESSION_TTL_DAYS", "7")), 1)
    except ValueError:
        return 7


def secure_cookie_enabled() -> bool:
    return os.getenv("ANYDATAS_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}


def self_signup_enabled() -> bool:
    return os.getenv("ANYDATAS_ALLOW_SIGNUP", "0").strip().lower() in {"1", "true", "yes", "on"}


def password_reset_ttl_hours() -> int:
    try:
        return min(max(int(os.getenv("ANYDATAS_PASSWORD_RESET_TTL_HOURS", "1")), 1), 24)
    except ValueError:
        return 1


def invitation_ttl_days() -> int:
    try:
        return min(max(int(os.getenv("ANYDATAS_INVITATION_TTL_DAYS", "7")), 1), 30)
    except ValueError:
        return 7


def hash_password(password: str) -> str:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must be at most {PASSWORD_MAX_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=32)
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PBKDF2_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(derived).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str | None) -> bool:
    if len(password) > PASSWORD_MAX_LENGTH:
        hashlib.pbkdf2_hmac(
            "sha256",
            password[:PASSWORD_MAX_LENGTH].encode("utf-8"),
            b"anydatas-oversized-password",
            PBKDF2_ITERATIONS,
            dklen=32,
        )
        return False
    if not encoded:
        hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            b"anydatas-missing-password",
            PBKDF2_ITERATIONS,
            dklen=32,
        )
        return False
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        iteration_count = int(iterations)
        if iteration_count < 100_000 or iteration_count > PBKDF2_MAX_ITERATIONS:
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iteration_count,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_api_token(
    conn: sqlite3.Connection,
    user_id: str,
    workspace_id: str,
    name: str,
    expires_days: int,
    scope: str = "read",
) -> tuple[str, str]:
    normalized_name = name.strip()
    if not normalized_name or len(normalized_name) > 120:
        raise ValueError("API token name must be between 1 and 120 characters.")
    if expires_days < 1 or expires_days > 365:
        raise ValueError("API token expiry must be between 1 and 365 days.")
    normalized_scope = scope.strip().lower()
    if normalized_scope not in API_TOKEN_SCOPES:
        raise ValueError("API token scope must be read or full.")
    token_id = uuid.uuid4().hex
    token = f"anydatas_{secrets.token_urlsafe(32)}"
    timestamp = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO api_tokens (
            id, token_hash, user_id, workspace_id, name,
            scope, created_at, expires_at, last_used_at, revoked_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        """,
        (
            token_id,
            session_token_hash(token),
            user_id,
            workspace_id,
            normalized_name,
            normalized_scope,
            timestamp.isoformat(),
            (timestamp + timedelta(days=expires_days)).isoformat(),
        ),
    )
    return token_id, token


def create_service_account(
    conn: sqlite3.Connection,
    workspace_id: str,
    created_by_user_id: str,
    name: str,
    role: str,
    token_scope: str,
    expires_days: int,
) -> tuple[str, str, str]:
    normalized_name = name.strip()
    normalized_role = role.strip().lower()
    normalized_scope = token_scope.strip().lower()
    if not normalized_name or len(normalized_name) > 120:
        raise ValueError("Service account name must be between 1 and 120 characters.")
    if normalized_role not in SERVICE_ACCOUNT_ROLES:
        raise ValueError("Service account role must be analyst or viewer.")
    if normalized_scope not in API_TOKEN_SCOPES:
        raise ValueError("API token scope must be read or full.")
    if expires_days < 1 or expires_days > 365:
        raise ValueError("API token expiry must be between 1 and 365 days.")
    service_account_id = uuid.uuid4().hex
    user_id = uuid.uuid4().hex
    timestamp = now_iso()
    conn.execute(
        "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, NULL, ?)",
        (user_id, f"service-{service_account_id}@service.anydatas.invalid", normalized_name, timestamp),
    )
    conn.execute(
        "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, ?, ?)",
        (user_id, workspace_id, normalized_role, timestamp),
    )
    conn.execute(
        """
        INSERT INTO service_accounts (
            id, user_id, workspace_id, name, role, created_by_user_id, created_at, revoked_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (service_account_id, user_id, workspace_id, normalized_name, normalized_role, created_by_user_id, timestamp),
    )
    token_id, token = create_api_token(
        conn,
        user_id,
        workspace_id,
        "Initial credential",
        expires_days,
        normalized_scope,
    )
    return service_account_id, token_id, token


def login_attempt_key(email: str, client_identifier: str) -> str:
    return hashlib.sha256(f"{email}|{client_identifier}".encode("utf-8")).hexdigest()


def check_login_rate_limit(conn: sqlite3.Connection, key_hash: str, timestamp: datetime) -> None:
    attempt = conn.execute("SELECT * FROM auth_login_attempts WHERE key_hash = ?", (key_hash,)).fetchone()
    if attempt is None:
        return
    locked_until = datetime.fromisoformat(attempt["locked_until"]) if attempt["locked_until"] else None
    if locked_until and locked_until > timestamp:
        retry_after = max(int((locked_until - timestamp).total_seconds()), 1)
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    first_failed_at = datetime.fromisoformat(attempt["first_failed_at"])
    if first_failed_at + timedelta(minutes=LOGIN_WINDOW_MINUTES) <= timestamp:
        conn.execute("DELETE FROM auth_login_attempts WHERE key_hash = ?", (key_hash,))


def record_login_failure(conn: sqlite3.Connection, key_hash: str, timestamp: datetime) -> bool:
    attempt = conn.execute("SELECT * FROM auth_login_attempts WHERE key_hash = ?", (key_hash,)).fetchone()
    first_failed_at = timestamp
    failed_count = 1
    if attempt is not None:
        existing_first = datetime.fromisoformat(attempt["first_failed_at"])
        if existing_first + timedelta(minutes=LOGIN_WINDOW_MINUTES) > timestamp:
            first_failed_at = existing_first
            failed_count = int(attempt["failed_count"]) + 1
    locked_until = timestamp + timedelta(minutes=LOGIN_LOCK_MINUTES) if failed_count >= LOGIN_FAILURE_LIMIT else None
    conn.execute(
        """
        INSERT INTO auth_login_attempts (key_hash, failed_count, first_failed_at, locked_until)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key_hash) DO UPDATE SET
            failed_count = excluded.failed_count,
            first_failed_at = excluded.first_failed_at,
            locked_until = excluded.locked_until
        """,
        (
            key_hash,
            failed_count,
            first_failed_at.isoformat(),
            locked_until.isoformat() if locked_until else None,
        ),
    )
    return locked_until is not None


def create_session(conn: sqlite3.Connection, user_id: str, workspace_id: str) -> str:
    token = secrets.token_urlsafe(32)
    timestamp = datetime.now(timezone.utc)
    conn.execute("DELETE FROM auth_sessions WHERE datetime(expires_at) <= datetime('now')")
    conn.execute(
        """
        INSERT INTO auth_sessions (token_hash, user_id, workspace_id, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            session_token_hash(token),
            user_id,
            workspace_id,
            timestamp.isoformat(),
            (timestamp + timedelta(days=session_ttl_days())).isoformat(),
        ),
    )
    return token


def revoke_session(conn: sqlite3.Connection, token: str) -> None:
    if token:
        conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (session_token_hash(token),))


def create_password_reset_token(
    conn: sqlite3.Connection,
    user_id: str,
    workspace_id: str,
    created_by_user_id: str,
) -> tuple[str, str]:
    timestamp = datetime.now(timezone.utc)
    conn.execute(
        """
        UPDATE password_reset_tokens
        SET revoked_at = ?
        WHERE user_id = ? AND workspace_id = ? AND used_at IS NULL AND revoked_at IS NULL
        """,
        (timestamp.isoformat(), user_id, workspace_id),
    )
    reset_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO password_reset_tokens (
            id, token_hash, user_id, workspace_id, created_by_user_id,
            created_at, expires_at, used_at, revoked_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        """,
        (
            reset_id,
            session_token_hash(token),
            user_id,
            workspace_id,
            created_by_user_id,
            timestamp.isoformat(),
            (timestamp + timedelta(hours=password_reset_ttl_hours())).isoformat(),
        ),
    )
    return reset_id, token


def get_active_password_reset(conn: sqlite3.Connection, token: str):
    if not token:
        return None
    return conn.execute(
        """
        SELECT reset.*, user.email, user.name, workspace.name AS workspace_name
        FROM password_reset_tokens reset
        JOIN users user ON user.id = reset.user_id
        JOIN workspaces workspace ON workspace.id = reset.workspace_id
        WHERE reset.token_hash = ?
          AND reset.used_at IS NULL
          AND reset.revoked_at IS NULL
          AND datetime(reset.expires_at) > datetime('now')
        """,
        (session_token_hash(token),),
    ).fetchone()


def reset_password_with_token(
    conn: sqlite3.Connection,
    token: str,
    password: str,
) -> tuple[sqlite3.Row, int]:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    reset = get_active_password_reset(conn, token)
    if reset is None:
        raise HTTPException(status_code=404, detail="Password reset link is invalid or expired")
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    timestamp = now_iso()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, reset["user_id"]))
    conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (reset["user_id"],))
    revoked_tokens = conn.execute(
        "UPDATE api_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
        (timestamp, reset["user_id"]),
    ).rowcount
    conn.execute("UPDATE password_reset_tokens SET used_at = ? WHERE id = ?", (timestamp, reset["id"]))
    return reset, revoked_tokens


def create_workspace_invitation(
    conn: sqlite3.Connection,
    workspace_id: str,
    invited_by_user_id: str,
    email: str,
    name: str,
    role: str,
) -> tuple[str, str]:
    normalized_email = email.strip().lower()
    display_name = (name or normalized_email.split("@")[0]).strip() or normalized_email
    normalized_role = role.strip().lower()
    if not normalized_email or "@" not in normalized_email or len(normalized_email) > 320:
        raise ValueError("A valid email is required.")
    if normalized_role not in INVITATION_ROLES:
        raise ValueError("Invitation role must be admin, analyst, or viewer.")
    existing_member = conn.execute(
        """
        SELECT 1
        FROM users u
        JOIN memberships m ON m.user_id = u.id
        WHERE u.email = ? AND m.workspace_id = ?
        """,
        (normalized_email, workspace_id),
    ).fetchone()
    if existing_member is not None:
        raise ValueError("This user is already a workspace member.")
    timestamp = datetime.now(timezone.utc)
    conn.execute(
        """
        UPDATE workspace_invitations
        SET revoked_at = ?
        WHERE workspace_id = ? AND email = ? AND accepted_at IS NULL AND revoked_at IS NULL
        """,
        (timestamp.isoformat(), workspace_id, normalized_email),
    )
    invitation_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO workspace_invitations (
            id, token_hash, workspace_id, email, name, role, invited_by_user_id,
            failed_attempts, created_at, expires_at, accepted_at, revoked_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, NULL)
        """,
        (
            invitation_id,
            session_token_hash(token),
            workspace_id,
            normalized_email,
            display_name,
            normalized_role,
            invited_by_user_id,
            timestamp.isoformat(),
            (timestamp + timedelta(days=invitation_ttl_days())).isoformat(),
        ),
    )
    return invitation_id, token


def get_active_invitation(conn: sqlite3.Connection, token: str):
    if not token:
        return None
    return conn.execute(
        """
        SELECT
            invitation.*,
            workspace.name AS workspace_name,
            CASE WHEN user.password_hash IS NOT NULL THEN 1 ELSE 0 END AS existing_account
        FROM workspace_invitations invitation
        JOIN workspaces workspace ON workspace.id = invitation.workspace_id
        LEFT JOIN users user ON user.email = invitation.email
        WHERE invitation.token_hash = ?
          AND invitation.accepted_at IS NULL
          AND invitation.revoked_at IS NULL
          AND datetime(invitation.expires_at) > datetime('now')
        """,
        (session_token_hash(token),),
    ).fetchone()


def accept_workspace_invitation(
    conn: sqlite3.Connection,
    token: str,
    password: str,
) -> RequestContext:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    invitation = get_active_invitation(conn, token)
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation is invalid or expired")
    user = conn.execute("SELECT * FROM users WHERE email = ?", (invitation["email"],)).fetchone()
    if user is not None and user["password_hash"]:
        if not verify_password(password, user["password_hash"]):
            failed_attempts = int(invitation["failed_attempts"]) + 1
            conn.execute(
                "UPDATE workspace_invitations SET failed_attempts = ?, revoked_at = CASE WHEN ? >= ? THEN ? ELSE revoked_at END WHERE id = ?",
                (
                    failed_attempts,
                    failed_attempts,
                    INVITATION_FAILURE_LIMIT,
                    now_iso(),
                    invitation["id"],
                ),
            )
            raise HTTPException(status_code=401, detail="Invalid account password")
        user_id = user["id"]
    else:
        try:
            password_hash = hash_password(password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if user is not None:
            user_id = user["id"]
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        else:
            user_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, invitation["email"], invitation["name"], password_hash, now_iso()),
            )
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO memberships (user_id, workspace_id, role, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, workspace_id) DO UPDATE SET role = excluded.role
        """,
        (user_id, invitation["workspace_id"], invitation["role"], timestamp),
    )
    conn.execute("UPDATE workspace_invitations SET accepted_at = ? WHERE id = ?", (timestamp, invitation["id"]))
    row = conn.execute(
        """
        SELECT
            u.id AS user_id,
            u.email AS user_email,
            u.name AS user_name,
            w.id AS workspace_id,
            w.name AS workspace_name,
            m.role AS role
        FROM memberships m
        JOIN users u ON u.id = m.user_id
        JOIN workspaces w ON w.id = m.workspace_id
        WHERE u.id = ? AND w.id = ?
        """,
        (user_id, invitation["workspace_id"]),
    ).fetchone()
    return RequestContext(**dict(row))


def get_request_context(request: Request, conn: sqlite3.Connection) -> RequestContext:
    authorization = request.headers.get("authorization", "")
    scheme, _, bearer_token = authorization.partition(" ")
    if scheme.lower() == "bearer" and bearer_token:
        row = conn.execute(
            """
            SELECT
                token.id AS token_id,
                u.id AS user_id,
                u.email AS user_email,
                u.name AS user_name,
                w.id AS workspace_id,
                w.name AS workspace_name,
                m.role AS role,
                token.scope AS token_scope
            FROM api_tokens token
            JOIN users u ON u.id = token.user_id
            JOIN workspaces w ON w.id = token.workspace_id
            JOIN memberships m ON m.user_id = token.user_id AND m.workspace_id = token.workspace_id
            WHERE token.token_hash = ?
              AND token.revoked_at IS NULL
              AND datetime(token.expires_at) > datetime('now')
            """,
            (session_token_hash(bearer_token),),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=401,
                detail="API token is invalid or expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if row["token_scope"] == "read" and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            raise HTTPException(status_code=403, detail="API token requires full scope for write requests")
        conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now_iso(), row["token_id"]))
        return RequestContext(
            user_id=row["user_id"],
            user_email=row["user_email"],
            user_name=row["user_name"],
            workspace_id=row["workspace_id"],
            workspace_name=row["workspace_name"],
            role=row["role"],
        )

    if password_auth_enabled():
        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        if not token:
            raise HTTPException(status_code=401, detail="Authentication required")
        row = conn.execute(
            """
            SELECT
                u.id AS user_id,
                u.email AS user_email,
                u.name AS user_name,
                w.id AS workspace_id,
                w.name AS workspace_name,
                m.role AS role
            FROM auth_sessions session
            JOIN users u ON u.id = session.user_id
            JOIN workspaces w ON w.id = session.workspace_id
            JOIN memberships m ON m.user_id = session.user_id AND m.workspace_id = session.workspace_id
            WHERE session.token_hash = ?
              AND datetime(session.expires_at) > datetime('now')
            """,
            (session_token_hash(token),),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="Session is invalid or expired")
        return RequestContext(**dict(row))

    user_id = request.cookies.get("anydatas_user_id") or DEFAULT_USER_ID
    workspace_id = request.cookies.get("anydatas_workspace_id") or DEFAULT_WORKSPACE_ID

    row = conn.execute(
        """
        SELECT
            u.id AS user_id,
            u.email AS user_email,
            u.name AS user_name,
            w.id AS workspace_id,
            w.name AS workspace_name,
            m.role AS role
        FROM memberships m
        JOIN users u ON u.id = m.user_id
        JOIN workspaces w ON w.id = m.workspace_id
        WHERE u.id = ? AND w.id = ?
        """,
        (user_id, workspace_id),
    ).fetchone()
    if row is None and user_id != DEFAULT_USER_ID:
        row = conn.execute(
            """
            SELECT
                u.id AS user_id,
                u.email AS user_email,
                u.name AS user_name,
                w.id AS workspace_id,
                w.name AS workspace_name,
                m.role AS role
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            JOIN workspaces w ON w.id = m.workspace_id
            WHERE u.id = ?
            ORDER BY m.created_at ASC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT
                u.id AS user_id,
                u.email AS user_email,
                u.name AS user_name,
                w.id AS workspace_id,
                w.name AS workspace_name,
                m.role AS role
            FROM memberships m
            JOIN users u ON u.id = m.user_id
            JOIN workspaces w ON w.id = m.workspace_id
            WHERE u.id = ? AND w.id = ?
            """,
            (DEFAULT_USER_ID, DEFAULT_WORKSPACE_ID),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="Default workspace is not initialized")
    return RequestContext(**dict(row))


def require_role(context: RequestContext, minimum_role: str) -> None:
    if ROLE_RANK[context.role] < ROLE_RANK[minimum_role]:
        raise HTTPException(status_code=403, detail=f"{minimum_role} role required")


def get_or_create_login_identity(conn: sqlite3.Connection, email: str, name: Optional[str] = None) -> RequestContext:
    normalized_email = email.strip().lower()
    display_name = (name or normalized_email.split("@")[0]).strip() or normalized_email
    if not normalized_email or "@" not in normalized_email:
        raise HTTPException(status_code=400, detail="A valid email is required")

    user = conn.execute(
        """
        SELECT * FROM users
        WHERE email = ?
          AND NOT EXISTS (SELECT 1 FROM service_accounts WHERE service_accounts.user_id = users.id)
        """,
        (normalized_email,),
    ).fetchone()
    timestamp = now_iso()
    if user is None:
        user_id = uuid.uuid4().hex
        workspace_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
            (user_id, normalized_email, display_name, timestamp),
        )
        conn.execute(
            "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
            (workspace_id, f"{display_name} Workspace", timestamp),
        )
        conn.execute(
            "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'owner', ?)",
            (user_id, workspace_id, timestamp),
        )
    else:
        user_id = user["id"]
        membership = conn.execute(
            "SELECT workspace_id FROM memberships WHERE user_id = ? ORDER BY created_at ASC LIMIT 1",
            (user_id,),
        ).fetchone()
        if membership is None:
            workspace_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
                (workspace_id, f"{display_name} Workspace", timestamp),
            )
            conn.execute(
                "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'owner', ?)",
                (user_id, workspace_id, timestamp),
            )
        else:
            workspace_id = membership["workspace_id"]

    row = conn.execute(
        """
        SELECT
            u.id AS user_id,
            u.email AS user_email,
            u.name AS user_name,
            w.id AS workspace_id,
            w.name AS workspace_name,
            m.role AS role
        FROM memberships m
        JOIN users u ON u.id = m.user_id
        JOIN workspaces w ON w.id = m.workspace_id
        WHERE u.id = ? AND w.id = ?
        """,
        (user_id, workspace_id),
    ).fetchone()
    return RequestContext(**dict(row))


def register_password_identity(
    conn: sqlite3.Connection,
    email: str,
    name: str,
    password: str,
) -> RequestContext:
    normalized_email = email.strip().lower()
    normalized_name = name.strip()
    if not normalized_email or "@" not in normalized_email or len(normalized_email) > 320:
        raise ValueError("A valid email is required.")
    if not normalized_name or len(normalized_name) > 120:
        raise ValueError("Name must be between 1 and 120 characters.")
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (normalized_email,)).fetchone() is not None:
        raise ValueError("An account with this email already exists.")
    password_hash = hash_password(password)
    user_id = uuid.uuid4().hex
    workspace_id = uuid.uuid4().hex
    timestamp = now_iso()
    conn.execute(
        "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, normalized_email, normalized_name, password_hash, timestamp),
    )
    conn.execute(
        "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
        (workspace_id, f"{normalized_name} Workspace", timestamp),
    )
    conn.execute(
        "INSERT INTO memberships (user_id, workspace_id, role, created_at) VALUES (?, ?, 'owner', ?)",
        (user_id, workspace_id, timestamp),
    )
    return RequestContext(
        user_id=user_id,
        user_email=normalized_email,
        user_name=normalized_name,
        workspace_id=workspace_id,
        workspace_name=f"{normalized_name} Workspace",
        role="owner",
    )


def authenticate_password(
    conn: sqlite3.Connection,
    email: str,
    password: str,
    client_identifier: str = "unknown",
) -> RequestContext:
    normalized_email = email.strip().lower()
    attempt_key = login_attempt_key(normalized_email, client_identifier)
    timestamp = datetime.now(timezone.utc)
    check_login_rate_limit(conn, attempt_key, timestamp)
    user = conn.execute(
        """
        SELECT * FROM users
        WHERE email = ?
          AND NOT EXISTS (SELECT 1 FROM service_accounts WHERE service_accounts.user_id = users.id)
        """,
        (normalized_email,),
    ).fetchone()
    password_matches = verify_password(password, user["password_hash"] if user is not None else None)
    if user is None or not password_matches:
        locked = record_login_failure(conn, attempt_key, timestamp)
        if locked:
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Try again later.",
                headers={"Retry-After": str(LOGIN_LOCK_MINUTES * 60)},
            )
        raise HTTPException(status_code=401, detail="Invalid email or password")
    conn.execute("DELETE FROM auth_login_attempts WHERE key_hash = ?", (attempt_key,))
    membership = conn.execute(
        "SELECT workspace_id FROM memberships WHERE user_id = ? ORDER BY created_at ASC LIMIT 1",
        (user["id"],),
    ).fetchone()
    if membership is None:
        raise HTTPException(status_code=403, detail="User has no workspace membership")
    row = conn.execute(
        """
        SELECT
            u.id AS user_id,
            u.email AS user_email,
            u.name AS user_name,
            w.id AS workspace_id,
            w.name AS workspace_name,
            m.role AS role
        FROM memberships m
        JOIN users u ON u.id = m.user_id
        JOIN workspaces w ON w.id = m.workspace_id
        WHERE u.id = ? AND w.id = ?
        """,
        (user["id"], membership["workspace_id"]),
    ).fetchone()
    return RequestContext(**dict(row))


def add_workspace_member(
    conn: sqlite3.Connection,
    workspace_id: str,
    email: str,
    name: str,
    role: str,
) -> str:
    if role not in ROLE_RANK:
        raise HTTPException(status_code=400, detail="Unsupported role")
    normalized_email = email.strip().lower()
    display_name = (name or normalized_email.split("@")[0]).strip() or normalized_email
    if not normalized_email or "@" not in normalized_email:
        raise HTTPException(status_code=400, detail="A valid email is required")

    timestamp = now_iso()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (normalized_email,)).fetchone()
    if user is None:
        user_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?)",
            (user_id, normalized_email, display_name, timestamp),
        )
    else:
        user_id = user["id"]

    conn.execute(
        """
        INSERT INTO memberships (user_id, workspace_id, role, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, workspace_id) DO UPDATE SET role = excluded.role
        """,
        (user_id, workspace_id, role, timestamp),
    )
    return user_id
