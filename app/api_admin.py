"""Admin API. Every route here requires a valid admin session cookie."""

from __future__ import annotations

import io
import json
import time
from typing import Any

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from . import config, db, deps, links, security, service

router = APIRouter(prefix="/api", tags=["admin"])

# --- Very small in-memory throttle for the login form ----------------------
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_WINDOW = 900
LOGIN_MAX_ATTEMPTS = 12


def _throttle(key: str) -> None:
    now = time.time()
    # Keep the table from growing forever if the panel is scanned from many IPs.
    if len(_LOGIN_ATTEMPTS) > 5000:
        for stale in [k for k, v in _LOGIN_ATTEMPTS.items() if not v or now - v[-1] > LOGIN_WINDOW]:
            _LOGIN_ATTEMPTS.pop(stale, None)
    hits = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < LOGIN_WINDOW]
    if len(hits) >= LOGIN_MAX_ATTEMPTS:
        wait = int(LOGIN_WINDOW - (now - hits[0]))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"تلاش‌های ناموفق زیاد است، {max(wait, 1)} ثانیه دیگر تلاش کنید",
        )
    _LOGIN_ATTEMPTS[key] = hits


def _throttle_fail(key: str) -> None:
    _LOGIN_ATTEMPTS.setdefault(key, []).append(time.time())


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --- Request models --------------------------------------------------------


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordBody(BaseModel):
    current_password: str = ""
    new_password: str = Field(min_length=6, max_length=256)


class ServerBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    protocol: str
    address: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class UserBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    note: str = ""
    duration_days: int = config.DEFAULT_DURATION_DAYS
    # 0 means unlimited, otherwise >= 2 GB
    quota_gb: float = 0
    # 0 means "no daily cap"
    daily_gb: float = 0
    server_ids: list[int] = Field(default_factory=list)
    extra_configs: str = ""
    activate_on_first_use: bool = False
    enabled: bool = True


class UserPatch(BaseModel):
    name: str | None = None
    note: str | None = None
    duration_days: int | None = None
    quota_gb: float | None = None
    daily_gb: float | None = None
    server_ids: list[int] | None = None
    extra_configs: str | None = None
    activate_on_first_use: bool | None = None
    enabled: bool | None = None


class TrafficBody(BaseModel):
    # Provide either an absolute total or an increment.
    used_bytes: int | None = None
    delta_bytes: int | None = None


class RenewBody(BaseModel):
    days: int | None = None
    reset_usage: bool = False


class SettingsBody(BaseModel):
    brand: str | None = None
    sub_base_url: str | None = None
    support_url: str | None = None
    default_duration_days: int | None = None
    default_quota_gb: float | None = None
    default_daily_gb: float | None = None
    allow_daily_limit: bool | None = None


# --- Helpers ---------------------------------------------------------------


def _public_server(server: dict) -> dict:
    return {
        "id": server["id"],
        "name": server["name"],
        "protocol": server["protocol"],
        "address": server["address"],
        "port": server["port"],
        "params": links.server_params(server),
        "enabled": bool(server.get("enabled")),
        "created_at": server.get("created_at"),
    }


def _user_payload(conn, user: dict, base_url: str, mapping: dict[int, dict]) -> dict:
    user = service.rollover_daily(conn, user)
    return service.user_view(
        user, base_url=base_url, servers=deps.servers_of(user, mapping)
    )


def _validate_url(value: str, label: str, *, strip_slash: bool = False) -> str:
    """Only absolute http(s) URLs may end up inside an href."""
    cleaned = (value or "").strip().rstrip("/") if strip_slash else (value or "").strip()
    if not cleaned:
        return ""
    if not cleaned.startswith(("http://", "https://")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{label} باید با http:// یا https:// شروع شود",
        )
    return cleaned


def _qr_svg(data: str) -> bytes:
    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2
    )
    code.add_data(data)
    code.make(fit=True)
    image = code.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue()


def _get_or_404(conn, user_id: int) -> dict:
    user = service.get_user(conn, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "کاربر پیدا نشد")
    return user


# --- Auth ------------------------------------------------------------------


@router.get("/auth/me")
def auth_me(request: Request, admin: dict = Depends(deps.current_admin)):
    return {
        "authenticated": True,
        "username": admin["username"],
        "admin_path": config.ADMIN_PATH,
        "setup_required": False,
    }


@router.get("/auth/status")
def auth_status(request: Request):
    """Public probe used by the login screen to decide what to show."""
    setup_required = db.admin_count() == 0
    authenticated = False
    username = None
    if not setup_required:
        try:
            admin = deps.current_admin(request)
            authenticated, username = True, admin["username"]
        except HTTPException:
            pass
    return {
        "setup_required": setup_required,
        "authenticated": authenticated,
        "username": username,
        "brand": db.get_setting("brand", "پنل مدیریت کانفیگ"),
        "admin_path": config.ADMIN_PATH,
    }


