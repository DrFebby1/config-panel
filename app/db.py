"""SQLite access layer: connections, schema and lightweight migrations.

One connection per unit of work (a request) with WAL enabled, which is safe
under uvicorn's threadpool and avoids sharing connections across threads.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import config, security

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT    PRIMARY KEY,
    admin_id   INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    user_agent TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS servers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    protocol   TEXT    NOT NULL,
    address    TEXT    NOT NULL,
    port       INTEGER NOT NULL,
    params     TEXT    NOT NULL DEFAULT '{}',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT    NOT NULL,
    note                 TEXT    NOT NULL DEFAULT '',
    uuid                 TEXT    NOT NULL UNIQUE,
    password             TEXT    NOT NULL DEFAULT '',
    sub_token            TEXT    NOT NULL UNIQUE,
    duration_days        INTEGER NOT NULL DEFAULT 30,
    quota_bytes          INTEGER,
    daily_quota_bytes    INTEGER,
    used_bytes           INTEGER NOT NULL DEFAULT 0,
    daily_used_bytes     INTEGER NOT NULL DEFAULT 0,
    daily_reset_on       TEXT,
    enabled              INTEGER NOT NULL DEFAULT 1,
    activate_on_first_use INTEGER NOT NULL DEFAULT 0,
    activated_at         INTEGER,
    expires_at           INTEGER,
    created_at           INTEGER NOT NULL,
    last_online_at       INTEGER,
    server_ids           TEXT    NOT NULL DEFAULT '[]',
    extra_configs        TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS usage_days (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day     TEXT    NOT NULL,
    bytes   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_days_day ON usage_days(day);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
"""

# Columns added after the first release. Applied on boot when missing so an
# existing database keeps working across upgrades.
_MIGRATIONS: list[tuple[str, str, str]] = []

SETTING_DEFAULTS: dict[str, str] = {
    "brand": "پنل مدیریت کانفیگ",
    "sub_base_url": "",
    "support_url": "",
    "default_duration_days": str(config.DEFAULT_DURATION_DAYS),
    "default_quota_gb": str(config.DEFAULT_QUOTA_BYTES // config.GB),
    "default_daily_gb": "0",
    "allow_daily_limit": "1",
}


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success and always close."""
    conn = connect()
    try:
        yield conn
    except Exception:
        # isolation_level=None means autocommit; an explicit transaction is only
        # open if a caller issued BEGIN, so only roll back in that case.
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Explicit write transaction so multi-statement updates stay atomic."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def init_db() -> None:
    """Create tables, apply migrations, seed settings and the bootstrap admin."""
    with db() as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        for key, value in SETTING_DEFAULTS.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                (key, value),
            )
        if not conn.execute("SELECT 1 FROM settings WHERE key='api_key'").fetchone():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('api_key', ?)",
                (security.new_token(32),),
            )
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
    bootstrap_admin()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def bootstrap_admin() -> None:
    """Create the admin from env vars, or reset its password when asked."""
    if not config.ADMIN_USERNAME or not config.ADMIN_PASSWORD:
        return
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM admins WHERE username = ?", (config.ADMIN_USERNAME,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO admins(username, password_hash, created_at) VALUES(?,?,?)",
                (
                    config.ADMIN_USERNAME,
                    security.hash_password(config.ADMIN_PASSWORD),
                    int(time.time()),
                ),
            )
        elif config.ADMIN_PASSWORD_RESET:
            conn.execute(
                "UPDATE admins SET password_hash = ? WHERE id = ?",
                (security.hash_password(config.ADMIN_PASSWORD), row["id"]),
            )


def admin_count() -> int:
    with db() as conn:
        return int(conn.execute("SELECT COUNT(*) AS c FROM admins").fetchone()["c"])


# --- Settings --------------------------------------------------------------


def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_settings() -> dict[str, str]:
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    merged = dict(SETTING_DEFAULTS)
    merged.update({r["key"]: r["value"] for r in rows})
    return merged


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def set_settings(values: dict[str, Any]) -> None:
    with db() as conn:
        for key, value in values.items():
            set_setting(conn, key, value)


def json_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
