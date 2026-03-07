from __future__ import annotations

import json

from desktop.engine.token_manager import (
    ACTIVE_PROFILE_ENV,
    ACTIVE_TOKEN_ENV,
    TokenManager,
)


def test_token_manager_initializes_from_preferences(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs.json"
    prefs.write_text(json.dumps({"copilot_profile": "work"}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_COPILOT_WORK", "jose-antonio-sanchez_hpi")

    manager = TokenManager(preferences_path=prefs)

    assert manager.active_profile == "work"
    assert manager.active_token == "jose-antonio-sanchez_hpi"


def test_token_manager_persists_profile_changes(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs.json"
    prefs.write_text(json.dumps({"copilot_profile": "personal"}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_COPILOT_PERSONAL", "jsanchezbcn")
    monkeypatch.setenv("GITHUB_COPILOT_WORK", "jose-antonio-sanchez_hpi")

    manager = TokenManager(preferences_path=prefs)
    state = manager.set_active_profile("work")

    stored = json.loads(prefs.read_text(encoding="utf-8"))
    assert state.profile == "work"
    assert stored["copilot_profile"] == "work"
    assert manager.active_token_env_var == "GITHUB_COPILOT_WORK"


def test_token_manager_sets_active_environment_variables(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs.json"
    prefs.write_text(json.dumps({"copilot_profile": "personal"}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_COPILOT_PERSONAL", "jsanchezbcn")

    manager = TokenManager(preferences_path=prefs)

    assert manager.active_profile == "personal"
    assert manager.active_token == "jsanchezbcn"
    assert manager._environ[ACTIVE_PROFILE_ENV] == "personal"
    assert manager._environ[ACTIVE_TOKEN_ENV] == "jsanchezbcn"


def test_token_manager_ignores_token_like_profile_value(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs.json"
    prefs.write_text(json.dumps({"copilot_profile": "work"}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_COPILOT_WORK", "gho_not_a_username_token")

    manager = TokenManager(preferences_path=prefs)

    assert manager.active_profile == "work"
    assert manager.active_token == ""
    assert manager._environ[ACTIVE_TOKEN_ENV] == ""
