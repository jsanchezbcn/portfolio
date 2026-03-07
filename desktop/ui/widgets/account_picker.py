from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QWidget, QPushButton,
    QMessageBox, QVBoxLayout, QDialog, QTextEdit
)

from desktop.engine.token_manager import PROFILE_TOKEN_KEYS, LEGACY_PROFILE_TOKEN_KEYS


class AccountPicker(QWidget):
    """Compact toolbar widget for selecting the active Copilot profile with account status.
    
    Displays available Copilot profiles (Personal/Work) and shows visual indicators
    of profile-account availability (✓ configured, ✗ missing). Users can switch
    profiles and view account mapping details.
    """

    profile_changed = Signal(str)
    token_status_changed = Signal(str, bool)  # (profile, has_token)

    def __init__(
        self,
        active_profile: str = "personal",
        token_checker: Optional[Callable[[str], bool]] = None,
        parent=None
    ):
        """Initialize the token picker widget.
        
        Args:
            active_profile: The currently active profile ("personal" or "work")
            token_checker: Optional callable to check token availability: (profile: str) -> bool
            parent: Parent widget
        """
        super().__init__(parent)
        self._token_checker = token_checker or self._default_token_checker
        self._token_status_cache: dict[str, bool] = {}
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._label = QLabel("Copilot:")
        self._combo = QComboBox()
        self._combo.addItem("Personal", userData="personal")
        self._combo.addItem("Work", userData="work")
        
        self._status_label = QLabel()
        self._status_label.setFont(QFont("Menlo", 10))
        self._status_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._status_label.setToolTip("Click to view Copilot token/account details")
        self._status_label.mousePressEvent = self._on_status_label_clicked
        
        self._info_btn = QPushButton("ℹ")
        self._info_btn.setMaximumWidth(28)
        self._info_btn.setToolTip("View Copilot token/account configuration")
        self._info_btn.clicked.connect(self._show_token_info_dialog)
        
        layout.addWidget(self._label)
        layout.addWidget(self._combo)
        layout.addWidget(self._status_label)
        layout.addWidget(self._info_btn)

        self.set_active_profile(active_profile)
        self._combo.currentIndexChanged.connect(self._on_profile_changed)
        self._refresh_token_status()

    def active_profile(self) -> str:
        """Return the currently selected profile."""
        return str(self._combo.currentData())

    def set_active_profile(self, profile: str) -> None:
        """Set the active profile and update token status display.
        
        Args:
            profile: Profile name ("personal" or "work")
        """
        normalized = "work" if str(profile).strip().lower() == "work" else "personal"
        index = self._combo.findData(normalized)
        if index >= 0 and index != self._combo.currentIndex():
            self._combo.setCurrentIndex(index)

    def token_available(self, profile: Optional[str] = None) -> bool:
        """Check if a token is configured for the given profile.
        
        Args:
            profile: Profile to check. If None, checks the active profile.
            
        Returns:
            True if token is available for the profile.
        """
        p = profile or self.active_profile()
        return self._token_status_cache.get(p, False)

    def refresh_token_status(self) -> None:
        """Manually refresh token status indicators."""
        self._refresh_token_status()

    def _on_profile_changed(self) -> None:
        """Handle profile selection change."""
        self._refresh_token_status()
        self.profile_changed.emit(self.active_profile())

    def _refresh_token_status(self) -> None:
        """Update token status cache and refresh UI display."""
        # Check token availability for all profiles
        for profile in ("personal", "work"):
            try:
                has_token = self._token_checker(profile)
                self._token_status_cache[profile] = has_token
            except Exception as e:
                # If checker raises, assume token is unavailable
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Error checking token for {profile}: {e}")
                self._token_status_cache[profile] = False
                has_token = False
            
            self.token_status_changed.emit(profile, has_token)
        
        # Update status label for active profile
        active = self.active_profile()
        has_token = self._token_status_cache.get(active, False)
        status_icon = "✅" if has_token else "❌"
        self._status_label.setText(status_icon)
        self._status_label.setToolTip(
            f"Copilot Token/account {'configured' if has_token else 'NOT configured'} for {active} profile"
        )

    def _default_token_checker(self, profile: str) -> bool:
        """Default profile-account checker that reads from environment variables.
        
        Args:
            profile: Profile name to check
            
        Returns:
            True if profile account environment variable is set and not empty.
        """
        env_var = PROFILE_TOKEN_KEYS.get(profile.strip().lower())
        legacy_env_var = LEGACY_PROFILE_TOKEN_KEYS.get(profile.strip().lower())
        if not env_var:
            return False
        return bool(os.getenv(env_var, "").strip() or os.getenv(legacy_env_var or "", "").strip())

    def _on_status_label_clicked(self, event) -> None:
        """Show token info dialog when status label is clicked."""
        self._show_token_info_dialog()

    def _show_token_info_dialog(self) -> None:
        """Display a dialog with token configuration details for all profiles."""
        dialog = TokenInfoDialog(self._token_status_cache, self)
        dialog.exec()


class TokenInfoDialog(QDialog):
    """Dialog showing account configuration status for all available profiles."""
    
    def __init__(self, token_status: dict[str, bool], parent=None):
        """Initialize the token info dialog.
        
        Args:
            token_status: Dict mapping profile name to token availability boolean
            parent: Parent widget
        """
        super().__init__(parent)
        self.setWindowTitle("Copilot Token Configuration")
        self.setMinimumWidth(400)
        self.setMinimumHeight(250)
        
        layout = QVBoxLayout(self)
        
        # Title
        title = QLabel("Copilot Profile Account Status")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)
        
        # Status text
        info_text = QTextEdit()
        info_text.setReadOnly(True)
        info_text.setStyleSheet(
            "background-color: #f8f9fa; border: 1px solid #e0e0e0; color: #1a1a1a; font-family: Menlo, Monaco, 'Courier New';"
        )
        
        status_lines = []
        for profile in ("personal", "work"):
            has_token = token_status.get(profile, False)
            status_icon = "✅ CONFIGURED" if has_token else "❌ NOT CONFIGURED"
            env_var = PROFILE_TOKEN_KEYS.get(profile)
            status_lines.append(f"{profile.upper()}: {status_icon}")
            status_lines.append(f"  Environment Variable: {env_var}")
            status_lines.append("")
        
        status_lines.extend([
            "Setup Instructions:",
            "1. Configure github.com username per profile in .env:",
            f"   - {PROFILE_TOKEN_KEYS['personal']}=<github_username_for_personal>",
            f"   - {PROFILE_TOKEN_KEYS['work']}=<github_username_for_work>",
            "",
            "2. Authenticate those accounts with gh on github.com:",
            "   - gh auth login --hostname github.com",
            "   - gh auth status",
            "",
            "3. Restart the application for changes to take effect.",
        ])
        
        info_text.setText("\n".join(status_lines))
        layout.addWidget(info_text)
        
        # Close button
        from PySide6.QtWidgets import QPushButton
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)
