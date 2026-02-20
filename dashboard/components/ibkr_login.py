"""
IBKR Auto-Login Streamlit component.

Launches scripts/ibkr_auto_login.py as a background subprocess and
polls /tmp/ibkr_login_status.json for real-time status updates.
Using a file instead of subprocess.PIPE avoids FD linkage between
Streamlit and Chromium that causes macOS to kill both under memory
pressure (Killed: 9).

Usage from app.py:
    from dashboard.components.ibkr_login import render_ibkr_login_button
    render_ibkr_login_button(adapter)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

# ── Constants ──────────────────────────────────────────────────────────────────
_SS_PROC     = "_ibkr_login_proc"       # subprocess.Popen object
_SS_STATUS   = "_ibkr_login_status"     # latest status string
_SS_MESSAGE  = "_ibkr_login_message"    # latest human-readable message
# Status file written by ibkr_auto_login.py — avoids stdout PIPE / FD leaks
_STATUS_FILE = Path("/tmp/ibkr_login_status.json")

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ibkr_auto_login.py"
_PYTHON = sys.executable  # same venv Python as the dashboard

_STATUS_LABELS: dict[str, tuple[str, str]] = {
    "starting":             ("⏳", "Starting gateway check…"),
    "restarting_gateway":   ("🔄", "Restarting IBKR Gateway…"),
    "gateway_restart_done": ("✅", "Gateway restarted."),
    "gateway_up":           ("🟢", "Gateway is up."),
    "navigating":           ("🌐", "Opening IBKR login page…"),
    "form_filled":          ("✏️",  "Credentials entered."),
    "form_submitted":       ("📨", "Login form submitted."),
    "waiting_2fa":          ("📱", "Approve in IBKR Mobile app on your phone…"),
    "authenticated":        ("✅", "Authenticated! Click 'Reload Accounts'."),
    "error":                ("❌", "Login error"),
    "gateway_restart_warning": ("⚠️", "Gateway restart warning"),
    "restart_script_missing":  ("⚠️", "Restart script missing"),
}


# ── Status file polling ───────────────────────────────────────────────────────
def _poll_status_file() -> tuple[str, str]:
    """Read the latest status from /tmp/ibkr_login_status.json.

    Returns (status, message) or ("", "") if the file is absent / unreadable.
    """
    try:
        if _STATUS_FILE.exists():
            data = json.loads(_STATUS_FILE.read_text())
            return data.get("status", ""), data.get("message", "")
    except Exception:
        pass
    return "", ""


# ── Public API ─────────────────────────────────────────────────────────────────
def render_ibkr_login_button(adapter=None) -> None:
    """
    Render the 'Sign in to IBKR' button in the Streamlit sidebar.
    Handles the full auto-login lifecycle.
    """
    # ── Poll status file → session_state ──────────────────────────────────────
    file_status, file_message = _poll_status_file()
    if file_status:
        st.session_state[_SS_STATUS]  = file_status
        st.session_state[_SS_MESSAGE] = file_message

    current_status = st.session_state.get(_SS_STATUS, "")
    current_msg    = st.session_state.get(_SS_MESSAGE, "")

    # ── Determine button label ─────────────────────────────────────────────────
    is_running = _is_login_running()
    btn_label  = "⏳ Login in progress…" if is_running else "Sign in to IBKR"

    # ── Button ─────────────────────────────────────────────────────────────────
    if st.sidebar.button(btn_label, disabled=is_running):
        _start_login()
        st.rerun()

    # ── Status display ─────────────────────────────────────────────────────────
    if current_status:
        icon, default_label = _STATUS_LABELS.get(current_status, ("ℹ️", current_status))
        display_msg = current_msg or default_label

        if current_status == "authenticated":
            st.sidebar.success(f"{icon} {display_msg}")
        elif current_status == "error":
            st.sidebar.error(f"{icon} {display_msg}")
        elif current_status == "waiting_2fa":
            st.sidebar.warning(f"{icon} {display_msg}")
        else:
            st.sidebar.info(f"{icon} {display_msg}")

    # ── Auto-refresh while login is in progress ────────────────────────────────
    if is_running and current_status not in ("authenticated", "error"):
        time.sleep(2)
        st.rerun()


# ── Internal helpers ───────────────────────────────────────────────────────────
def _is_login_running() -> bool:
    """Return True if the login subprocess is still alive."""
    proc: subprocess.Popen | None = st.session_state.get(_SS_PROC)
    if proc is None:
        return False
    return proc.poll() is None  # None means still running


def _start_login() -> None:
    """Kill any existing login process and start a fresh one."""
    # Kill previous process if still alive
    old_proc: subprocess.Popen | None = st.session_state.get(_SS_PROC)
    if old_proc and old_proc.poll() is None:
        try:
            old_proc.terminate()
        except Exception:
            pass

    # Reset state
    st.session_state[_SS_STATUS]  = "starting"
    st.session_state[_SS_MESSAGE] = ""
    _THREAD_BUF["status"]  = "starting"
    _THREAD_BUF["message"] = ""

    # Launch the login script as a subprocess with stdout piped.
    # start_new_session=True puts the child in its own process group so that
    # macOS will not SIGKILL Streamlit when Playwright/Chromium uses lots of RAM.
    # stderr goes to DEVNULL — Playwright's stderr is noisy and fills the pipe.
    # Clear the status file so stale values don't bleed in from a previous run
    try:
        _STATUS_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    proc = subprocess.Popen(
        [_PYTHON, str(_SCRIPT)],
        stdout=subprocess.DEVNULL,   # no PIPE — avoids FD linkage with Chromium
        stderr=subprocess.DEVNULL,
        start_new_session=True,      # detach from Streamlit's process group
    )
    st.session_state[_SS_PROC] = proc
