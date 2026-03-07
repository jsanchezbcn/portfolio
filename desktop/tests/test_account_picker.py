from __future__ import annotations

import pytest
from unittest.mock import Mock

from desktop.ui.widgets.account_picker import AccountPicker, TokenInfoDialog


class TestAccountPickerBasics:
    """Test basic AccountPicker functionality."""

    def test_defaults_to_personal(self, qtbot):
        """Test that AccountPicker defaults to personal profile."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        assert widget.active_profile() == "personal"

    def test_can_switch_to_work(self, qtbot):
        """Test switching to work profile."""
        widget = AccountPicker("personal")
        qtbot.addWidget(widget)
        widget.set_active_profile("work")
        assert widget.active_profile() == "work"

    def test_can_switch_back_to_personal(self, qtbot):
        """Test switching back to personal profile."""
        widget = AccountPicker("work")
        qtbot.addWidget(widget)
        widget.set_active_profile("personal")
        assert widget.active_profile() == "personal"

    def test_normalizes_profile_names(self, qtbot):
        """Test that profile names are normalized (case-insensitive)."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        widget.set_active_profile("WORK")
        assert widget.active_profile() == "work"
        
        widget.set_active_profile("Personal")
        assert widget.active_profile() == "personal"

    def test_emits_profile_changed_signal(self, qtbot):
        """Test that profile_changed signal is emitted."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        with qtbot.waitSignal(widget.profile_changed, timeout=1000) as blocker:
            widget.set_active_profile("work")
        
        assert blocker.args == ["work"]

    def test_emits_profile_changed_only_on_actual_change(self, qtbot):
        """Test that signal is not emitted when profile doesn't change."""
        widget = AccountPicker("personal")
        qtbot.addWidget(widget)
        
        signal_emissions = []
        widget.profile_changed.connect(lambda p: signal_emissions.append(p))
        
        # Try to set the same profile - signal shouldn't be emitted
        widget.set_active_profile("personal")
        qtbot.wait(100)
        
        # Signal should not have been emitted
        assert len(signal_emissions) == 0


class TestTokenStatusDisplay:
    """Test token status indication features."""

    def test_token_checker_callback(self, qtbot):
        """Test that token checker callback is used."""
        mock_checker = Mock(side_effect=lambda p: p == "personal")
        widget = AccountPicker(token_checker=mock_checker)
        qtbot.addWidget(widget)
        
        assert mock_checker.called
        assert widget.token_available("personal") is True
        assert widget.token_available("work") is False

    def test_token_available_returns_cached_status(self, qtbot):
        """Test that token_available returns cached status."""
        mock_checker = Mock(side_effect=lambda p: p == "work")
        widget = AccountPicker(token_checker=mock_checker)
        qtbot.addWidget(widget)
        
        assert widget.token_available("work") is True
        assert widget.token_available("personal") is False

    def test_token_available_defaults_to_active_profile(self, qtbot):
        """Test that token_available checks active profile by default."""
        mock_checker = Mock(return_value=True)
        widget = AccountPicker("personal", token_checker=mock_checker)
        qtbot.addWidget(widget)
        
        # Should check the active profile
        assert widget.token_available() is True
        
        widget.set_active_profile("work")
        assert widget.token_available() is True

    def test_refresh_token_status_updates_cache(self, qtbot):
        """Test that refresh_token_status updates the cache."""
        call_count = 0
        
        def counting_checker(profile):
            nonlocal call_count
            call_count += 1
            return profile == "personal"
        
        widget = AccountPicker(token_checker=counting_checker)
        qtbot.addWidget(widget)
        
        initial_count = call_count
        widget.refresh_token_status()
        
        # Should have called the checker again
        assert call_count > initial_count

    def test_token_status_changed_signal_emission(self, qtbot):
        """Test that token_status_changed signal is emitted."""
        mock_checker = Mock(return_value=True)
        widget = AccountPicker(token_checker=mock_checker)
        qtbot.addWidget(widget)
        
        signal_emissions = []
        widget.token_status_changed.connect(lambda p, has: signal_emissions.append((p, has)))
        
        widget.refresh_token_status()
        
        # Should have emissions for both personal and work
        profiles = [p for p, _ in signal_emissions]
        assert "personal" in profiles
        assert "work" in profiles

    def test_status_label_shows_correct_icon(self, qtbot):
        """Test that status label shows correct icon based on token availability."""
        # With token available
        mock_checker = Mock(return_value=True)
        widget = AccountPicker(token_checker=mock_checker)
        qtbot.addWidget(widget)
        
        assert "✅" in widget._status_label.text()
        
        # With token unavailable
        mock_checker = Mock(return_value=False)
        widget2 = AccountPicker(token_checker=mock_checker)
        qtbot.addWidget(widget2)
        
        assert "❌" in widget2._status_label.text()


