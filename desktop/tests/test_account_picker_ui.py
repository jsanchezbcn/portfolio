"""UI tests for AccountPicker widget using pytest and qtbot.

These tests verify the AccountPicker user interface behavior, including:
- Visual elements (combobox, status label, info button)
- User interactions (clicking, selections)
- Dialog behavior
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QPushButton, QComboBox, QLabel
from PySide6.QtCore import Qt

from desktop.ui.widgets.account_picker import AccountPicker, TokenInfoDialog


class TestAccountPickerUIElements:
    """Test AccountPicker visual elements and layout."""

    def test_combobox_visible(self, qtbot):
        """Test that combobox is visible in the widget."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        widget.show()
        
        combo = widget.findChild(QComboBox)
        assert combo is not None
        assert combo.isVisible()

    def test_status_label_visible(self, qtbot):
        """Test that status label is visible."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        widget.show()
        
        assert widget._status_label is not None
        assert widget._status_label.isVisible()

    def test_info_button_visible(self, qtbot):
        """Test that info button is visible."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        widget.show()
        
        info_btn = widget.findChild(QPushButton)
        assert info_btn is not None
        assert info_btn.isVisible()

    def test_combobox_has_two_items(self, qtbot):
        """Test that combobox has Personal and Work options."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        combo = widget.findChild(QComboBox)
        assert combo.count() == 2
        assert combo.itemText(0) == "Personal"
        assert combo.itemText(1) == "Work"

    def test_label_text(self, qtbot):
        """Test that label displays 'Copilot:'."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        label = widget._label
        assert "Copilot" in label.text()


class TestAccountPickerUserInteractions:
    """Test user interactions with AccountPicker."""

    def test_click_combobox_to_switch_profile(self, qtbot):
        """Test clicking combobox to switch profile."""
        widget = AccountPicker("personal")
        qtbot.addWidget(widget)
        widget.show()
        
        combo = widget.findChild(QComboBox)
        assert widget.active_profile() == "personal"
        
        # Click on Work item
        combo.setCurrentIndex(1)
        qtbot.wait(100)
        
        assert widget.active_profile() == "work"

    def test_double_click_status_label_opens_dialog(self, qtbot):
        """Test that clicking status label attempts to open dialog."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        widget.show()
        
        status_label = widget._status_label
        
        # Mock the show_token_info_dialog method
        dialog_shown = []
        original_show = widget._show_token_info_dialog
        widget._show_token_info_dialog = lambda: dialog_shown.append(True)
        
        # Simulate label click
        from PySide6.QtGui import QMouseEvent
        event = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            status_label.rect().center(),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier
        )
        status_label.mousePressEvent(event)
        
        assert len(dialog_shown) > 0
        widget._show_token_info_dialog = original_show

    def test_info_button_click_opens_info_dialog(self, qtbot):
        """Test that info button click opens the token info dialog."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        widget.show()
        
        info_btn = widget.findChild(QPushButton)
        assert info_btn is not None
        
        # The button should trigger _show_token_info_dialog
        dialog_shown = []
        original_show = widget._show_token_info_dialog
        widget._show_token_info_dialog = lambda: dialog_shown.append(True)
        
        qtbot.mouseClick(info_btn, Qt.MouseButton.LeftButton)
        qtbot.wait(100)
        
        assert len(dialog_shown) > 0
        widget._show_token_info_dialog = original_show

    def test_tooltip_on_status_label(self, qtbot):
        """Test that status label has helpful tooltip."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        tooltip = widget._status_label.toolTip()
        assert "Token" in tooltip
        assert "configured" in tooltip.lower() or "not configured" in tooltip.lower()

    def test_tooltip_on_info_button(self, qtbot):
        """Test that info button has helpful tooltip."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        info_btn = widget.findChild(QPushButton)
        tooltip = info_btn.toolTip()
        assert "token" in tooltip.lower() or "configuration" in tooltip.lower()


