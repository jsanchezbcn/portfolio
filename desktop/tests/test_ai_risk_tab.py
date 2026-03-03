"""desktop/tests/test_ai_risk_tab.py — Basic tests for AI Risk tab."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from desktop.ui.ai_risk_tab import AIRiskTab, _get_copilot_account


class TestAIRiskTab:
    def test_creates_without_crash(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)

    def test_default_model_picker_has_entries(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._cmb_model.count() > 0

    def test_default_scenario_is_auto(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)
        assert "Auto" in tab._cmb_scenario.currentText()

    def test_has_whatif_button(self, qtbot, mock_engine):
        tab = AIRiskTab(mock_engine)
        qtbot.addWidget(tab)
        assert "WhatIf" in tab._btn_whatif.text()


class TestGetCopilotAccount:
    """Unit tests for _get_copilot_account() account-detection function."""

    def _mock_run(self, stdout: str, returncode: int = 0):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = ""
        return result

    def test_returns_login_line_when_present(self):
        """Picks first line that contains 'Logged in to'."""
        stdout = (
            "github.com\n"
            "  ✓ Logged in to github.com as jsanchezbcn (oauth_token)\n"
            "  ✓ Git operations: https\n"
        )
        with patch("subprocess.run", return_value=self._mock_run(stdout, 0)):
            result = _get_copilot_account()
        assert "jsanchezbcn" in result

    def test_returns_account_line_when_no_login_line(self):
        """Falls back to any line containing 'Account'."""
        stdout = (
            "github.com\n"
            "  Account: jsanchezbcn\n"
        )
        with patch("subprocess.run", return_value=self._mock_run(stdout, 0)):
            result = _get_copilot_account()
        assert "jsanchezbcn" in result

    def test_falls_back_to_as_split_when_no_keyword_line(self):
        """Falls back to splitting on 'as' when none of the keyword lines matched."""
        stdout = "Connected as jsanchezbcn (token)\n"
        with patch("subprocess.run", return_value=self._mock_run(stdout, 0)):
            result = _get_copilot_account()
        assert "jsanchezbcn" in result

    def test_returns_unknown_when_gh_fails(self):
        """Returns unknown string when gh exits non-zero and git also fails."""
        fail = MagicMock()
        fail.returncode = 1
        fail.stdout = ""
        fail.stderr = ""
        with patch("subprocess.run", return_value=fail):
            result = _get_copilot_account()
        assert "unknown" in result.lower() or "GitHub" in result

    def test_falls_back_to_git_on_file_not_found(self):
        """When gh is not installed, falls back to git config user.name."""
        git_result = MagicMock()
        git_result.returncode = 0
        git_result.stdout = "jsanchezbcn\n"

        def side_effect(cmd, **kwargs):
            if "gh" in cmd:
                raise FileNotFoundError
            return git_result

        with patch("subprocess.run", side_effect=side_effect):
            result = _get_copilot_account()
        assert "jsanchezbcn" in result

    def test_returns_detection_failed_on_timeout(self):
        """Returns detection-failed string on TimeoutExpired."""
        import subprocess

        def side_effect(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5)

        with patch("subprocess.run", side_effect=side_effect):
            result = _get_copilot_account()
        assert "GitHub Copilot" in result
