"""Live integration test for scripts/xray_report.py.

Boots a real uvicorn server on a free port with a throwaway database, then runs
the reporter as a subprocess exactly as an operator would from cron, asserting
that traffic lands in the panel and that deltas/restarts are handled.

    python tests/test_reporter_live.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="panel-reporter-"))
DATA_DIR = TMP / "data"
STATE = TMP / "report-state.json"
FIXTURE = TMP / "statsquery.json"
ADMIN = {"username": "admin", "password": "admin12345"}

FAILURES: list[str] = []
PASSED = 0
COOKIE: dict[str, str] = {}


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL  {name}  {detail}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def api(base: str, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if COOKIE.get("c"):
        headers["Cookie"] = COOKIE["c"]
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.headers.get("Set-Cookie"):
            COOKIE["c"] = response.headers["Set-Cookie"].split(";")[0]
        payload = response.read().decode()
        return json.loads(payload) if payload else None


def write_fixture(entries) -> None:
    stats = []
    for email, up, down in entries:
        stats.append({"name": f"user>>>{email}>>>traffic>>>uplink", "value": str(up)})
        stats.append({"name": f"user>>>{email}>>>traffic>>>downlink", "value": str(down)})
    FIXTURE.write_text(json.dumps({"stat": stats}), "utf-8")


def run_reporter(base: str, api_key: str, *extra) -> subprocess.CompletedProcess:
    command = [
        sys.executable, str(ROOT / "scripts" / "xray_report.py"),
        "--panel-url", base, "--api-key", api_key,
        "--from-file", str(FIXTURE), "--state", str(STATE),
        "--match", "name", *extra,
    ]
    return subprocess.run(command, capture_output=True, text=True, timeout=90)


def wait_for_health(base: str, seconds: int = 40) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=3) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def load_reporter_module():
    spec = importlib.util.spec_from_file_location("xray_report", ROOT / "scripts" / "xray_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    # --- pure functions, no server needed ---------------------------------
    print("== parser ==")
    reporter = load_reporter_module()
    parsed = reporter.parse_totals({"stat": [
        {"name": "user>>>a@x>>>traffic>>>uplink", "value": "100"},
        {"name": "user>>>a@x>>>traffic>>>downlink", "value": 250},
        {"name": "user>>>b@x>>>traffic>>>uplink", "value": "1"},
        {"name": "inbound>>>in-1>>>traffic>>>uplink", "value": "999"},
        {"name": "garbage", "value": "5"},
    ]})
    check("uplink+downlink summed per email", parsed == {"a@x": 350, "b@x": 1}, str(parsed))
    check("string and int values both handled", parsed.get("a@x") == 350)
    check("non-user counters ignored", len(parsed) == 2)
    check("empty payload is empty", reporter.parse_totals({}) == {})
    check("missing value defaults to zero",
          reporter.parse_totals({"stat": [{"name": "user>>>z>>>traffic>>>uplink"}]}) == {"z": 0})

    # --- live server --------------------------------------------------------
    print("\n== live server ==")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({"DATA_DIR": str(DATA_DIR), "ADMIN_USERNAME": ADMIN["username"],
                "ADMIN_PASSWORD": ADMIN["password"], "TZ_NAME": "Asia/Tehran"})
    env.pop("PUBLIC_BASE_URL", None)
    env.pop("DB_PATH", None)

    log = open(TMP / "server.log", "w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        check("server starts", wait_for_health(base), (TMP / "server.log").read_text("utf-8", "replace")[-800:])

        api(base, "/api/auth/login", "POST", ADMIN)
        created = api(base, "/api/users", "POST", {
            "name": "node1-user", "duration_days": 30, "quota_gb": 10, "daily_gb": 0, "server_ids": [],
        })
        user_id = created["id"]
        check("panel user created", created["name"] == "node1-user", str(created))

        with sqlite3.connect(str(DATA_DIR / "panel.db")) as conn:
            api_key = conn.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()[0]

        def used() -> int:
            return api(base, f"/api/users/{user_id}")["used_bytes"]

        # 1) first report bills the whole counter
        write_fixture([("node1-user", 1000, 2000)])
        result = run_reporter(base, api_key)
        check("first run exits cleanly", result.returncode == 0, result.stderr)
        check("first run bills the full counter", used() == 3000, str(used()))

        # 2) unchanged counters are not billed twice
        result = run_reporter(base, api_key)
        check("repeat run is a no-op", used() == 3000, str(used()))
        check("repeat run says nothing new", "nothing new" in result.stderr, result.stderr)

        # 3) only the growth is billed
        write_fixture([("node1-user", 4000, 4000)])
        run_reporter(base, api_key)
        check("growth is billed as a delta", used() == 8000, str(used()))

        # 4) node restart: counters drop, the new value becomes the delta
        write_fixture([("node1-user", 60, 40)])
        run_reporter(base, api_key)
        check("node restart never bills backwards", used() == 8100, str(used()))
        state = json.loads(STATE.read_text("utf-8"))
        check("state remembers the latest counter", state.get("node1-user") == 100, str(state))

        # 5) unknown users are skipped, known ones still update
        write_fixture([("node1-user", 200, 100), ("ghost-user", 500, 500)])
        result = run_reporter(base, api_key)
        check("unknown user does not break the batch", result.returncode == 0, result.stderr)
        check("unknown user is logged as skipped", "ghost-user" in result.stderr, result.stderr)
        check("known user still updated", used() == 8300, str(used()))
        check("state for the known user is kept",
              json.loads(STATE.read_text("utf-8")).get("node1-user") == 300,
              STATE.read_text("utf-8"))

        # 6) dry run must not touch the panel
        write_fixture([("node1-user", 999, 1)])
        before = used()
        result = run_reporter(base, api_key, "--dry-run")
        check("dry run exits cleanly", result.returncode == 0, result.stderr)
        check("dry run does not change usage", used() == before, str(used()))
        check("dry run prints the payload", "delta_bytes" in result.stdout, result.stdout)

        # 7) absolute mode replaces the counter
        write_fixture([("node1-user", 9000, 1000)])
        result = run_reporter(base, api_key, "--mode", "absolute")
        check("absolute mode exits cleanly", result.returncode == 0, result.stderr)
        check("absolute mode sets the total", used() == 10000, str(used()))

        # 8) a lower absolute total must never reduce what was billed
        write_fixture([("node1-user", 5, 5)])
        run_reporter(base, api_key, "--mode", "absolute")
        check("absolute mode never bills backwards", used() == 10000, str(used()))

        # 9) the panel recorded this in today's history
        stats = api(base, "/api/stats?days=14")
        check("today's traffic reflects the reports", stats["today_bytes"] >= 10000, str(stats["today_bytes"]))

        # 10) --match local maps "user@host" onto the panel name
        api(base, "/api/users", "POST", {
            "name": "local-user", "duration_days": 5, "quota_gb": 5, "daily_gb": 0, "server_ids": [],
        })
        STATE.unlink(missing_ok=True)
        write_fixture([("local-user@vpn.example.com", 700, 300)])
        result = run_reporter(base, api_key, "--match", "local")
        check("--match local exits cleanly", result.returncode == 0, result.stderr)
        local_user = api(base, "/api/users?search=local-user")["items"][0]
        check("--match local bills the local part", local_user["used_bytes"] == 1000, str(local_user["used_bytes"]))

        # 11) a wrong API key fails loudly instead of silently dropping traffic.
        # The fixture must have grown, otherwise there is no delta to send and
        # the reporter correctly returns before touching the API.
        write_fixture([("local-user@vpn.example.com", 2000, 2000)])
        bad = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "xray_report.py"), "--panel-url", base,
             "--api-key", "wrong", "--from-file", str(FIXTURE), "--state", str(STATE)],
            capture_output=True, text=True, timeout=90,
        )
        check("bad api key fails loudly", bad.returncode != 0 and "401" in bad.stderr, bad.stderr)

        # 12) missing credentials are a usage error, not a crash
        blank = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "xray_report.py"), "--from-file", str(FIXTURE)],
            capture_output=True, text=True, timeout=90,
            env={k: v for k, v in os.environ.items() if k not in ("PANEL_URL", "PANEL_API_KEY")},
        )
        check("missing panel url is a clear error", blank.returncode != 0 and "required" in blank.stderr, blank.stderr)

        # 13) no counters at all is a clean no-op
        FIXTURE.write_text(json.dumps({"stat": []}), "utf-8")
        empty = run_reporter(base, api_key)
        check("empty stats is a clean no-op", empty.returncode == 0 and "no per-user" in empty.stderr, empty.stderr)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()

    print("\n" + "=" * 58)
    print(f"passed: {PASSED}   failed: {len(FAILURES)}")
    for name in FAILURES:
        print("  - " + name)
    print("=" * 58)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
