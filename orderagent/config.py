"""Order agent configuration.

The order agent is a separate process from the gateway on purpose: it owns
the Playwright/Chromium dependency and the payment guard rails, and the
gateway stays stdlib-only. When the Mac mini arrives this whole directory
moves there and only ORDER_AGENT_URL in the gateway's .env changes.
"""
from __future__ import annotations

import os
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("ORDER_AGENT_PORT", "8766"))  # 8765 is CRM, 8378 is approval daemon

# Everything the agent writes at runtime (store cache, sqlite, screenshots,
# browser profile) lives under data/ -- gitignored, personal.
DATA_DIR = Path(__file__).resolve().parent / "data"
PROFILE_DIR = DATA_DIR / "browser_profile"  # payment methods live ONLY here, entered by hand
JOBS_DIR = DATA_DIR / "jobs"
DB_PATH = DATA_DIR / "orders.db"

# --- payment guard rails (NFR-1) -------------------------------------------
# Payment execution is opt-in per environment AND per job: the code path that
# clicks the final pay control refuses to run unless ORDER_PAYMENT_ENABLED=1
# was set when the agent started. There is no way to flip it over HTTP.
PAYMENT_ENABLED = os.environ.get("ORDER_PAYMENT_ENABLED", "") == "1"
MAX_ORDER_YEN = int(os.environ.get("ORDER_MAX_YEN", "3000"))
APPROVAL_TIMEOUT_SECONDS = 300  # FR-5: 5 minutes, then the cart is abandoned

# The gateway this agent announces through (same box today).
GATEWAY_URL = os.environ.get("ORDER_GATEWAY_URL", "http://127.0.0.1:8080")

# Human-speed pacing for site automation (ToS risk mitigation: §7).
ACTION_DELAY_SECONDS = float(os.environ.get("ORDER_ACTION_DELAY", "1.2"))

MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/148.0.0.0 Mobile Safari/537.36")
VIEWPORT = {"width": 390, "height": 844}

# Real Chrome, not Playwright's bundled Chromium: the bundled chrome.exe
# dies with a side-by-side configuration error on this machine (2026-08-16,
# survives --force reinstall), and the payment session must live in ONE
# profile shared by the manual registration browser and the agent -- same
# binary, same encryption keys.
BROWSER_CHANNEL = os.environ.get("ORDER_BROWSER_CHANNEL", "chrome")


def ensure_dirs() -> None:
    for d in (DATA_DIR, PROFILE_DIR, JOBS_DIR):
        d.mkdir(parents=True, exist_ok=True)
