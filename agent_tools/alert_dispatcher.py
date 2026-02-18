"""Alert dispatcher — delivers portfolio risk violation notifications.

Usage::

    from agent_tools.alert_dispatcher import build_default_dispatcher

    dispatcher = build_default_dispatcher()
    dispatcher.dispatch("Portfolio risk check", violations)

Environment variables:
    SLACK_WEBHOOK_URL — when set, violations are also POSTed to Slack.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any

LOGGER = logging.getLogger(__name__)


class AlertDispatcher:
    """Base class for alert delivery.  Sub-class and implement :meth:`dispatch`."""

    def dispatch(self, title: str, violations: list[dict[str, Any]]) -> None:
        """Send *violations* with a human-readable *title*.

        Args:
            title: Short description of the alert context (e.g. "Risk check after refresh").
            violations: List produced by :meth:`PortfolioTools.check_risk_limits`.
        """
        raise NotImplementedError


class LogDispatcher(AlertDispatcher):
    """Emit violations as structured ``CRITICAL`` log lines (always-available default)."""

    def dispatch(self, title: str, violations: list[dict[str, Any]]) -> None:
        for violation in violations:
            LOGGER.critical(
                "[RISK ALERT] %s | metric=%s current=%s limit=%s | %s",
                title,
                violation.get("metric", "?"),
                violation.get("current", "?"),
                violation.get("limit", "?"),
                violation.get("message", ""),
            )


class SlackDispatcher(AlertDispatcher):
    """POST violations to a Slack Incoming Webhook (``SLACK_WEBHOOK_URL`` env var)."""

    def __init__(self, webhook_url: str | None = None) -> None:
        self.webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "")

    def dispatch(self, title: str, violations: list[dict[str, Any]]) -> None:
        if not self.webhook_url:
            LOGGER.warning("SlackDispatcher: SLACK_WEBHOOK_URL is not configured — skipping Slack alert.")
            return

        lines = [f":rotating_light: *{title}*"]
        for v in violations:
            lines.append(
                f"• *{v.get('metric', '?')}*: {v.get('message', '')} "
                f"(current: `{v.get('current', '?')}`, limit: `{v.get('limit', '?')}`)"
            )

        payload = {"text": "\n".join(lines)}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status != 200:
                    LOGGER.warning("SlackDispatcher: non-200 response %s", response.status)
        except urllib.error.URLError as exc:
            LOGGER.error("SlackDispatcher: failed to deliver alert — %s", exc)


class CompositeDispatcher(AlertDispatcher):
    """Fan-out to multiple dispatchers; failures in one don't abort others."""

    def __init__(self, *dispatchers: AlertDispatcher) -> None:
        self.dispatchers: list[AlertDispatcher] = list(dispatchers)

    def dispatch(self, title: str, violations: list[dict[str, Any]]) -> None:
        for dispatcher in self.dispatchers:
            try:
                dispatcher.dispatch(title, violations)
            except Exception as exc:  # pragma: no cover
                LOGGER.error("AlertDispatcher error in %s: %s", type(dispatcher).__name__, exc)


def build_default_dispatcher() -> AlertDispatcher:
    """Build the default dispatcher from environment configuration.

    If ``SLACK_WEBHOOK_URL`` is set, returns a :class:`CompositeDispatcher` that
    logs *and* posts to Slack.  Otherwise returns a plain :class:`LogDispatcher`.
    """
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if webhook_url:
        return CompositeDispatcher(LogDispatcher(), SlackDispatcher(webhook_url))
    return LogDispatcher()
