"""
IBKR Auto-Login Streamlit component.

Launches scripts/ibkr_auto_login.py as a background subprocess and
streams JSON status lines into session_state so the sidebar can show
real-time progress without blocking Streamlit.

Usage from app.py:
    from dashboard.components.ibkr_login import render_ibkr_login_button
    render_ibkr_login_button(adapter)
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import streamlit as st

# ── Constants ──────────────────────────────────────────────────────────────────
_SS_PROC     = "_ibkr_login_proc"       # subprocess.Popen object
_SS_STATUS   = "_ibkr_login_status"     # latest status string
_SS_MESSAGE  = "_ibkr_login_message"    # latest human-readable message
_SS_THREAD   = "_ibkr_login_thread"     # reader thread

# Module-level buffer written by background thread, read by main Streamlit thread.
# Using plain dict + no lock is safe: single writer, single reader, string values.
_THREAD_BUF: dict[str, str] = {"status": "", "message": ""}

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


# ── Background reader ──────────────────────────────────────────────────────────
def _read_proc_output(proc: subprocess.Popen) -> None:
    """Run in a daemon thread — reads JSON lines from the login subprocess."""
    try:
        for raw_line in proc.stdout:  # type: ignore[union-attr]
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
                # Write to module-level buffer, NOT st.session_state.
                # Streamlit session_state must only be touched on the main thread.
                _THREAD_BUF["status"]  = data.get("status", "")
                _THREAD_BUF["message"] = data.get("message", "")
            except json.JSONDecodeError:
                pass  # non-JSON stdout lines (e.g. from subprocess deps)
    except Exception:
        pass


# ── Public API ─────────────────────────────────────────────────────────────────
def render_ibkr_login_button(adapter=None) -> None:
    """
    Render the 'Sign in to IBKR' button in the Streamlit sidebar.
    Handles the full auto-login lifecycle.
    """
    # ── Sync from thread buffer → session_state (main thread only) ─────────────
    if _THREAD_BUF["status"]:
        st.session_state[_SS_STATUS]  = _THREAD_BUF["status"]
        st.session_state[_SS_MESSAGE] = _THREAD_BUF["message"]

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
    proc = subprocess.Popen(
        [_PYTHON, str(_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,              # line-buffered
        start_new_session=True, # detach from Streamlit's process group
    )
    st.session_state[_SS_PROC] = proc

    # Start background reader thread
    t = threading.Thread(target=_read_proc_output, args=(proc,), daemon=True)
    t.start()
    st.session_state[_SS_THREAD] = t
