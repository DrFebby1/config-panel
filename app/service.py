"""Business logic: user lifecycle, quota accounting, daily limits and status.

Terminology used throughout:
  * quota_bytes        - total allowance. None means unlimited.
  * daily_quota_bytes  - per-day allowance, reset at midnight in config.TIMEZONE.
                         None means "no daily cap".
  * duration_days      - lifetime of the config, 1..30 days.
  * used_bytes         - total traffic consumed so far.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid as uuid_lib
from datetime import datetime, timedelta
from typing import Any, Iterable

from . import config, db, links, security

DAY = 86400

STATUS_LABELS = {
    "pending": "در انتظار اولین اتصال",
    "active": "فعال",
    "expired": "منقضی شده",
    "exhausted": "حجم تمام شده",
    "daily_limited": "سقف روزانه پر شده",
    "disabled": "غیرفعال",
}

# Statuses that must not receive working configs / subscription content.
BLOCKED_STATUSES = {"expired", "exhausted", "daily_limited", "disabled"}


class ValidationError(ValueError):
    """Raised for user input that fails validation; mapped to HTTP 400."""


# --- Time helpers ----------------------------------------------------------


def now() -> int:
    return int(time.time())


def local_day(ts: int | None = None) -> str:
    """Calendar day in the configured timezone, e.g. '2026-09-25'."""
    moment = datetime.fromtimestamp(ts or time.time(), tz=config.TIMEZONE)
    return moment.strftime("%Y-%m-%d")


def local_date(ts: int | None = None) -> str:
    """Human readable local timestamp used in the UI."""
    moment = datetime.fromtimestamp(ts or time.time(), tz=config.TIMEZONE)
    return moment.strftime("%Y-%m-%d %H:%M")


def day_offset(offset: int) -> str:
    moment = datetime.now(tz=config.TIMEZONE) + timedelta(days=offset)
    return moment.strftime("%Y-%m-%d")


# --- Validation ------------------------------------------------------------

MIN_DAILY_BYTES = 100 * config.MB


def validate_duration_days(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValidationError("مدت زمان باید یک عدد صحیح باشد")
    if days < config.MIN_DURATION_DAYS or days > config.MAX_DURATION_DAYS:
        raise ValidationError(
            f"مدت زمان باید بین {config.MIN_DURATION_DAYS} تا "
            f"{config.MAX_DURATION_DAYS} روز باشد"
        )
    return days


def gb_to_bytes(value: Any) -> int:
    try:
        gb = float(value)
    except (TypeError, ValueError):
        raise ValidationError("حجم باید عدد باشد")
    if math.isnan(gb) or math.isinf(gb):
        raise ValidationError("حجم نامعتبر است")
    return int(round(gb * config.GB))


def validate_quota_gb(value: Any) -> int | None:
    """0 (or None/empty) means unlimited; otherwise at least 2 GB."""
    if value is None or value == "":
        return None
    size = gb_to_bytes(value)
    if size <= 0:
        return None
    if size < config.MIN_QUOTA_BYTES:
        raise ValidationError(
            f"حداقل حجم مجاز {config.MIN_QUOTA_BYTES // config.GB} گیگابایت است "
            "یا باید «بی‌نهایت» انتخاب شود"
        )
    return size


def validate_daily_gb(value: Any) -> int | None:
    """0 (or None/empty) means no daily cap; otherwise at least 100 MB."""
    if value is None or value == "":
        return None
    size = gb_to_bytes(value)
    if size <= 0:
        return None
    if size < MIN_DAILY_BYTES:
        raise ValidationError("حداقل محدودیت روزانه ۰.۱ گیگابایت است")
    return size


def validate_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValidationError("نام کاربر الزامی است")
    if len(name) > 64:
        raise ValidationError("نام کاربر حداکثر ۶۴ کاراکتر است")
    return name


# --- Reads -----------------------------------------------------------------


def get_user(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_token(conn: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    row = conn.execute("SELECT * FROM users WHERE sub_token = ?", (token,)).fetchone()
    return dict(row) if row else None


def find_user(conn: sqlite3.Connection, needle: str) -> dict[str, Any] | None:
    """Resolve a user by subscription token, UUID or exact name."""
    needle = (needle or "").strip()
    if not needle:
        return None
    for column in ("sub_token", "uuid", "name"):
        row = conn.execute(
            f"SELECT * FROM users WHERE {column} = ? LIMIT 1", (needle,)  # noqa: S608
        ).fetchone()
        if row:
            return dict(row)
    return None


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def servers_for_user(
    conn: sqlite3.Connection, user: dict[str, Any]
) -> list[dict[str, Any]]:
    ids = [int(i) for i in db.json_list(user.get("server_ids")) if str(i).isdigit()]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM servers WHERE id IN ({placeholders})", ids  # noqa: S608
    ).fetchall()
    order = {sid: index for index, sid in enumerate(ids)}
    result = [dict(r) for r in rows]
    result.sort(key=lambda s: order.get(int(s["id"]), 999))
    return result


# --- Status ----------------------------------------------------------------


def compute_status(user: dict[str, Any], at: int | None = None) -> str:
    at = at or now()
    if not user.get("enabled"):
        return "disabled"
    expires_at = user.get("expires_at")
    if expires_at is None:
        return "pending"
    if at >= int(expires_at):
        return "expired"
    quota = user.get("quota_bytes")
    if quota and int(user.get("used_bytes") or 0) >= int(quota):
        return "exhausted"
    daily = user.get("daily_quota_bytes")
    if daily and int(user.get("daily_used_bytes") or 0) >= int(daily):
        return "daily_limited"
    return "active"


def _day_counters(user: dict[str, Any], at: int | None = None) -> tuple[int, int, int]:
    """Return (days_total, days_used, days_left)."""
    at = at or now()
    total = int(user.get("duration_days") or 0)
    activated_at = user.get("activated_at")
    expires_at = user.get("expires_at")
    if activated_at is None or expires_at is None:
        return total, 0, total
    seconds_left = int(expires_at) - at
    days_left = max(0, math.ceil(seconds_left / DAY))
    days_left = min(days_left, total)
    return total, total - days_left, days_left


def user_view(
    user: dict[str, Any],
    *,
    base_url: str = "",
    servers: list[dict[str, Any]] | None = None,
    include_links: bool = True,
    at: int | None = None,
) -> dict[str, Any]:
    """Serialise a user row into everything the panels need to display."""
    at = at or now()
    status = compute_status(user, at)
    total_days, days_used, days_left = _day_counters(user, at)

    used = int(user.get("used_bytes") or 0)
    quota = user.get("quota_bytes")
    quota_int = int(quota) if quota else None
    remaining = max(0, quota_int - used) if quota_int is not None else None

    daily_used = int(user.get("daily_used_bytes") or 0)
    daily_quota = user.get("daily_quota_bytes")
    daily_quota_int = int(daily_quota) if daily_quota else None
    daily_remaining = (
        max(0, daily_quota_int - daily_used) if daily_quota_int is not None else None
    )

    token = user.get("sub_token") or ""
    base = base_url.rstrip("/")
    payload: dict[str, Any] = {
        "id": user["id"],
        "name": user.get("name") or "",
        "note": user.get("note") or "",
        "uuid": user.get("uuid") or "",
        "password": user.get("password") or "",
        "sub_token": token,
        "enabled": bool(user.get("enabled")),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "blocked": status in BLOCKED_STATUSES,
        "duration_days": total_days,
        "days_total": total_days,
        "days_used": days_used,
        "days_left": days_left,
        "quota_bytes": quota_int,
        "used_bytes": used,
        "remaining_bytes": remaining,
        "unlimited": quota_int is None,
        "usage_percent": (
            round(used * 100.0 / quota_int, 1) if quota_int else 0.0
        ),
        "daily_quota_bytes": daily_quota_int,
        "daily_used_bytes": daily_used,
        "daily_remaining_bytes": daily_remaining,
        "daily_unlimited": daily_quota_int is None,
        "daily_percent": (
            round(daily_used * 100.0 / daily_quota_int, 1) if daily_quota_int else 0.0
        ),
        "daily_reset_on": user.get("daily_reset_on"),
        "activate_on_first_use": bool(user.get("activate_on_first_use")),
        "created_at": user.get("created_at"),
        "activated_at": user.get("activated_at"),
        "expires_at": user.get("expires_at"),
        "last_online_at": user.get("last_online_at"),
        "created_at_display": local_date(user.get("created_at")),
        "expires_at_display": (
            local_date(user.get("expires_at")) if user.get("expires_at") else ""
        ),
        "last_online_display": (
            local_date(user.get("last_online_at")) if user.get("last_online_at") else ""
        ),
        "server_ids": [int(i) for i in db.json_list(user.get("server_ids")) if str(i).isdigit()],
        "extra_configs": user.get("extra_configs") or "",
        "sub_url": f"{base}/sub/{token}" if base else "",
        "portal_url": f"{base}/u/{token}" if base else "",
    }
    if include_links:
        active_servers = servers if servers is not None else []
        payload["configs"] = (
            [] if status in BLOCKED_STATUSES else links.build_links(user, active_servers)
        )
        if status in BLOCKED_STATUSES:
            # Still expose them for the admin so the config text is never lost.
            payload["configs_all"] = links.build_links(user, active_servers)
    return payload


# --- Writes ----------------------------------------------------------------


def _unique_token(conn: sqlite3.Connection, column: str, generator) -> str:
    for _ in range(10):
        candidate = generator()
        exists = conn.execute(
            f"SELECT 1 FROM users WHERE {column} = ?", (candidate,)  # noqa: S608
        ).fetchone()
        if not exists:
            return candidate
    raise RuntimeError(f"could not generate a unique {column}")


def create_user(
    conn: sqlite3.Connection,
    *,
    name: str,
    note: str = "",
    duration_days: Any = config.DEFAULT_DURATION_DAYS,
    quota_gb: Any = 0,
    daily_gb: Any = 0,
    server_ids: Iterable[Any] = (),
    extra_configs: str = "",
    activate_on_first_use: bool = False,
    enabled: bool = True,
    at: int | None = None,
) -> dict[str, Any]:
    at = at or now()
    clean_name = validate_name(name)
    days = validate_duration_days(duration_days)
    quota = validate_quota_gb(quota_gb)
    daily = validate_daily_gb(daily_gb)
    clean_servers = _clean_server_ids(conn, server_ids)

    user_uuid = str(uuid_lib.uuid4())
    token = _unique_token(conn, "sub_token", lambda: security.new_token(24))
    activated_at = None if activate_on_first_use else at
    expires_at = None if activate_on_first_use else at + days * DAY

    cursor = conn.execute(
        """
        INSERT INTO users (name, note, uuid, password, sub_token, duration_days,
                           quota_bytes, daily_quota_bytes, enabled,
                           activate_on_first_use, activated_at, expires_at,
                           created_at, daily_reset_on, server_ids, extra_configs)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            clean_name,
            str(note or "").strip(),
            user_uuid,
            security.new_token(16),
            token,
            days,
            quota,
            daily,
            1 if enabled else 0,
            1 if activate_on_first_use else 0,
            activated_at,
            expires_at,
            at,
            local_day(at),
            _dump_ids(clean_servers),
            str(extra_configs or ""),
        ),
    )
    return get_user(conn, int(cursor.lastrowid)) or {}


