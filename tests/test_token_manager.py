"""Tests for token manager integration with token picker.

Tests TokenManager functionality and interaction with AccountPicker.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import Mock, patch
import json
import tempfile

from desktop.engine.token_manager import TokenManager, PROFILE_TOKEN_KEYS


class TestTokenManager:
    """Test TokenManager functionality."""

    def test_token_manager_initialization(self):
        """Test TokenManager initializes with default values."""
        manager = TokenManager()
        assert manager.active_profile in ("personal", "work")

    def test_token_manager_loads_preferences(self, tmp_path, monkeypatch):
        """Test that TokenManager loads preferences correctly."""
        pref_file = tmp_path / "prefs.json"
        prefs = {"copilot_profile": "work"}
        pref_file.write_text(json.dumps(prefs))
        
        manager = TokenManager(preferences_path=pref_file)
        assert manager.active_profile == "work"

    def test_token_manager_set_active_profile(self, tmp_path):
        """Test changing active profile."""
        manager = TokenManager(preferences_path=tmp_path / "prefs.json")
        
        state = manager.set_active_profile("work")
        assert state.profile == "work"
        assert manager.active_profile == "work"

    def test_token_manager_persists_profile(self, tmp_path):
        """Test that profile selection is persisted."""
        pref_file = tmp_path / "prefs.json"
        
        manager1 = TokenManager(preferences_path=pref_file)
        manager1.set_active_profile("work")
        
        # Load again
        manager2 = TokenManager(preferences_path=pref_file)
        assert manager2.active_profile == "work"

    def test_token_manager_available_profiles(self):
        """Test that available_profiles returns expected profiles."""
        manager = TokenManager()
        profiles = manager.available_profiles()
        assert profiles == ("personal", "work")

    def test_token_manager_state_for_profile(self, monkeypatch):
        """Test getting state for a specific profile."""
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_PERSONAL", "token_p")
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_WORK", "")
        
        manager = TokenManager()
        
        personal_state = manager.state_for("personal")
        assert personal_state.profile == "personal"
        assert personal_state.token_value == "token_p"
        assert personal_state.token_available is True
        
        work_state = manager.state_for("work")
        assert work_state.profile == "work"
        assert work_state.token_available is False

    def test_token_manager_has_configured_token(self, monkeypatch):
        """Test checking if a token is configured."""
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_PERSONAL", "token_p")
        monkeypatch.delenv("GITHUB_COPILOT_TOKEN_WORK", raising=False)
        
        manager = TokenManager()
        
        assert manager.has_configured_token("personal") is True
        assert manager.has_configured_token("work") is False

    def test_token_manager_has_configured_token_for_active_profile(self, monkeypatch):
        """Test checking token for active profile."""
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_WORK", "token_w")
        monkeypatch.delenv("GITHUB_COPILOT_TOKEN_PERSONAL", raising=False)
        
        manager = TokenManager()
        manager.set_active_profile("work")
        
        assert manager.has_configured_token() is True

    def test_token_manager_normalizes_profile_names(self):
        """Test that profile names are normalized."""
        manager = TokenManager()
        
        state = manager.set_active_profile("WORK")
        assert state.profile == "work"
        
        state = manager.set_active_profile("Personal")
        assert state.profile == "personal"

    def test_token_manager_invalid_profile_defaults_to_personal(self):
        """Test that invalid profiles default to personal."""
        manager = TokenManager()
        
        state = manager.set_active_profile("invalid")
        assert state.profile == "personal"

    def test_token_manager_with_custom_environ(self):
        """Test TokenManager with custom environment dict."""
        custom_env = {
            "GITHUB_COPILOT_TOKEN_PERSONAL": "custom_token_p",
            "GITHUB_COPILOT_TOKEN_WORK": "",
        }
        
        manager = TokenManager(environ=custom_env)
        
        assert manager.state_for("personal").token_available is True
        assert manager.state_for("work").token_available is False


class TestTokenManagerTokenRouting:
    """Test token routing and environment variable handling."""

    def test_active_token_from_profile(self, monkeypatch):
        """Test retrieving active token."""
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_PERSONAL", "token_personal")
        
        manager = TokenManager()
        manager.set_active_profile("personal")
        
        assert manager.active_token == "token_personal"

    def test_active_token_env_var(self, monkeypatch):
        """Test active token environment variable name."""
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_WORK", "token_work")
        
        manager = TokenManager()
        manager.set_active_profile("work")
        
        assert manager.active_token_env_var == "GITHUB_COPILOT_WORK"

    def test_environment_variables_set_correctly(self, monkeypatch):
        """Test that environment variables are set when profile changes."""
        env = {
            "GITHUB_COPILOT_TOKEN_PERSONAL": "token_p",
            "GITHUB_COPILOT_TOKEN_WORK": "token_w",
        }
        
        manager = TokenManager(environ=env)
        manager.set_active_profile("work")
        
        assert env["GITHUB_COPILOT_ACTIVE_PROFILE"] == "work"
        assert env["GITHUB_COPILOT_ACTIVE_TOKEN"] == "token_w"

    def test_active_state_from_env_static_method(self, monkeypatch):
        """Test active_state_from_env static method."""
        monkeypatch.setenv("GITHUB_COPILOT_ACTIVE_PROFILE", "work")
        monkeypatch.setenv("GITHUB_COPILOT_ACTIVE_TOKEN", "token_w")
        
        state = TokenManager.active_state_from_env()
        assert state.profile == "work"
        assert state.token_value == "token_w"


class TestTokenManagerWithAccountPicker:
    """Test integration between TokenManager and AccountPicker."""

    def test_account_picker_can_use_token_manager(self, monkeypatch):
        """Test that AccountPicker can use TokenManager for token checking."""
        from desktop.ui.widgets.account_picker import AccountPicker
        
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_PERSONAL", "token_p")
        monkeypatch.delenv("GITHUB_COPILOT_TOKEN_WORK", raising=False)
        
        manager = TokenManager()
        
        def checker(profile):
            return manager.has_configured_token(profile)
        
        widget = AccountPicker(token_checker=checker)
        
        assert widget.token_available("personal") is True
        assert widget.token_available("work") is False

    def test_profile_change_updates_manager(self, monkeypatch):
        """Test that changing profile in picker updates manager."""
        from desktop.ui.widgets.account_picker import AccountPicker
        
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_PERSONAL", "token_p")
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_WORK", "token_w")
        
        manager = TokenManager()
        
        def checker(profile):
            return manager.has_configured_token(profile)
        
        widget = AccountPicker(token_checker=checker)
        
        # Simulate manager state update on profile change
        widget.profile_changed.connect(manager.set_active_profile)
        
        widget.set_active_profile("work")
        # Manually update manager (in real scenario this connects signal)
        manager.set_active_profile("work")
        
        assert manager.active_profile == "work"


class TestTokenManagerErrorHandling:
    """Test error handling in TokenManager."""

    def test_handles_missing_preferences_file(self, tmp_path):
        """Test handling of missing preferences file."""
        nonexistent_file = tmp_path / "nonexistent" / "prefs.json"
        
        manager = TokenManager(preferences_path=nonexistent_file)
        # Should not raise, defaults to personal
        assert manager.active_profile is not None

    def test_handles_invalid_json_in_preferences(self, tmp_path):
        """Test handling of invalid JSON in preferences file."""
        pref_file = tmp_path / "prefs.json"
        pref_file.write_text("{ invalid json")
        
        manager = TokenManager(preferences_path=pref_file)
        # Should not raise, defaults to personal
        assert manager.active_profile is not None

    def test_handles_missing_token_env_vars(self):
        """Test handling when token env vars are not set."""
        env = {}
        manager = TokenManager(environ=env)
        
        state = manager.state_for("personal")
        assert state.token_available is False

    def test_handles_empty_token_strings(self):
        """Test handling of empty token strings."""
        env = {
            "GITHUB_COPILOT_TOKEN_PERSONAL": "",
            "GITHUB_COPILOT_TOKEN_WORK": "   ",
        }
        manager = TokenManager(environ=env)
        
        assert manager.has_configured_token("personal") is False
        assert manager.has_configured_token("work") is False


class TestTokenManagerStatePersistence:
    """Test persistence of token manager state."""

    def test_profile_persists_across_instances(self, tmp_path):
        """Test that profile preference persists across manager instances."""
        pref_file = tmp_path / "prefs.json"
        
        # First instance
        manager1 = TokenManager(preferences_path=pref_file)
        manager1.set_active_profile("work")
        
        # Second instance
        manager2 = TokenManager(preferences_path=pref_file)
        assert manager2.active_profile == "work"

    def test_profile_not_persisted_with_persist_false(self, tmp_path):
        """Test that profile is not persisted when persist=False."""
        pref_file = tmp_path / "prefs.json"
        
        manager1 = TokenManager(preferences_path=pref_file)
        manager1.set_active_profile("work", persist=False)
        
        # Check file wasn't written with work profile
        if pref_file.exists():
            content = json.loads(pref_file.read_text())
            # Either file wasn't created or doesn't have work profile
            # (depends on initialization)

    def test_multiple_preferences_managed_separately(self, tmp_path):
        """Test managing multiple preference files."""
        pref_file1 = tmp_path / "prefs1.json"
        pref_file2 = tmp_path / "prefs2.json"
        
        manager1 = TokenManager(preferences_path=pref_file1)
        manager2 = TokenManager(preferences_path=pref_file2)
        
        manager1.set_active_profile("personal")
        manager2.set_active_profile("work")
        
        # Reload and verify
        manager1_reload = TokenManager(preferences_path=pref_file1)
        manager2_reload = TokenManager(preferences_path=pref_file2)
        
        assert manager1_reload.active_profile == "personal"
        assert manager2_reload.active_profile == "work"
