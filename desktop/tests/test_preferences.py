from __future__ import annotations

from desktop.config.preferences import DEFAULT_PREFERENCES, load_preferences, save_preferences


def test_load_preferences_creates_defaults(tmp_path):
    prefs = tmp_path / "prefs.json"

    loaded = load_preferences(prefs)

    assert loaded == DEFAULT_PREFERENCES
    assert prefs.exists()


def test_save_preferences_merges_defaults(tmp_path):
    prefs = tmp_path / "prefs.json"

    save_preferences({"copilot_profile": "work"}, prefs)
    loaded = load_preferences(prefs)

    assert loaded["copilot_profile"] == "work"
    assert loaded["compact_mode"] is False
    assert loaded["sound_enabled"] is True
