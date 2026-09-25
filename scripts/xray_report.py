#!/usr/bin/env python3
"""Push per-user traffic from an Xray node into the panel.

The panel counts traffic; it does not measure it (it never sees the VPN
packets). This script is the bridge: it reads Xray's built-in per-user stats
and reports the bytes to the panel's API.

Typical cron entry on the VPN server (every 5 minutes):

    */5 * * * * PANEL_URL=https://my-panel.up.railway.app \\
        PANEL_API_KEY=xxxx /usr/bin/python3 /opt/panel/scripts/xray_report.py

Why deltas by default
---------------------
Xray's counters reset whenever the node restarts. Reporting an absolute total
would therefore make the panel look like it lost traffic. Delta mode keeps a
small state file next to the script and sends only the difference, so a node
restart is handled correctly (the counter going backwards is treated as a
restart and the new value is sent as the delta).

Match panel users by email
--------------------------
Xray identifies clients by "email". The panel resolves a user by subscription
token, UUID or exact name. Use --match to control the mapping:
    name (default)  email must equal the panel user's name
    local           the part before '@' must equal the panel user's name
    uuid            email must equal the panel user's UUID
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

STAT_RE = re.compile(r"^user>>>(?P<email>.+?)>>>traffic>>>(?P<dir>uplink|downlink)$")


def log(message: str) -> None:
    print(f"[xray-report] {message}", file=sys.stderr)


def run_statsquery(server: str, binary: str, timeout: int) -> dict:
    """Ask Xray for its traffic counters."""
    command = [binary, "api", "statsquery", f"--server={server}", "-pattern", "user>>>"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        raise SystemExit(f"could not find the xray binary at {binary!r} (use --xray-binary)")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"xray statsquery timed out after {timeout}s")

    if result.returncode != 0:
        raise SystemExit(
            f"xray statsquery failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        return json.loads(result.stdout or "{}")
    except ValueError as exc:
        raise SystemExit(f"could not parse xray output as JSON: {exc}")


def parse_totals(payload: dict) -> dict[str, int]:
    """Collapse `user>>><email>>>traffic>>>uplink/downlink` into totals."""
    totals: dict[str, int] = {}
    for entry in payload.get("stat") or []:
        name = str(entry.get("name") or "")
        match = STAT_RE.match(name)
        if not match:
            continue
        try:
            value = int(entry.get("value") or 0)
        except (TypeError, ValueError):
            continue
        totals[match.group("email")] = totals.get(match.group("email"), 0) + value
    return totals


def load_state(path: Path) -> dict[str, int]:
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, int] = {}
    for key, value in raw.items():
        try:
            clean[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return clean


def save_state(path: Path, state: dict[str, int]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
        temp.replace(path)
    except OSError as exc:
        log(f"warning: could not save state to {path}: {exc}")


def post(url: str, api_key: str, payload: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"panel rejected the report ({exc.code}): {exc.read().decode('utf-8', 'replace')}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"could not reach the panel at {url}: {exc.reason}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Report Xray traffic to the config panel.")
    parser.add_argument("--panel-url", default=os.environ.get("PANEL_URL", ""),
                        help="panel base url, e.g. https://my-panel.up.railway.app")
    parser.add_argument("--api-key", default=os.environ.get("PANEL_API_KEY", ""),
                        help="API key from the panel's settings tab")
    parser.add_argument("--server", default=os.environ.get("XRAY_API", "127.0.0.1:10085"),
                        help="xray api endpoint")
    parser.add_argument("--xray-binary", default=os.environ.get("XRAY_BINARY", "xray"))
    parser.add_argument("--state", default=os.environ.get("REPORT_STATE", "/var/lib/panel-report-state.json"),
                        help="where to remember the last reported counters")
    parser.add_argument("--mode", choices=("delta", "absolute"), default="delta",
                        help="delta is restart-safe (default); absolute sends cumulative totals")
    parser.add_argument("--match", choices=("name", "local", "uuid"), default="name",
                        help="how an xray email maps onto a panel user")
    parser.add_argument("--from-file", help="read a saved statsquery JSON instead of calling xray")
    parser.add_argument("--dry-run", action="store_true", help="print what would be sent")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()

    if not args.panel_url:
        raise SystemExit("--panel-url (or PANEL_URL) is required")
    if not args.api_key:
        raise SystemExit("--api-key (or PANEL_API_KEY) is required")

    if args.from_file:
        payload = json.loads(Path(args.from_file).read_text("utf-8"))
    else:
        payload = run_statsquery(args.server, args.xray_binary, args.timeout)

    totals = parse_totals(payload)
    if not totals:
        log("no per-user traffic counters found (is the stats API enabled in Xray?)")
        return 0

    state_path = Path(args.state)
    state = load_state(state_path) if args.mode == "delta" else {}

    reports = []
    new_state = dict(state)
    for email, current in sorted(totals.items()):
        if args.match == "local":
            user = email.split("@", 1)[0]
        else:
            user = email

        if args.mode == "delta":
            previous = state.get(email, 0)
            # A drop means the node restarted: bill the new counter as the delta.
            delta = current - previous if current >= previous else current
            new_state[email] = current
            if delta <= 0:
                continue
            reports.append({"user": user, "delta_bytes": delta})
        else:
            if current <= 0:
                continue
            reports.append({"user": user, "used_bytes": current})

    if not reports:
        log("nothing new to report")
        return 0

    if args.dry_run:
        print(json.dumps({"url": args.panel_url.rstrip("/") + "/api/report", "reports": reports},
                         ensure_ascii=False, indent=2))
        return 0

    response = post(args.panel_url.rstrip("/") + "/api/report", args.api_key, {"reports": reports}, args.timeout)

    missing = 0
    for result in response.get("results", []):
        if not result.get("ok"):
            missing += 1
            log(f"skipped {result.get('user')!r}: {result.get('error')}")

    if args.mode == "delta":
        # Only remember counters we successfully pushed, otherwise traffic that
        # was rejected (unknown user) would be silently dropped forever.
        failed = {r.get("user") for r in response.get("results", []) if not r.get("ok")}
        for email in list(new_state):
            if email in failed or (args.match == "local" and email.split("@", 1)[0] in failed):
                new_state.pop(email, None)
        save_state(state_path, new_state)

    log(f"reported {len(reports) - missing} user(s), skipped {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