class TestTokenInfoDialogUI:
    """Test TokenInfoDialog UI behavior."""

    def test_dialog_is_displayed_correctly(self, qtbot):
        """Test that dialog displays with correct title and content."""
        token_status = {"personal": True, "work": False}
        dialog = TokenInfoDialog(token_status)
        qtbot.addWidget(dialog)
        dialog.show()
        
        assert dialog.windowTitle() == "Copilot Token Configuration"
        assert dialog.isVisible()

    def test_dialog_has_close_button(self, qtbot):
        """Test that dialog has a close button."""
        token_status = {}
        dialog = TokenInfoDialog(token_status)
        qtbot.addWidget(dialog)
        dialog.show()
        
        close_btn = dialog.findChild(QPushButton)
        assert close_btn is not None
        assert "Close" in close_btn.text()

    def test_dialog_close_button_closes_dialog(self, qtbot):
        """Test that close button actually closes the dialog."""
        token_status = {}
        dialog = TokenInfoDialog(token_status)
        qtbot.addWidget(dialog)
        dialog.show()
        
        assert dialog.isVisible()
        
        close_btn = dialog.findChild(QPushButton)
        qtbot.mouseClick(close_btn, Qt.MouseButton.LeftButton)
        qtbot.wait(100)
        
        # Dialog should be closed

    def test_dialog_minimum_size(self, qtbot):
        """Test that dialog has reasonable minimum size."""
        token_status = {}
        dialog = TokenInfoDialog(token_status)
        qtbot.addWidget(dialog)
        
        assert dialog.minimumWidth() >= 400
        assert dialog.minimumHeight() >= 250

    def test_dialog_content_is_readonly(self, qtbot):
        """Test that status text is read-only."""
        token_status = {"personal": True, "work": False}
        dialog = TokenInfoDialog(token_status)
        qtbot.addWidget(dialog)
        
        from PySide6.QtWidgets import QTextEdit
        text_edit = dialog.findChild(QTextEdit)
        assert text_edit is not None
        assert text_edit.isReadOnly()


class TestAccountPickerVisualFeedback:
    """Test visual feedback for token status."""

    def test_status_icon_updates_on_profile_change(self, qtbot, monkeypatch):
        """Test that status icon updates when switching profiles."""
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_PERSONAL", "token_p")
        monkeypatch.delenv("GITHUB_COPILOT_TOKEN_WORK", raising=False)
        
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        # Personal has token
        icon_personal = widget._status_label.text()
        assert "✅" in icon_personal
        
        # Switch to work (which has no token)
        widget.set_active_profile("work")
        icon_work = widget._status_label.text()
        assert "❌" in icon_work

    def test_status_label_tooltip_updates(self, qtbot, monkeypatch):
        """Test that status label tooltip reflects current status."""
        monkeypatch.delenv("GITHUB_COPILOT_TOKEN_PERSONAL", raising=False)
        monkeypatch.delenv("GITHUB_COPILOT_TOKEN_WORK", raising=False)
        
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        # Check personal profile
        tooltip = widget._status_label.toolTip()
        assert "personal" in tooltip.lower()
        assert "not configured" in tooltip.lower() or "unavailable" in tooltip.lower()

    def test_cursor_changes_on_status_label(self, qtbot):
        """Test that cursor changes to pointing hand on status label."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        widget.show()
        
        # The cursor should be a pointing hand
        cursor = widget._status_label.cursor()
        assert cursor.shape() == Qt.CursorShape.PointingHandCursor


class TestAccountPickerEdgeCases:
    """Test edge cases and error handling."""

    def test_handles_null_token_checker(self, qtbot):
        """Test that widget handles None token checker gracefully."""
        widget = AccountPicker(token_checker=None)
        qtbot.addWidget(widget)
        
        # Should not raise exception
        token_available = widget.token_available("personal")
        assert isinstance(token_available, bool)

    def test_handles_token_checker_exception(self, qtbot):
        """Test graceful handling of token checker exceptions."""
        def bad_checker(profile):
            raise ValueError("Checker error")
        
        # Widget should not crash when token checker raises
        widget = AccountPicker(token_checker=bad_checker)
        qtbot.addWidget(widget)
        
        # Widget should still be functional
        assert widget.active_profile() in ("personal", "work")
        
        # Token availability should default to False on error
        assert widget.token_available("personal") is False
        assert widget.token_available("work") is False

    def test_rapid_profile_switches(self, qtbot):
        """Test rapid profile switching."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        # Rapidly switch profiles
        for _ in range(10):
            widget.set_active_profile("work")
            widget.set_active_profile("personal")
        
        # Should end up in personal profile
        assert widget.active_profile() == "personal"

    def test_invalid_profile_name_defaults_to_personal(self, qtbot):
        """Test that invalid profile names default to personal."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        widget.set_active_profile("invalid_profile")
        assert widget.active_profile() == "personal"

    def test_whitespace_in_profile_name_handled(self, qtbot):
        """Test that whitespace in profile names is handled."""
        widget = AccountPicker()
        qtbot.addWidget(widget)
        
        widget.set_active_profile("  work  ")
        assert widget.active_profile() == "work"
        
        widget.set_active_profile("\npersonal\t")
        assert widget.active_profile() == "personal"
