#!/usr/bin/env python3
"""
IBKR Auto-Login via Playwright.

Reads credentials from .env, restarts the Client Portal gateway if needed,
then fills the login form at https://localhost:5001/ using a visible browser
so the user can approve the 2FA phone notification.

Prints JSON status lines to stdout so the Streamlit dashboard can parse them:
  {"status": "starting"}
  {"status": "gateway_up"}
  {"status": "form_filled"}
  {"status": "waiting_2fa"}
  {"status": "authenticated"}
  {"status": "error", "message": "..."}

Status is ALSO written to /tmp/ibkr_login_status.json for file-based IPC
(used by the Streamlit component so no pipe/fd connects to this process).

Usage:
  python scripts/ibkr_auto_login.py
  python scripts/ibkr_auto_login.py --headless   # CI/headless mode
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
import urllib3

# ── Path setup ─────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass  # dotenv optional – env vars may already be set

# ── Config ─────────────────────────────────────────────────────────────────────
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GATEWAY_BASE    = os.getenv("IBKR_GATEWAY_URL", "https://localhost:5001")
# The .env may have http:// — the gateway always listens on https://
if GATEWAY_BASE.startswith("http://"):
    GATEWAY_BASE = GATEWAY_BASE.replace("http://", "https://", 1)
LOGIN_URL       = f"{GATEWAY_BASE}/"
TICKLE_URL      = f"{GATEWAY_BASE}/v1/api/tickle"
AUTH_STATUS_URL = f"{GATEWAY_BASE}/v1/api/iserver/auth/status"

# Handle dual spelling in .env (IBKR_UUSERNAME typo is in the current .env)
IBKR_USER = (
    os.getenv("IBKR_USERNAME")
    or os.getenv("IBKR_UUSERNAME")
    or ""
)
IBKR_PASS = os.getenv("IBKR_PASSWORD", "")

RESTART_SCRIPT = PROJECT_ROOT / "scripts" / "restart_ibkr_portal.sh"
GATEWAY_JAR    = PROJECT_ROOT / "clientportal" / "bin" / "run.sh"

# File used for IPC with the Streamlit dashboard (avoids stdout pipe)
_STATUS_FILE = Path("/tmp/ibkr_login_status.json")


# ── Helpers ────────────────────────────────────────────────────────────────────
def emit(status: str, **extra):
    """Write a JSON status to the IPC file AND stdout."""
    payload = {"status": status, **extra}
    line = json.dumps(payload)
    print(line, flush=True)
    try:
        _STATUS_FILE.write_text(line)
    except Exception:
        pass


def gateway_alive(timeout: float = 4.0) -> bool:
    try:
        r = requests.get(TICKLE_URL, verify=False, timeout=timeout)
        return r.status_code in (200, 401)
    except Exception:
        return False


def is_authenticated() -> bool:
    """Return True if the gateway reports authenticated=True."""
    try:
        r = requests.get(AUTH_STATUS_URL, verify=False, timeout=5)
        if r.status_code == 200:
            data = r.json()
            return bool(data.get("authenticated"))
    except Exception:
        pass
    return False


def restart_gateway() -> bool:
    """Restart the IBKR Client Portal gateway process."""
    if RESTART_SCRIPT.exists():
        emit("restarting_gateway")
        result = subprocess.run(
            [str(RESTART_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            emit("gateway_restart_done")
        else:
            emit("gateway_restart_warning", stderr=result.stderr[:200])
    else:
        emit("restart_script_missing", path=str(RESTART_SCRIPT))

    # Wait up to 45 s for gateway to respond
    deadline = time.time() + 45
    while time.time() < deadline:
        if gateway_alive():
            return True
        time.sleep(2)
    return False


# ── Main login flow ─────────────────────────────────────────────────────────────
def run_login(headless: bool = False, restart: bool = True) -> bool:
    """
    Open browser, fill IBKR login form, wait for 2FA approval.
    Returns True if authentication succeeds within timeout.
    """
    if not IBKR_USER:
        emit("error", message="IBKR_USERNAME / IBKR_UUSERNAME not set in .env")
        return False
    if not IBKR_PASS:
        emit("error", message="IBKR_PASSWORD not set in .env")
        return False

    emit("starting")

    # ── 1. Ensure gateway is up ────────────────────────────────────────────────
    if not gateway_alive():
        if restart:
            ok = restart_gateway()
            if not ok:
                emit("error", message="Gateway did not start within timeout.")
                return False
        else:
            emit("error", message="Gateway is not running and restart=False.")
            return False

    emit("gateway_up")

    # ── 2. Open browser and fill form ─────────────────────────────────────────
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--ignore-certificate-errors"],
        )
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()

        try:
            emit("navigating", url=LOGIN_URL)
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)

            # ── Fill username ─────────────────────────────────────────────────
            page.wait_for_selector("#xyz-field-username", timeout=15_000)
            page.fill("#xyz-field-username", IBKR_USER)

            # ── Fill password ─────────────────────────────────────────────────
            page.fill("#xyz-field-password", IBKR_PASS)

            emit("form_filled")

            # ── Click Login ───────────────────────────────────────────────────
            page.click(".xyzblock-username-submit button[type=submit]")
            emit("form_submitted")

            # ── Wait for 2FA screen (notification block appears) ──────────────
            try:
                page.wait_for_selector(
                    ".xyzblock-notification, .xyzblock-finished, .xyzblock-error",
                    timeout=15_000,
                )
                # Check if we immediately hit an error (wrong credentials)
                error_el = page.query_selector(".xyzblock-error")
                if error_el and error_el.is_visible():
                    msg = page.text_content(".xyz-errormessage") or "Login error"
                    emit("error", message=msg.strip())
                    browser.close()
                    return False

                emit("waiting_2fa",
                     message="Approve the IBKR Mobile notification on your phone.")

            except PWTimeout:
                # Maybe already logged in (previously authenticated session)
                emit("waiting_2fa", message="2FA screen not shown — may already be authenticated.")

            # ── Poll until authenticated (up to 3 minutes) ────────────────────
            deadline = time.time() + 180  # 3-minute window for the user
            while time.time() < deadline:
                if is_authenticated():
                    emit("authenticated")
                    browser.close()
                    return True

                # Also check for success overlay on the page
                finished = page.query_selector(".xyzblock-finished")
                if finished and finished.is_visible():
                    emit("authenticated")
                    browser.close()
                    return True

                time.sleep(3)

            emit("error", message="Timed out waiting for 2FA approval (3 min).")
            browser.close()
            return False

        except Exception as exc:
            emit("error", message=str(exc)[:300])
            try:
                browser.close()
            except Exception:
                pass
            return False


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IBKR auto-login via Playwright")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser in headless mode (no visible window)")
    parser.add_argument("--no-restart", action="store_true",
                        help="Skip gateway restart even if gateway is down")
    args = parser.parse_args()

    success = run_login(headless=args.headless, restart=not args.no_restart)
    sys.exit(0 if success else 1)
