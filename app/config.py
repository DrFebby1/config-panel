"""Environment-driven configuration.

Every tunable lives here so the rest of the codebase never reads os.environ
directly. Anything that must differ between a local run and a Railway deploy is
an environment variable (see .env.example).
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent

GB = 1024**3
MB = 1024**2


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _number(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --- Storage ---------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR") or (BASE_DIR / "data")).expanduser()
DB_PATH = Path(os.environ.get("DB_PATH") or (DATA_DIR / "panel.db")).expanduser()

# --- Server ----------------------------------------------------------------
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = _number("PORT", 8000)

# --- Admin access ----------------------------------------------------------
# Where the admin UI is served from. Overridable so the panel is not trivially
# discoverable by port scanners.
ADMIN_PATH = "/" + (os.environ.get("ADMIN_PATH", "panel").strip().strip("/") or "panel")

# Optional bootstrap credentials. When ADMIN_USERNAME/ADMIN_PASSWORD are set and
# no admin exists yet, the account is created on boot. Without them the panel
# shows a first-run setup screen instead.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_PASSWORD_RESET = _flag("ADMIN_PASSWORD_RESET", False)

SESSION_COOKIE = "panel_session"
SESSION_TTL = _number("SESSION_DAYS", 30) * 86400

# --- Timezone --------------------------------------------------------------
# Used for daily traffic resets, "days remaining" and the usage history.
TIMEZONE_NAME = os.environ.get("TZ_NAME", "Asia/Tehran")
try:
    TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except Exception:  # pragma: no cover - bad TZ_NAME falls back to UTC
    TIMEZONE = ZoneInfo("UTC")
    TIMEZONE_NAME = "UTC"

# --- Limits enforced when creating a config --------------------------------
MIN_DURATION_DAYS = 1
MAX_DURATION_DAYS = 30
# A config is either unlimited or at least this big.
MIN_QUOTA_BYTES = 2 * GB
DEFAULT_DURATION_DAYS = 30
DEFAULT_QUOTA_BYTES = 50 * GB

# Base URL used when rendering subscription links for end users. Empty means
# "derive it from the incoming request", which is correct for local runs.
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")

# Static assets (admin panel and user portal) live next to the app package.
STATIC_DIR = BASE_DIR / "static"

# Set to 0 to disable the interactive /docs page.
ENABLE_DOCS = _flag("ENABLE_DOCS", True)
