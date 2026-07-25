#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth import hash_password  # noqa: E402


def default_data_dir() -> Path:
    return Path(os.getenv("ANYDATAS_DATA_DIR", str(ROOT / "var"))).expanduser().resolve()


def set_password(data_dir: Path, email: str, password: str) -> str:
    database_path = data_dir.expanduser().resolve() / "anydatas.sqlite3"
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {database_path}")
    normalized_email = email.strip().lower()
    with sqlite3.connect(database_path) as conn:
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT * FROM users WHERE email = ?", (normalized_email,)).fetchone()
        if user is None:
            raise ValueError(f"User does not exist: {normalized_email}")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "password_hash" not in columns:
            raise ValueError(
                "Database is not initialized for password authentication. Start the current application once first."
            )
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user["id"],))
    return normalized_email


def main() -> None:
    parser = argparse.ArgumentParser(description="Set an AnyDatas user password and revoke existing sessions.")
    parser.add_argument("email")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--password-env", help="Read the password from this environment variable instead of prompting")
    args = parser.parse_args()
    password = os.getenv(args.password_env, "") if args.password_env else getpass.getpass("New password: ")
    if args.password_env and not password:
        parser.error(f"Password environment variable is empty or missing: {args.password_env}")
    try:
        email = set_password(args.data_dir, args.email, password)
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    print(f"Password updated and existing sessions revoked for {email}.")


if __name__ == "__main__":
    main()
