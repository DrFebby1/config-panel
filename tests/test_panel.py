"""End-to-end tests for the config panel.

Runs a real FastAPI app against a throwaway SQLite database and drives it over
HTTP, asserting the behaviour that actually matters: quota accounting, daily
limits, expiry, validation bounds and link generation.

    python tests/test_panel.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# --- Isolate the app on a temporary database BEFORE importing it -----------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="panel-test-"))
os.environ["DATA_DIR"] = str(TMP / "data")
os.environ.pop("ADMIN_USERNAME", None)
os.environ.pop("ADMIN_PASSWORD", None)
os.environ.pop("PUBLIC_BASE_URL", None)
os.environ["TZ_NAME"] = "Asia/Tehran"

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, service  # noqa: E402
from app.main import app  # noqa: E402

GB = 1024**3
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def b64decode(value: str) -> str:
    padded = value.strip() + "=" * (-len(value.strip()) % 4)
    return base64.b64decode(padded).decode("utf-8")


def main() -> int:
    with TestClient(app) as client:
        # --- health & first-run ------------------------------------------
        section("health & first run")
        response = client.get("/health")
        check("health returns ok", response.status_code == 200 and response.json()["status"] == "ok")

        status_body = client.get("/api/auth/status").json()
        check("fresh install asks for setup", status_body["setup_required"] is True)

        check(
            "admin API blocked before login",
            client.get("/api/users").status_code == 401,
        )

        response = client.post("/api/auth/setup", json={"username": "root", "password": "sup3rsecret"})
        check("setup creates the admin", response.status_code == 200, response.text)
        check("setup returns a session cookie", config.SESSION_COOKIE in client.cookies)

        check(
            "setup cannot run twice",
            client.post("/api/auth/setup", json={"username": "x", "password": "yyyyyy"}).status_code == 409,
        )

        # --- auth --------------------------------------------------------
        section("authentication")
        client.post("/api/auth/logout")
        check("logout clears the session", client.get("/api/users").status_code == 401)
        check(
            "wrong password rejected",
            client.post("/api/auth/login", json={"username": "root", "password": "nope"}).status_code == 401,
        )
        response = client.post(
            "/api/auth/login", json={"username": "root", "password": "sup3rsecret"}
        )
        check("login works", response.status_code == 200, response.text)
        check("admin identity exposed", client.get("/api/auth/me").json()["username"] == "root")

        # --- servers -----------------------------------------------------
        section("server / inbound definitions")
        response = client.post(
            "/api/servers",
            json={
                "name": "آلمان ۱",
                "protocol": "vless",
                "address": "de.example.com",
                "port": 443,
                "params": {
                    "network": "ws",
                    "security": "tls",
                    "path": "/rahnema",
                    "sni": "cdn.example.com",
                    "fingerprint": "chrome",
                },
            },
        )
        check("server created", response.status_code == 201, response.text)
        vless_server = response.json()

        response = client.post(
            "/api/servers",
            json={
                "name": "تروجان",
                "protocol": "trojan",
                "address": "tr.example.com",
                "port": 8443,
                "params": {"network": "tcp", "security": "tls", "sni": "tr.example.com"},
            },
        )
        trojan_server = response.json()
        check("second server created", response.status_code == 201)

        check(
            "bad protocol rejected",
            client.post(
                "/api/servers",
                json={"name": "x", "protocol": "wireguard", "address": "a", "port": 1},
            ).status_code == 400,
        )

        # --- user creation bounds ---------------------------------------
        section("validation bounds")
        base_user = {
            "name": "علی",
            "duration_days": 30,
            "quota_gb": 10,
            "daily_gb": 1,
            "server_ids": [vless_server["id"], trojan_server["id"]],
        }
        for bad_days in (0, 31, -5):
            response = client.post("/api/users", json=dict(base_user, duration_days=bad_days))
            check(f"duration {bad_days} rejected", response.status_code == 400, response.text)

        response = client.post("/api/users", json=dict(base_user, quota_gb=1))
        check("quota below 2GB rejected", response.status_code == 400, response.text)

        response = client.post("/api/users", json=dict(base_user, quota_gb=1.99))
        check("quota 1.99GB rejected", response.status_code == 400)

        response = client.post("/api/users", json=dict(base_user, daily_gb=0.05))
        check("daily below 100MB rejected", response.status_code == 400)

        response = client.post("/api/users", json=dict(base_user, name="   "))
        check("blank name rejected", response.status_code == 400)

        # 1-day and 30-day boundaries must be accepted.
        for ok_days in (1, 30):
            response = client.post(
                "/api/users", json=dict(base_user, name=f"مرزی {ok_days}", duration_days=ok_days)
            )
            check(f"duration {ok_days} accepted", response.status_code == 201, response.text)
            client.delete(f"/api/users/{response.json()['id']}")

        # --- create the real user ---------------------------------------
        section("user creation")
        response = client.post("/api/users", json=base_user)
        check("user created", response.status_code == 201, response.text)
        user = response.json()
        user_id = user["id"]

        check("quota stored as bytes", user["quota_bytes"] == 10 * GB)
        check("daily quota stored as bytes", user["daily_quota_bytes"] == 1 * GB)
        check("duration is 30 days", user["duration_days"] == 30)
        check("new config is active", user["status"] == "active")
        check("30 days remain", user["days_left"] == 30, str(user["days_left"]))
        check("0 days consumed", user["days_used"] == 0)
        check("full quota available", user["remaining_bytes"] == 10 * GB)
        check("subscription url built", user["sub_url"].endswith("/sub/" + user["sub_token"]))
        check("portal url built", user["portal_url"].endswith("/u/" + user["sub_token"]))
        check("two configs rendered", len(user["configs"]) == 2, str(len(user["configs"])))

        # --- link formats ------------------------------------------------
        section("share link formats")
        vless_link = next(c["link"] for c in user["configs"] if c["protocol"] == "vless")
        trojan_link = next(c["link"] for c in user["configs"] if c["protocol"] == "trojan")

        check("vless scheme", vless_link.startswith("vless://"), vless_link)
        check("vless carries the user uuid", user["uuid"] in vless_link)
        parsed = urlparse(vless_link)
        query = parse_qs(parsed.query)
        check("vless host:port", parsed.hostname == "de.example.com" and parsed.port == 443)
        check("vless transport is ws", query.get("type") == ["ws"], parsed.query)
        check("vless path preserved", query.get("path") == ["/rahnema"], parsed.query)
        check("vless security tls", query.get("security") == ["tls"], parsed.query)
        check("vless sni set", query.get("sni") == ["cdn.example.com"], parsed.query)
        check("vless encryption none", query.get("encryption") == ["none"])
        check("vless remark has the name", "علی" in unquote(parsed.fragment), parsed.fragment)

        check("trojan scheme", trojan_link.startswith("trojan://"), trojan_link)
        check("trojan uses the user password", user["password"] in trojan_link, trojan_link)

        # vmess and shadowsocks formats
        response = client.post(
            "/api/servers",
            json={
                "name": "vmess-srv",
                "protocol": "vmess",
                "address": "vm.example.com",
                "port": 80,
                "params": {"network": "ws", "security": "none", "path": "/vm"},
            },
        )
        vmess_server = response.json()
        response = client.post(
            "/api/servers",
            json={
                "name": "ss-srv",
                "protocol": "shadowsocks",
                "address": "ss.example.com",
                "port": 8388,
                "params": {"method": "chacha20-ietf-poly1305"},
            },
        )
        ss_server = response.json()

        # Attach every server so all four protocols render for this user.
        response = client.patch(
            f"/api/users/{user_id}",
            json={"server_ids": [vless_server["id"], trojan_server["id"], vmess_server["id"], ss_server["id"]]},
        )
        check("servers can be attached later", response.status_code == 200, response.text)

        configs = client.get(f"/api/users/{user_id}/configs").json()
        vmess_link = next(c["link"] for c in configs["configs"] if c["protocol"] == "vmess")
        ss_link = next(c["link"] for c in configs["configs"] if c["protocol"] == "shadowsocks")

        check("vmess is a base64 json blob", vmess_link.startswith("vmess://"))
        payload = json.loads(b64decode(vmess_link[len("vmess://"):]))
        check("vmess json has the uuid", payload["id"] == user["uuid"], str(payload))
        check("vmess add/port/path", payload["add"] == "vm.example.com" and payload["port"] == "80" and payload["path"] == "/vm")
        check("ss scheme", ss_link.startswith("ss://"))
        ss_userinfo = b64decode(ss_link[len("ss://"):].split("@")[0])
        check("ss userinfo is method:password", ss_userinfo == "chacha20-ietf-poly1305:" + user["password"], ss_userinfo)

        # --- subscription -------------------------------------------------
        section("subscription endpoint")
        response = client.get(f"/sub/{user['sub_token']}")
        check("subscription returns 200", response.status_code == 200)
        check("subscription is plain text", response.headers["content-type"].startswith("text/plain"))
        check("subscription advertises userinfo", "download=0" in response.headers.get("subscription-userinfo", ""))
        check(
            "subscription advertises the quota",
            f"total={10 * GB}" in response.headers.get("subscription-userinfo", ""),
        )
        decoded = b64decode(response.text).splitlines()
        check("subscription decodes to 4 links", len(decoded) == 4, str(len(decoded)))
        check("subscription contains the vless link", vless_link in decoded)

        response = client.get(f"/sub/{user['sub_token']}?format=plain")
        check("plain format returns raw links", response.text.splitlines()[0].startswith("vless://"))

        check("unknown subscription token is 404", client.get("/sub/does-not-exist").status_code == 404)

        # --- portal --------------------------------------------------------
        section("user portal")
        portal = client.get(f"/api/portal/{user['sub_token']}").json()
        check("portal lists configs", len(portal["configs"]) == 4)
        check("portal hides the uuid", "uuid" not in portal)
        check("portal hides the password", "password" not in portal)
        check("portal exposes days left", portal["days_left"] == 30)
        check("portal marks daily limit enabled", portal["daily_enabled"] is True)

        response = client.get(f"/api/portal/{user['sub_token']}/qr.svg")
        check("qr endpoint serves svg", response.status_code == 200 and response.content.startswith(b"<?xml"))

        # --- usage reporting ------------------------------------------------
        section("usage reporting")
        check("report without api key is rejected", client.post("/api/report", json={"user": "1", "delta_bytes": 1}).status_code == 401)
        check(
            "report with a wrong api key is rejected",
            client.post(
                "/api/report", json={"user": "1", "delta_bytes": 1}, headers={"X-API-Key": "bad"}
            ).status_code == 401,
        )

        api_key = client.get("/api/settings/api-key").json()["api_key"]
        headers = {"X-API-Key": api_key}

        response = client.post(
            "/api/report",
            json={"user": user["sub_token"], "used_bytes": 400 * 1024**2},
            headers=headers,
        )
        check("absolute report accepted", response.status_code == 200, response.text)
        check("absolute report applied", response.json()["results"][0]["used_bytes"] == 400 * 1024**2)

        response = client.post(
            "/api/report", json={"reports": [{"user": user["uuid"], "delta_bytes": 100 * 1024**2}]}, headers=headers
        )
        check("delta report applied", response.json()["results"][0]["used_bytes"] == 500 * 1024**2, response.text)

        # A node restarting reports a lower cumulative total; never bill backwards.
        response = client.post(
            "/api/report", json={"user": user["name"], "used_bytes": 1234}, headers=headers
        )
        check("lower absolute report ignored", response.json()["results"][0]["used_bytes"] == 500 * 1024**2, response.text)

        listed = client.get("/api/users").json()["items"][0]
        check("used bytes surfaced to admin", listed["used_bytes"] == 500 * 1024**2)
        check("remaining volume computed", listed["remaining_bytes"] == 10 * GB - 500 * 1024**2)
        check("usage percent computed", abs(listed["usage_percent"] - 4.9) < 0.2, str(listed["usage_percent"]))

        # --- daily limit -----------------------------------------------------
        section("daily limit")
        response = client.post(
            "/api/report", json={"user": user["sub_token"], "delta_bytes": 600 * 1024**2}, headers=headers
        )
        body = response.json()["results"][0]
        check("daily limit trips at the cap", body["status"] == "daily_limited", str(body))

        blocked_sub = client.get(f"/sub/{user['sub_token']}")
        check("blocked subscription is empty", blocked_sub.text == "")
        check("blocked subscription flags itself", blocked_sub.headers.get("x-panel-blocked") == "1")
        check("blocked subscription keeps usage info", "download=" in blocked_sub.headers.get("subscription-userinfo", ""))

        blocked_portal = client.get(f"/api/portal/{user['sub_token']}").json()
        check("blocked portal hides configs", blocked_portal["configs"] == [])
        check("blocked portal flags itself", blocked_portal["blocked"] is True)

        # Simulate the next local day and confirm the counter rolls over.
        with db.db() as conn:
            conn.execute(
                "UPDATE users SET daily_reset_on = ?, daily_used_bytes = ? WHERE id = ?",
                (service.day_offset(-1), 900 * 1024**2, user_id),
            )
        after_rollover = client.get(f"/api/users/{user_id}").json()
        check("daily counter resets on a new day", after_rollover["daily_used_bytes"] == 0, str(after_rollover["daily_used_bytes"]))
        check("user becomes active again", after_rollover["status"] == "active", after_rollover["status"])
        check("total usage is untouched by the rollover", after_rollover["used_bytes"] == 1100 * 1024**2)

        # --- quota exhaustion --------------------------------------------------
        section("quota exhaustion")
        response = client.post(
            f"/api/users/{user_id}/traffic", json={"used_bytes": 10 * GB}
        )
        check("admin can set absolute usage", response.status_code == 200, response.text)
        check("user is exhausted", response.json()["status"] == "exhausted", response.json()["status"])
        check(
            "exhausted user gets an empty subscription",
            client.get(f"/sub/{user['sub_token']}").text == "",
        )

        response = client.post(f"/api/users/{user_id}/reset-usage")
        check("reset usage restores the user", response.json()["status"] == "active")
        check("reset usage zeroes the counter", response.json()["used_bytes"] == 0)

        # --- expiry -------------------------------------------------------------
        section("expiry")
        # A realistic finished config: activated 31 days ago with a 30 day
        # duration, so it expired one day ago.
        with db.db() as conn:
            now = int(time.time())
            conn.execute(
                "UPDATE users SET activated_at = ?, expires_at = ? WHERE id = ?",
                (now - 31 * 86400, now - 86400, user_id),
            )
        expired = client.get(f"/api/users/{user_id}").json()
        check("expired status computed", expired["status"] == "expired", expired["status"])
        check("a finished period counts all days as consumed", expired["days_used"] == 30, str(expired["days_used"]))
        check("days left is zero when expired", expired["days_left"] == 0, str(expired["days_left"]))
        check("expired subscription is empty", client.get(f"/sub/{user['sub_token']}").text == "")

        # half of the period elapsed -> days left must round up, not down
        with db.db() as conn:
            now = int(time.time())
            conn.execute(
                "UPDATE users SET activated_at = ?, expires_at = ? WHERE id = ?",
                (now - int(2.2 * 86400), now + int(27.8 * 86400), user_id),
            )
        mid = client.get(f"/api/users/{user_id}").json()
        check("days left rounds up mid-period", mid["days_left"] == 28, str(mid["days_left"]))
        check("days used is consistent", mid["days_used"] == 2, str(mid["days_used"]))
        check("days add up to the duration", mid["days_used"] + mid["days_left"] == mid["days_total"])

        # --- renew / link rotation -----------------------------------------------
        section("renew and link rotation")
        response = client.post(f"/api/users/{user_id}/renew", json={"days": 7, "reset_usage": True})
        renewed = response.json()
        check("renew sets the new duration", renewed["duration_days"] == 7)
        check("renew resets the clock", renewed["days_left"] == 7, str(renewed["days_left"]))
        check("renew restores active status", renewed["status"] == "active")
        check("renew can clear usage", renewed["used_bytes"] == 0)
        check(
            "renew rejects out-of-range days",
            client.post(f"/api/users/{user_id}/renew", json={"days": 400}).status_code == 400,
        )

        old_token = renewed["sub_token"]
        rotated = client.post(f"/api/users/{user_id}/reset-link").json()
        check("new token differs", rotated["sub_token"] != old_token)
        check("old link stops working", client.get(f"/sub/{old_token}").status_code == 404)
        check("new link works", client.get(f"/sub/{rotated['sub_token']}").status_code == 200)

        # --- unlimited + pending activation --------------------------------------
        section("unlimited quota and deferred activation")
        response = client.post(
            "/api/users",
            json={
                "name": "بی‌نهایت",
                "duration_days": 15,
                "quota_gb": 0,
                "daily_gb": 0,
                "server_ids": [vless_server["id"]],
                "activate_on_first_use": True,
            },
        )
        unlimited = response.json()
        check("unlimited user created", response.status_code == 201, response.text)
        check("unlimited quota is null", unlimited["quota_bytes"] is None)
        check("unlimited flag set", unlimited["unlimited"] is True)
        check("remaining is unlimited", unlimited["remaining_bytes"] is None)
        check("no daily cap", unlimited["daily_unlimited"] is True)
        check("starts as pending", unlimited["status"] == "pending", unlimited["status"])
        check("no expiry yet", unlimited["expires_at"] is None)
        check("days left is the full duration", unlimited["days_left"] == 15)

        # Editing a config that waits for its first connection must not start it.
        edited = client.patch(
            f"/api/users/{unlimited['id']}", json={"duration_days": 7, "note": "ویرایش شد"}
        ).json()
        check("editing duration keeps a pending config pending", edited["status"] == "pending", edited["status"])
        check("editing duration does not stamp activation", edited["activated_at"] is None)
        check("editing duration does not create an expiry", edited["expires_at"] is None)
        check("editing duration updates the length", edited["duration_days"] == 7)
        check("pending days left follows the new length", edited["days_left"] == 7, str(edited["days_left"]))
        check("editing keeps deferred activation on", edited["activate_on_first_use"] is True)
        client.patch(f"/api/users/{unlimited['id']}", json={"duration_days": 15})

        # Switching deferred activation off must anchor the clock to creation.
        activated_now = client.patch(
            f"/api/users/{unlimited['id']}", json={"activate_on_first_use": False}
        ).json()
        check("turning off deferred activation sends the clock running", activated_now["activated_at"] is not None)
        check("turning off deferred activation sets an expiry", activated_now["expires_at"] is not None)
        client.patch(f"/api/users/{unlimited['id']}", json={"activate_on_first_use": True})

        response = client.post(
            "/api/report",
            json={"user": unlimited["sub_token"], "delta_bytes": 5 * GB},
            headers=headers,
        )
        activated = response.json()["results"][0]
        check("first report activates the clock", activated["status"] == "active", activated["status"])

        reloaded = client.get(f"/api/users/{unlimited['id']}").json()
        check("activation stamped", reloaded["activated_at"] is not None)
        check("expiry is 15 days out", 14 <= reloaded["days_left"] <= 15, str(reloaded["days_left"]))

        response = client.post(
            "/api/report", json={"user": unlimited["sub_token"], "delta_bytes": 50 * GB}, headers=headers
        )
        check(
            "unlimited user never gets exhausted",
            response.json()["results"][0]["status"] == "active",
            response.json()["results"][0]["status"],
        )

        # --- reporting snapshot ---------------------------------------------------
        section("node snapshot endpoint")
        snapshot = client.get("/api/report/users", headers=headers).json()
        check("snapshot lists users", snapshot["count"] >= 2, str(snapshot["count"]))
        check("snapshot exposes status", "status" in snapshot["items"][0])
        check("snapshot rejects a bad key", client.get("/api/report/users", headers={"X-API-Key": "x"}).status_code == 401)

        # --- CSRF ------------------------------------------------------------------
        section("csrf protection")
        response = client.post(
            "/api/users",
            json=base_user,
            headers={"Origin": "https://evil.example"},
        )
        check("cross-site write rejected", response.status_code == 403, str(response.status_code))

        # --- stats ------------------------------------------------------------------
        section("dashboard stats")
        stats = client.get("/api/stats?days=14").json()
        check("stats counts users", stats["users_total"] >= 2)
        check("stats sums used bytes", stats["total_used_bytes"] > 0)
        check("series has 14 points", len(stats["series"]) == 14, str(len(stats["series"])))
        check("series covers today", stats["series"][-1]["day"] == service.local_day())
        check("today traffic recorded", stats["today_bytes"] > 0)
        check("status counts present", "active" in stats["status_counts"])

        # --- settings ----------------------------------------------------------------
        section("settings")
        response = client.put(
            "/api/settings",
            json={"brand": "پنل من", "default_duration_days": 10, "default_quota_gb": 20, "default_daily_gb": 2},
        )
        check("settings saved", response.status_code == 200 and response.json()["brand"] == "پنل من")
        check(
            "invalid default duration rejected",
            client.put("/api/settings", json={"default_duration_days": 60}).status_code == 400,
        )
        check("api key never leaks through settings", "api_key" not in client.get("/api/settings").json())
        check(
            "javascript: URLs rejected in settings",
            client.put("/api/settings", json={"support_url": "javascript:alert(1)"}).status_code == 400,
        )
        check(
            "relative URLs rejected in settings",
            client.put("/api/settings", json={"sub_base_url": "/evil"}).status_code == 400,
        )
        ok = client.put("/api/settings", json={"support_url": "https://t.me/support", "sub_base_url": "https://panel.example.com"})
        check("valid https settings accepted", ok.status_code == 200 and ok.json()["support_url"] == "https://t.me/support", ok.text)
        client.put("/api/settings", json={"support_url": "", "sub_base_url": ""})

        # --- export & delete -----------------------------------------------------------
        section("export and delete")
        export = client.get("/api/export").json()
        check("export contains users", len(export["users"]) >= 2)
        check("export contains servers", len(export["servers"]) >= 4)
        check("export contains history", len(export["usage_days"]) >= 1)

        check("wrong method on users", client.put("/api/users", json={}).status_code == 405)
        check("unknown user is 404", client.get("/api/users/999999").status_code == 404)

        check("delete user", client.delete(f"/api/users/{unlimited['id']}").status_code == 200)
        check("deleted user is gone", client.get(f"/api/users/{unlimited['id']}").status_code == 404)
        with db.db() as conn:
            orphans = conn.execute(
                "SELECT COUNT(*) AS c FROM usage_days WHERE user_id = ?", (unlimited["id"],)
            ).fetchone()["c"]
        check("usage history cascades on delete", orphans == 0, str(orphans))

        # --- pages ------------------------------------------------------------------------
        section("pages")
        check("admin page served", client.get(config.ADMIN_PATH + "/").status_code == 200)
        check("root redirects to the panel", client.get("/", follow_redirects=False).status_code in (307, 308))
        user_page = client.get(f"/u/{rotated['sub_token']}")
        check("user portal page served", user_page.status_code == 200 and b"portal" in user_page.content)
        check("static css served", client.get("/static/style.css").status_code == 200)

        # --- password change -----------------------------------------------------------------
        section("admin password")
        check(
            "password change needs the current one",
            client.post("/api/auth/password", json={"current_password": "wrong", "new_password": "newpass1"}).status_code == 400,
        )
        response = client.post(
            "/api/auth/password",
            json={"current_password": "sup3rsecret", "new_password": "brandnew1"},
        )
        check("password changed", response.status_code == 200, response.text)
        check("old password no longer works", client.post("/api/auth/login", json={"username": "root", "password": "sup3rsecret"}).status_code == 401)
        check("new password works", client.post("/api/auth/login", json={"username": "root", "password": "brandnew1"}).status_code == 200)

    print("\n" + "=" * 62)
    print(f"passed: {len(PASSED)}   failed: {len(FAILED)}")
    if FAILED:
        print("\nfailing checks:")
        for name in FAILED:
            print("  - " + name)
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