@router.post("/auth/setup")
def auth_setup(body: LoginBody, request: Request, response: Response):
    """First-run only: create the very first admin account."""
    if db.admin_count() > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "مدیر از قبل ساخته شده است")
    if len(body.password) < 6:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "رمز عبور حداقل ۶ کاراکتر باشد")
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO admins(username, password_hash, created_at) VALUES(?,?,?)",
            (body.username.strip(), security.hash_password(body.password), int(time.time())),
        )
        admin_id = int(cursor.lastrowid)
    return _start_session(response, request, admin_id, body.username.strip())


@router.post("/auth/login")
def auth_login(body: LoginBody, request: Request, response: Response):
    key = _client_ip(request)
    _throttle(key)
    with db.db() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash FROM admins WHERE username = ?",
            (body.username.strip(),),
        ).fetchone()
    if row is None or not security.verify_password(body.password, row["password_hash"]):
        _throttle_fail(key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "نام کاربری یا رمز عبور اشتباه است")
    _LOGIN_ATTEMPTS.pop(key, None)
    return _start_session(response, request, int(row["id"]), row["username"])


def _start_session(response: Response, request: Request, admin_id: int, username: str) -> dict:
    token = security.new_token(32)
    now = int(time.time())
    with db.db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT INTO sessions(token_hash, admin_id, created_at, expires_at, user_agent)"
            " VALUES(?,?,?,?,?)",
            (
                security.token_fingerprint(token),
                admin_id,
                now,
                security.session_expiry(now),
                request.headers.get("user-agent", "")[:255],
            ),
        )
    response.set_cookie(
        config.SESSION_COOKIE,
        token,
        max_age=config.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=deps.is_secure_request(request),
        path="/",
    )
    return {"authenticated": True, "username": username}


@router.post("/auth/logout")
def auth_logout(request: Request, response: Response):
    token = request.cookies.get(config.SESSION_COOKIE)
    if token:
        with db.db() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?",
                (security.token_fingerprint(token),),
            )
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return {"authenticated": False}


@router.post("/auth/password")
def auth_password(body: PasswordBody, admin: dict = Depends(deps.current_admin)):
    with db.db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM admins WHERE id = ?", (admin["id"],)
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "مدیر پیدا نشد")
    if not security.verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "رمز عبور فعلی اشتباه است")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE admins SET password_hash = ? WHERE id = ?",
            (security.hash_password(body.new_password), admin["id"]),
        )
        conn.execute("DELETE FROM sessions WHERE admin_id = ?", (admin["id"],))
    return {"ok": True, "message": "رمز عبور تغییر کرد، دوباره وارد شوید"}


# --- Servers ---------------------------------------------------------------


@router.get("/servers")
def list_servers(admin: dict = Depends(deps.current_admin)):
    with db.db() as conn:
        rows = conn.execute("SELECT * FROM servers ORDER BY id").fetchall()
    return {"items": [_public_server(dict(r)) for r in rows]}


def _validate_protocol(protocol: str) -> str:
    value = (protocol or "").strip().lower()
    if value not in links.PROTOCOLS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "پروتکل باید یکی از این‌ها باشد: " + ", ".join(links.PROTOCOLS),
        )
    return value


@router.post("/servers", status_code=201)
def create_server(body: ServerBody, admin: dict = Depends(deps.current_admin)):
    with db.db() as conn:
        cursor = conn.execute(
            "INSERT INTO servers(name, protocol, address, port, params, enabled, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                body.name.strip(),
                _validate_protocol(body.protocol),
                body.address.strip(),
                body.port,
                json.dumps(body.params or {}, ensure_ascii=False),
                1 if body.enabled else 0,
                int(time.time()),
            ),
        )
        row = conn.execute("SELECT * FROM servers WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _public_server(dict(row))


@router.put("/servers/{server_id}")
def update_server(server_id: int, body: ServerBody, admin: dict = Depends(deps.current_admin)):
    with db.db() as conn:
        if not conn.execute("SELECT 1 FROM servers WHERE id = ?", (server_id,)).fetchone():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "سرور پیدا نشد")
        conn.execute(
            "UPDATE servers SET name=?, protocol=?, address=?, port=?, params=?, enabled=?"
            " WHERE id=?",
            (
                body.name.strip(),
                _validate_protocol(body.protocol),
                body.address.strip(),
                body.port,
                json.dumps(body.params or {}, ensure_ascii=False),
                1 if body.enabled else 0,
                server_id,
            ),
        )
        row = conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
    return _public_server(dict(row))


