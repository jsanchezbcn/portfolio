from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_PREFERENCES: dict[str, Any] = {
    "copilot_profile": "personal",
    "compact_mode": False,
    "sound_enabled": True,
    "debug_tool_calls": True,
}


def preferences_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(__file__).with_name("prefs.json")


def load_preferences(path: str | Path | None = None) -> dict[str, Any]:
    pref_path = preferences_path(path)
    if not pref_path.exists():
        save_preferences(DEFAULT_PREFERENCES, pref_path)
        return dict(DEFAULT_PREFERENCES)
    try:
        raw = json.loads(pref_path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        raw = {}
    prefs = dict(DEFAULT_PREFERENCES)
    if isinstance(raw, dict):
        prefs.update(raw)
    return prefs


def save_preferences(preferences: dict[str, Any], path: str | Path | None = None) -> Path:
    pref_path = preferences_path(path)
    pref_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(DEFAULT_PREFERENCES)
    payload.update(preferences or {})
    pref_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return pref_path
