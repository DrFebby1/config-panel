"""Shared FastAPI dependencies: admin authentication and URL resolution."""

from __future__ import annotations

import sqlite3
import time

from fastapi import HTTPException, Request, status

from . import config, db, security


def client_base_url(request: Request) -> str:
    """Public origin used to build links that are handed to end users.

    Priority: PUBLIC_BASE_URL env var, then the value saved in settings, then
    whatever host the request arrived on (correct behind Railway's proxy).
    """
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL
    saved = (db.get_setting("sub_base_url") or "").strip().rstrip("/")
    if saved:
        return saved

    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    scheme = (forwarded_proto.split(",")[0].strip() or request.url.scheme)
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{scheme}://{host.split(',')[0].strip()}"


def server_map(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute("SELECT * FROM servers ORDER BY id").fetchall()
    return {int(r["id"]): dict(r) for r in rows}


def servers_of(user: dict, mapping: dict[int, dict]) -> list[dict]:
    ids = [int(i) for i in db.json_list(user.get("server_ids")) if str(i).isdigit()]
    return [mapping[i] for i in ids if i in mapping]


def current_admin(request: Request) -> dict:
    """Resolve the signed-in admin from the session cookie or raise 401."""
    token = request.cookies.get(config.SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "برای دسترسی باید وارد شوید")
    fingerprint = security.token_fingerprint(token)
    with db.db() as conn:
        row = conn.execute(
            "SELECT s.expires_at, a.id, a.username FROM sessions s "
            "JOIN admins a ON a.id = s.admin_id WHERE s.token_hash = ?",
            (fingerprint,),
        ).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "نشست معتبر نیست")
        if int(row["expires_at"]) < int(time.time()):
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (fingerprint,))
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "نشست منقضی شده است")
        return {"id": row["id"], "username": row["username"], "token_hash": fingerprint}


def require_api_key(request: Request) -> None:
    """Guard the machine-to-machine reporting endpoints."""
    supplied = request.headers.get("x-api-key") or request.query_params.get("api_key", "")
    expected = db.get_setting("api_key")
    if not security.safe_equals(supplied, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key نامعتبر است")


def is_secure_request(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip() == "https"