def _dump_ids(ids: list[int]) -> str:
    return json.dumps(ids)


def _clean_server_ids(conn: sqlite3.Connection, server_ids: Iterable[Any]) -> list[int]:
    cleaned: list[int] = []
    for raw in server_ids or []:
        try:
            sid = int(raw)
        except (TypeError, ValueError):
            continue
        if sid in cleaned:
            continue
        row = conn.execute("SELECT 1 FROM servers WHERE id = ?", (sid,)).fetchone()
        if row:
            cleaned.append(sid)
    return cleaned


def update_user(
    conn: sqlite3.Connection, user_id: int, changes: dict[str, Any], at: int | None = None
) -> dict[str, Any]:
    """Apply a partial update. Only whitelisted fields are honoured."""
    at = at or now()
    user = get_user(conn, user_id)
    if user is None:
        raise ValidationError("کاربر پیدا نشد")

    updates: dict[str, Any] = {}

    if "name" in changes:
        updates["name"] = validate_name(changes["name"])
    if "note" in changes:
        updates["note"] = str(changes["note"] or "").strip()
    if "extra_configs" in changes:
        updates["extra_configs"] = str(changes["extra_configs"] or "")
    if "enabled" in changes:
        updates["enabled"] = 1 if changes["enabled"] else 0
    if "server_ids" in changes:
        updates["server_ids"] = _dump_ids(_clean_server_ids(conn, changes["server_ids"]))

    if "duration_days" in changes:
        days = validate_duration_days(changes["duration_days"])
        updates["duration_days"] = days
        # Re-anchor expiry off the original activation so the new length takes
        # effect immediately without giving away extra days.
        if user.get("activated_at"):
            updates["expires_at"] = int(user["activated_at"]) + days * DAY
        elif not user.get("activate_on_first_use"):
            anchor = user.get("created_at") or at
            updates["activated_at"] = anchor
            updates["expires_at"] = anchor + days * DAY
        # Otherwise the config is still waiting for its first connection and
        # must stay pending - editing its length must not start the clock.

    if "quota_gb" in changes:
        updates["quota_bytes"] = validate_quota_gb(changes["quota_gb"])
    if "daily_gb" in changes:
        updates["daily_quota_bytes"] = validate_daily_gb(changes["daily_gb"])

    if "activate_on_first_use" in changes:
        flag = 1 if changes["activate_on_first_use"] else 0
        updates["activate_on_first_use"] = flag
        # Turning it on for an untouched user defers activation; turning it off
        # anchors the clock at creation time.
        if flag and not user.get("activated_at"):
            updates["activated_at"] = None
            updates["expires_at"] = None
        elif not flag and not user.get("activated_at"):
            anchor = user.get("created_at") or at
            updates["activated_at"] = anchor
            updates["expires_at"] = anchor + int(
                updates.get("duration_days", user["duration_days"])
            ) * DAY

    if not updates:
        return user

    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE users SET {assignments} WHERE id = ?",  # noqa: S608
        (*updates.values(), user_id),
    )
    return get_user(conn, user_id) or user