@router.delete("/servers/{server_id}")
def delete_server(server_id: int, admin: dict = Depends(deps.current_admin)):
    with db.db() as conn:
        conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))
    return {"ok": True}


# --- Users -----------------------------------------------------------------


@router.get("/users")
def list_users(
    request: Request,
    search: str = "",
    status_filter: str = "",
    admin: dict = Depends(deps.current_admin),
):
    base = deps.client_base_url(request)
    with db.db() as conn:
        mapping = deps.server_map(conn)
        items = [_user_payload(conn, u, base, mapping) for u in service.list_users(conn)]

    needle = (search or "").strip().lower()
    if needle:
        items = [
            i
            for i in items
            if needle in i["name"].lower()
            or needle in i["note"].lower()
            or needle in i["sub_token"].lower()
            or needle in i["uuid"].lower()
        ]
    if status_filter:
        wanted = {s.strip() for s in status_filter.split(",") if s.strip()}
        items = [i for i in items if i["status"] in wanted]
    return {"items": items, "count": len(items)}


@router.post("/users", status_code=201)
def create_user(body: UserBody, request: Request, admin: dict = Depends(deps.current_admin)):
    with db.transaction() as conn:
        user = service.create_user(
            conn,
            name=body.name,
            note=body.note,
            duration_days=body.duration_days,
            quota_gb=body.quota_gb,
            daily_gb=body.daily_gb,
            server_ids=body.server_ids,
            extra_configs=body.extra_configs,
            activate_on_first_use=body.activate_on_first_use,
            enabled=body.enabled,
        )
        mapping = deps.server_map(conn)
        payload = service.user_view(
            user,
            base_url=deps.client_base_url(request),
            servers=deps.servers_of(user, mapping),
        )
    return payload


@router.get("/users/{user_id}")
def get_user(user_id: int, request: Request, admin: dict = Depends(deps.current_admin)):
    with db.db() as conn:
        user = _get_or_404(conn, user_id)
        mapping = deps.server_map(conn)
        return _user_payload(conn, user, deps.client_base_url(request), mapping)


@router.patch("/users/{user_id}")
def patch_user(
    user_id: int,
    body: UserPatch,
    request: Request,
    admin: dict = Depends(deps.current_admin),
):
    changes = body.model_dump(exclude_unset=True)
    with db.transaction() as conn:
        _get_or_404(conn, user_id)
        user = service.update_user(conn, user_id, changes)
        mapping = deps.server_map(conn)
        return service.user_view(
            user,
            base_url=deps.client_base_url(request),
            servers=deps.servers_of(user, mapping),
        )


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(deps.current_admin)):
    with db.db() as conn:
        _get_or_404(conn, user_id)
        service.delete_user(conn, user_id)
    return {"ok": True}


@router.post("/users/{user_id}/reset-usage")
def user_reset_usage(user_id: int, request: Request, admin: dict = Depends(deps.current_admin)):
    with db.transaction() as conn:
        _get_or_404(conn, user_id)
        user = service.reset_usage(conn, user_id)
        mapping = deps.server_map(conn)
        return service.user_view(
            user,
            base_url=deps.client_base_url(request),
            servers=deps.servers_of(user, mapping),
        )


@router.post("/users/{user_id}/reset-link")
def user_reset_link(user_id: int, request: Request, admin: dict = Depends(deps.current_admin)):
    with db.transaction() as conn:
        _get_or_404(conn, user_id)
        user = service.reset_sub_token(conn, user_id)
        mapping = deps.server_map(conn)
        return service.user_view(
            user,
            base_url=deps.client_base_url(request),
            servers=deps.servers_of(user, mapping),
        )


@router.post("/users/{user_id}/renew")
def user_renew(
    user_id: int, body: RenewBody, request: Request, admin: dict = Depends(deps.current_admin)
):
    with db.transaction() as conn:
        _get_or_404(conn, user_id)
        if body.reset_usage:
            service.reset_usage(conn, user_id)
        user = service.renew_user(conn, user_id, body.days)
        mapping = deps.server_map(conn)
        return service.user_view(
            user,
            base_url=deps.client_base_url(request),
            servers=deps.servers_of(user, mapping),
        )


@router.post("/users/{user_id}/traffic")
def user_traffic(
    user_id: int,
    body: TrafficBody,
    request: Request,
    admin: dict = Depends(deps.current_admin),
):
    if body.used_bytes is None and body.delta_bytes is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "used_bytes یا delta_bytes لازم است")
    with db.transaction() as conn:
        user = _get_or_404(conn, user_id)
        if body.used_bytes is not None:
            user = service.set_usage(conn, user_id, body.used_bytes)
        else:
            user = service.apply_report(conn, user, delta_bytes=body.delta_bytes)
        mapping = deps.server_map(conn)
        return service.user_view(
            user,
            base_url=deps.client_base_url(request),
            servers=deps.servers_of(user, mapping),
        )