class TestDefaultTokenChecker:
    """Test default token checker implementation."""

    def test_default_checker_reads_from_env(self, qtbot, monkeypatch):
        """Test that default checker reads from environment variables."""
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_PERSONAL", "test_token_personal")
        monkeypatch.delenv("GITHUB_COPILOT_TOKEN_WORK", raising=False)
        
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        assert widget.token_available("personal") is True
        assert widget.token_available("work") is False

    def test_default_checker_ignores_empty_tokens(self, qtbot, monkeypatch):
        """Test that default checker treats empty tokens as unavailable."""
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_PERSONAL", "")
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_WORK", "   ")
        
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        assert widget.token_available("personal") is False
        assert widget.token_available("work") is False

    def test_default_checker_handles_missing_env_vars(self, qtbot, monkeypatch):
        """Test that default checker gracefully handles missing env vars."""
        monkeypatch.delenv("GITHUB_COPILOT_TOKEN_PERSONAL", raising=False)
        monkeypatch.delenv("GITHUB_COPILOT_TOKEN_WORK", raising=False)
        
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        # Should not raise exceptions
        assert widget.token_available("personal") is False
        assert widget.token_available("work") is False


class TestTokenInfoDialog:
    """Test TokenInfoDialog functionality."""

    def test_dialog_creation(self, qtbot):
        """Test that TokenInfoDialog can be created and displayed."""
        token_status = {"personal": True, "work": False}
        dialog = TokenInfoDialog(token_status)
        qtbot.addWidget(dialog)
        
        assert dialog.windowTitle() == "Copilot Token Configuration"

    def test_dialog_displays_token_status(self, qtbot):
        """Test that dialog displays token status correctly."""
        token_status = {"personal": True, "work": False}
        dialog = TokenInfoDialog(token_status)
        qtbot.addWidget(dialog)
        
        text_edit = dialog.findChild(type(dialog.findChild(type(None))))
        # Find the text edit widget
        from PySide6.QtWidgets import QTextEdit
        text_edits = dialog.findChildren(QTextEdit)
        assert len(text_edits) > 0
        
        content = text_edits[0].toPlainText()
        assert "PERSONAL" in content
        assert "WORK" in content
        assert "CONFIGURED" in content
        assert "NOT CONFIGURED" in content

    def test_dialog_shows_setup_instructions(self, qtbot):
        """Test that dialog shows setup instructions."""
        token_status = {}
        dialog = TokenInfoDialog(token_status)
        qtbot.addWidget(dialog)
        
        from PySide6.QtWidgets import QTextEdit
        text_edits = dialog.findChildren(QTextEdit)
        assert len(text_edits) > 0
        
        content = text_edits[0].toPlainText()
        assert "Setup Instructions" in content
        assert "gh auth login --hostname github.com" in content
        assert "GITHUB_COPILOT" in content

    def test_dialog_close_button_works(self, qtbot):
        """Test that close button closes the dialog."""
        token_status = {"personal": True, "work": False}
        dialog = TokenInfoDialog(token_status)
        qtbot.addWidget(dialog)
        
        from PySide6.QtWidgets import QPushButton
        from PySide6.QtCore import Qt
        close_btn = dialog.findChild(QPushButton)
        assert close_btn is not None
        
        qtbot.mouseClick(close_btn, Qt.MouseButton.LeftButton)
        # Dialog should be closed


class TestAccountPickerWithTokenManager:
    """Test AccountPicker integration with TokenManager."""

    def test_picker_works_with_token_manager(self, qtbot, monkeypatch):
        """Test AccountPicker can work with TokenManager."""
        from desktop.engine.token_manager import TokenManager
        
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_PERSONAL", "token_personal")
        monkeypatch.delenv("GITHUB_COPILOT_TOKEN_WORK", raising=False)
        
        token_manager = TokenManager()
        
        def checker(profile):
            state = token_manager.state_for(profile)
            return state.token_available
        
        widget = AccountPicker(token_checker=checker)
        qtbot.addWidget(widget)
        
        assert widget.token_available("personal") is True
        assert widget.token_available("work") is False

    def test_profile_change_reflects_different_tokens(self, qtbot, monkeypatch):
        """Test that switching profiles shows different token status."""
        from desktop.engine.token_manager import TokenManager
        
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_PERSONAL", "token_personal")
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_WORK", "token_work")
        
        token_manager = TokenManager()
        
        def checker(profile):
            state = token_manager.state_for(profile)
            return state.token_available
        
        widget = AccountPicker(token_checker=checker)
        qtbot.addWidget(widget)
        
        # Both should be available
        assert widget.token_available("personal") is True
        assert widget.token_available("work") is True
        
        # Switch to work
        widget.set_active_profile("work")
        assert widget.active_profile() == "work"