def renew_user(
    conn: sqlite3.Connection, user_id: int, days: int | None = None, at: int | None = None
) -> dict[str, Any]:
    """Restart the clock from now without touching consumed traffic."""
    at = at or now()
    user = get_user(conn, user_id)
    if user is None:
        raise ValidationError("کاربر پیدا نشد")
    span = validate_duration_days(days if days is not None else user["duration_days"])
    conn.execute(
        "UPDATE users SET duration_days = ?, activated_at = ?, expires_at = ?, enabled = 1"
        " WHERE id = ?",
        (span, at, at + span * DAY, user_id),
    )
    return get_user(conn, user_id) or user


def rollover_daily(
    conn: sqlite3.Connection, user: dict[str, Any], day: str | None = None
) -> dict[str, Any]:
    """Zero the daily counter when the local calendar day has changed."""
    day = day or local_day()
    if user.get("daily_reset_on") == day:
        return user
    conn.execute(
        "UPDATE users SET daily_used_bytes = 0, daily_reset_on = ? WHERE id = ?",
        (day, user["id"]),
    )
    user = dict(user)
    user["daily_used_bytes"] = 0
    user["daily_reset_on"] = day
    return user


def _bump(
    conn: sqlite3.Connection, user: dict[str, Any], delta: int, at: int
) -> dict[str, Any]:
    """Add traffic, updating totals, today's counter and the history table."""
    if delta == 0:
        return user
    activation_sql = ""
    params: list[Any] = []
    if user.get("activate_on_first_use") and not user.get("activated_at"):
        activated = at
        activation_sql = ", activated_at = ?, expires_at = ?"
        params.extend([activated, activated + int(user["duration_days"]) * DAY])
        user = dict(user)
        user["activated_at"] = activated
        user["expires_at"] = activated + int(user["duration_days"]) * DAY

    # Both counters are incremented in SQL so a stale in-memory copy can never
    # clobber traffic that a parallel request already recorded.
    conn.execute(
        "UPDATE users SET used_bytes = used_bytes + ?, daily_used_bytes = daily_used_bytes + ?,"
        " last_online_at = ?" + activation_sql + " WHERE id = ?",
        (delta, delta, at, *params, user["id"]),
    )
    conn.execute(
        "INSERT INTO usage_days(user_id, day, bytes) VALUES(?,?,?) "
        "ON CONFLICT(user_id, day) DO UPDATE SET bytes = bytes + excluded.bytes",
        (user["id"], local_day(at), delta),
    )
    return get_user(conn, user["id"]) or user


