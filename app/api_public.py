"""Public surface: health, subscription links, user portal and usage reporting.

Nothing here needs an admin session - a user only ever needs their own secret
token. The reporting endpoints are guarded by an API key instead.
"""

from __future__ import annotations

import base64
import io
import time
from typing import Any

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from . import config, db, deps, links, service

router = APIRouter(tags=["public"])


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


def _load(conn, token: str) -> tuple[dict, list[dict]]:
    user = service.get_user_by_token(conn, token)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "لینک نامعتبر است")
    # Reading a user is also the natural moment to roll the daily counter over.
    user = service.rollover_daily(conn, user)
    return user, deps.servers_of(user, deps.server_map(conn))


def _userinfo_header(user: dict) -> str:
    quota = user.get("quota_bytes")
    expires = user.get("expires_at")
    return (
        f"upload=0; download={int(user.get('used_bytes') or 0)}; "
        f"total={int(quota) if quota else 0}; "
        f"expire={int(expires) if expires else 0}"
    )


# --- Health ----------------------------------------------------------------


@router.get("/health")
def health():
    """Used by Railway's healthcheck."""
    try:
        with db.db() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:  # pragma: no cover - only when the volume is broken
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"database: {exc}")
    return {"status": "ok", "time": int(time.time()), "timezone": config.TIMEZONE_NAME}


# --- Subscription ----------------------------------------------------------


@router.get("/sub/{token}", response_class=Response)
def subscription(token: str, request: Request, format: str = "base64"):
    """The link the user pastes into v2rayNG / Nekobox / Clash / Hiddify.

    Always answers 200 with the `Subscription-Userinfo` header so clients can
    display remaining volume and expiry even when the config is blocked.
    """
    with db.db() as conn:
        user, servers = _load(conn, token)

    status_value = service.compute_status(user)
    bundle = links.build_links(user, servers)
    if status_value in service.BLOCKED_STATUSES:
        bundle = []

    body = ""
    if bundle:
        body = (
            links.subscription_text(bundle)
            if (format or "base64").lower() != "plain"
            else "\n".join(item["link"] for item in bundle)
        )

    # HTTP headers are latin-1 only, so the (possibly Persian) profile title is
    # sent using the base64: convention that v2rayN/Nekobox understand, and the
    # human readable status is exposed as a machine key only.
    title = f"{db.get_setting('brand', 'Panel')} - {user.get('name') or ''}"
    headers = {
        "Subscription-Userinfo": _userinfo_header(user),
        "Profile-Title": "base64:" + base64.b64encode(title.encode("utf-8")).decode("ascii"),
        "Profile-Update-Interval": "12",
        "X-Panel-Status": status_value,
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Access-Control-Allow-Origin": "*",
    }
    if status_value in service.BLOCKED_STATUSES:
        headers["X-Panel-Blocked"] = "1"
    return Response(content=body, media_type="text/plain; charset=utf-8", headers=headers)


@router.get("/sub/{token}/info")
def subscription_info(token: str, request: Request):
    with db.db() as conn:
        user, servers = _load(conn, token)
    return _portal_payload(user, servers, deps.client_base_url(request))


def _portal_payload(user: dict, servers: list[dict], base_url: str) -> dict[str, Any]:
    view = service.user_view(user, base_url=base_url, servers=servers)
    status_value = view["status"]
    for private in ("password", "uuid", "sub_token", "server_ids", "extra_configs"):
        view.pop(private, None)
    view["configs"] = [] if status_value in service.BLOCKED_STATUSES else view.get("configs", [])
    view.pop("configs_all", None)
    view["brand"] = db.get_setting("brand", "پنل مدیریت کانفیگ")
    view["support_url"] = db.get_setting("support_url", "")
    view["daily_enabled"] = user.get("daily_quota_bytes") is not None
    return view


# --- User portal -----------------------------------------------------------


@router.get("/api/portal/{token}")
def portal(token: str, request: Request):
    with db.db() as conn:
        user, servers = _load(conn, token)
    return _portal_payload(user, servers, deps.client_base_url(request))


@router.get("/api/portal/{token}/qr.svg")
def portal_qr(token: str, request: Request):
    with db.db() as conn:
        user, _ = _load(conn, token)
    url = f"{deps.client_base_url(request)}/sub/{user['sub_token']}"
    return Response(
        content=_qr_svg(url),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


# --- Machine-to-machine usage reporting ------------------------------------


class ReportItem(BaseModel):
    user: str
    used_bytes: int | None = None
    delta_bytes: int | None = None


class ReportBody(BaseModel):
    reports: list[ReportItem] | None = None
    user: str | None = None
    used_bytes: int | None = None
    delta_bytes: int | None = None


@router.get("/api/report/users", dependencies=[Depends(deps.require_api_key)])
def report_users():
    """Snapshot of every user so a node can sync before reporting traffic."""
    with db.db() as conn:
        mapping = deps.server_map(conn)
        items = []
        for user in service.list_users(conn):
            user = service.rollover_daily(conn, user)
            view = service.user_view(user, servers=deps.servers_of(user, mapping), include_links=False)
            items.append(
                {
                    "name": view["name"],
                    "uuid": view["uuid"],
                    "sub_token": view["sub_token"],
                    "status": view["status"],
                    "used_bytes": view["used_bytes"],
                    "quota_bytes": view["quota_bytes"],
                    "daily_used_bytes": view["daily_used_bytes"],
                    "daily_quota_bytes": view["daily_quota_bytes"],
                    "expires_at": view["expires_at"],
                }
            )
    return {"items": items, "count": len(items), "server_time": int(time.time())}


@router.post("/api/report", dependencies=[Depends(deps.require_api_key)])
def report_traffic(body: ReportBody):
    items = body.reports or []
    if not items and body.user:
        items = [
            ReportItem(user=body.user, used_bytes=body.used_bytes, delta_bytes=body.delta_bytes)
        ]
    if not items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "گزارشی ارسال نشده است")

    results = []
    with db.transaction() as conn:
        for item in items:
            user = service.find_user(conn, item.user)
            if user is None:
                results.append({"user": item.user, "ok": False, "error": "not_found"})
                continue
            try:
                updated = service.apply_report(
                    conn, user, used_bytes=item.used_bytes, delta_bytes=item.delta_bytes
                )
            except service.ValidationError as exc:
                results.append({"user": item.user, "ok": False, "error": str(exc)})
                continue
            results.append(
                {
                    "user": item.user,
                    "ok": True,
                    "used_bytes": int(updated.get("used_bytes") or 0),
                    "daily_used_bytes": int(updated.get("daily_used_bytes") or 0),
                    "status": service.compute_status(updated),
                }
            )
    return {"results": results}