@router.get("/users/{user_id}/configs")
def user_configs(user_id: int, request: Request, admin: dict = Depends(deps.current_admin)):
    with db.db() as conn:
        user = _get_or_404(conn, user_id)
        mapping = deps.server_map(conn)
        user = service.rollover_daily(conn, user)
        view = service.user_view(
            user,
            base_url=deps.client_base_url(request),
            servers=deps.servers_of(user, mapping),
            include_links=False,
        )
        return {
            "user": {"id": view["id"], "name": view["name"], "status": view["status"]},
            "configs": links.build_links(user, deps.servers_of(user, mapping)),
            "sub_url": view["sub_url"],
            "portal_url": view["portal_url"],
            "subscription_text": links.subscription_text(
                links.build_links(user, deps.servers_of(user, mapping))
            ),
        }


@router.get("/users/{user_id}/qr.svg")
def user_qr(user_id: int, request: Request, admin: dict = Depends(deps.current_admin)):
    with db.db() as conn:
        user = _get_or_404(conn, user_id)
    url = f"{deps.client_base_url(request)}/sub/{user['sub_token']}"
    return Response(content=_qr_svg(url), media_type="image/svg+xml")


# --- Dashboard / settings --------------------------------------------------


@router.get("/stats")
def stats(request: Request, days: int = 14, admin: dict = Depends(deps.current_admin)):
    with db.db() as conn:
        data = service.panel_stats(conn)
        data["series"] = service.usage_series(conn, days)
    return data


@router.get("/settings")
def read_settings(admin: dict = Depends(deps.current_admin)):
    values = db.get_settings()
    values.pop("api_key", None)
    return values


@router.put("/settings")
def write_settings(body: SettingsBody, admin: dict = Depends(deps.current_admin)):
    changes: dict[str, Any] = {}
    raw = body.model_dump(exclude_unset=True)
    if "brand" in raw:
        changes["brand"] = str(raw["brand"] or "").strip() or "پنل مدیریت کانفیگ"
    if "sub_base_url" in raw:
        changes["sub_base_url"] = _validate_url(
            str(raw["sub_base_url"] or ""), "آدرس عمومی", strip_slash=True
        )
    if "support_url" in raw:
        changes["support_url"] = _validate_url(str(raw["support_url"] or ""), "لینک پشتیبانی")
    if "allow_daily_limit" in raw:
        changes["allow_daily_limit"] = "1" if raw["allow_daily_limit"] else "0"
    if "default_duration_days" in raw and raw["default_duration_days"] is not None:
        days = service.validate_duration_days(raw["default_duration_days"])
        changes["default_duration_days"] = str(days)
    if "default_quota_gb" in raw and raw["default_quota_gb"] is not None:
        quota = service.validate_quota_gb(raw["default_quota_gb"])
        changes["default_quota_gb"] = str(
            0 if quota is None else round(quota / config.GB, 2)
        )
    if "default_daily_gb" in raw and raw["default_daily_gb"] is not None:
        daily = service.validate_daily_gb(raw["default_daily_gb"])
        changes["default_daily_gb"] = str(
            0 if daily is None else round(daily / config.GB, 2)
        )
    if changes:
        db.set_settings(changes)
    values = db.get_settings()
    values.pop("api_key", None)
    return values


@router.get("/settings/api-key")
def read_api_key(admin: dict = Depends(deps.current_admin)):
    return {"api_key": db.get_setting("api_key")}


@router.post("/settings/api-key/rotate")
def rotate_api_key(admin: dict = Depends(deps.current_admin)):
    value = security.new_token(32)
    db.set_settings({"api_key": value})
    return {"api_key": value}


@router.get("/export")
def export_data(admin: dict = Depends(deps.current_admin)):
    """Full JSON dump so the operator can back up / migrate."""
    with db.db() as conn:
        users = [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY id").fetchall()]
        servers = [dict(r) for r in conn.execute("SELECT * FROM servers ORDER BY id").fetchall()]
        history = [
            dict(r)
            for r in conn.execute(
                "SELECT user_id, day, bytes FROM usage_days ORDER BY day"
            ).fetchall()
        ]
    return {
        "exported_at": int(time.time()),
        "timezone": config.TIMEZONE_NAME,
        "servers": servers,
        "users": users,
        "usage_days": history,
    }