def apply_report(
    conn: sqlite3.Connection,
    user: dict[str, Any],
    *,
    used_bytes: Any = None,
    delta_bytes: Any = None,
    at: int | None = None,
) -> dict[str, Any]:
    """Traffic update coming from an external node.

    An absolute `used_bytes` lower than what we already recorded is ignored:
    nodes reset their counters on restart and we must never bill backwards.
    """
    at = at or now()
    user = rollover_daily(conn, user, local_day(at))

    if delta_bytes is not None:
        try:
            delta = int(delta_bytes)
        except (TypeError, ValueError):
            raise ValidationError("delta_bytes باید عدد باشد")
        return _bump(conn, user, max(0, delta), at)

    if used_bytes is None:
        raise ValidationError("used_bytes یا delta_bytes لازم است")
    try:
        reported = int(used_bytes)
    except (TypeError, ValueError):
        raise ValidationError("used_bytes باید عدد باشد")
    if reported < 0:
        raise ValidationError("used_bytes نمیتواند منفی باشد")

    delta = reported - int(user.get("used_bytes") or 0)
    if delta <= 0:
        # Nothing new to bill; still record that the node checked in.
        conn.execute("UPDATE users SET last_online_at = ? WHERE id = ?", (at, user["id"]))
        return get_user(conn, user["id"]) or user
    return _bump(conn, user, delta, at)


def set_usage(
    conn: sqlite3.Connection, user_id: int, used_bytes: Any, at: int | None = None
) -> dict[str, Any]:
    """Admin override: set the absolute total, keeping history in sync."""
    at = at or now()
    user = get_user(conn, user_id)
    if user is None:
        raise ValidationError("کاربر پیدا نشد")
    try:
        target = max(0, int(used_bytes))
    except (TypeError, ValueError):
        raise ValidationError("حجم باید عدد باشد")
    delta = target - int(user.get("used_bytes") or 0)
    if delta == 0:
        return user
    if delta < 0:
        # Lowering the total must also lower today's counter without going below 0.
        conn.execute(
            "UPDATE users SET used_bytes = ?, daily_used_bytes = MAX(0, daily_used_bytes + ?)"
            " WHERE id = ?",
            (target, delta, user_id),
        )
        conn.execute(
            "UPDATE usage_days SET bytes = MAX(0, bytes + ?) WHERE user_id = ? AND day = ?",
            (delta, user_id, local_day(at)),
        )
        return get_user(conn, user_id) or user
    user = rollover_daily(conn, user, local_day(at))
    return _bump(conn, user, delta, at)


def reset_usage(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """Wipe counters and history for a user (fresh billing period)."""
    conn.execute(
        "UPDATE users SET used_bytes = 0, daily_used_bytes = 0, last_online_at = NULL"
        " WHERE id = ?",
        (user_id,),
    )
    conn.execute("DELETE FROM usage_days WHERE user_id = ?", (user_id,))
    return get_user(conn, user_id) or {}


def reset_sub_token(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """Give the user a brand new private link (invalidates the old one)."""
    token = _unique_token(conn, "sub_token", lambda: security.new_token(24))
    conn.execute("UPDATE users SET sub_token = ? WHERE id = ?", (token, user_id))
    return get_user(conn, user_id) or {}


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# --- Aggregates ------------------------------------------------------------


def usage_series(conn: sqlite3.Connection, days: int = 14) -> list[dict[str, Any]]:
    """Traffic per day for the last N local days, gaps filled with zero."""
    days = max(1, min(int(days or 14), 90))
    span = [day_offset(-(days - 1 - index)) for index in range(days)]
    rows = conn.execute(
        "SELECT day, SUM(bytes) AS bytes FROM usage_days WHERE day >= ? GROUP BY day",
        (span[0],),
    ).fetchall()
    found = {r["day"]: int(r["bytes"] or 0) for r in rows}
    return [{"day": day, "bytes": found.get(day, 0)} for day in span]


def panel_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    users = list_users(conn)
    at = now()
    counters = {key: 0 for key in STATUS_LABELS}
    for user in users:
        rollover_daily(conn, user, local_day(at))
        counters[compute_status(user, at)] += 1
    # rollover_daily may have written, so re-read for accurate counters.
    users = list_users(conn)

    total_used = sum(int(u.get("used_bytes") or 0) for u in users)
    total_daily = sum(int(u.get("daily_used_bytes") or 0) for u in users)
    total_quota = sum(int(u["quota_bytes"]) for u in users if u.get("quota_bytes"))
    today = local_day(at)
    today_row = conn.execute(
        "SELECT SUM(bytes) AS bytes FROM usage_days WHERE day = ?", (today,)
    ).fetchone()
    expiring_soon = sum(
        1
        for u in users
        if compute_status(u, at) == "active"
        and u.get("expires_at")
        and int(u["expires_at"]) - at <= 3 * DAY
    )
    return {
        "users_total": len(users),
        "status_counts": counters,
        "total_used_bytes": total_used,
        "total_daily_used_bytes": total_daily,
        "total_quota_bytes": total_quota,
        "today_bytes": int(today_row["bytes"] or 0) if today_row else 0,
        "expiring_soon": expiring_soon,
        "server_time": local_date(at),
        "timezone": config.TIMEZONE_NAME,
    }
