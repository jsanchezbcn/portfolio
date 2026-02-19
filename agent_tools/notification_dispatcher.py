"""
Unified notification dispatcher supporting Telegram, Email, and legacy Slack.
Primary: Telegram (free, instant)
Backup: Email via Gmail SMTP (free)
"""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
import httpx

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    """
    Sends alerts via multiple channels:
    - Telegram (instant, free) — primary
    - Email (SMTP) — backup
    - Slack (legacy support)
    """

    def __init__(self):
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.notification_method = os.getenv("NOTIFICATION_METHOD", "telegram").strip().lower()

        # Email (SMTP)
        self.email_enabled = os.getenv("EMAIL_ENABLED", "false").lower() in ("true", "1", "yes")
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com").strip()
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_username = os.getenv("SMTP_USERNAME", "").strip()
        self.smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
        self.email_to = os.getenv("EMAIL_TO", "").strip()
        self.email_from = os.getenv("EMAIL_FROM", "").strip()

        # Legacy Slack (deprecated)
        self.slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL", "").strip()

    async def send_alert(
        self,
        title: str,
        body: str,
        urgency: str = "info",
        data: Optional[dict] = None,
    ) -> bool:
        """
        Send alert via configured notification method.

        Args:
            title: Alert headline (e.g., "⚠️ Risk Limit Breach")
            body: Alert body text
            urgency: 'green'|'yellow'|'red'|'info' (affects emoji/formatting)
            data: Optional dict with extra fields for email (suggestions, prices, etc.)

        Returns:
            True if sent successfully, False otherwise
        """
        success = True

        # Determine which method to use
        if self.notification_method == "telegram" and self.telegram_token and self.telegram_chat_id:
            success &= await self._send_telegram(title, body, urgency, data)
        elif self.notification_method == "email" and self.email_enabled and self.smtp_username:
            success &= self._send_email(title, body, urgency, data)
        elif self.notification_method == "slack" and self.slack_webhook_url:
            success &= await self._send_slack(title, body, urgency, data)
        else:
            logger.warning(
                f"No notification method configured. "
                f"method={self.notification_method}, "
                f"telegram_ok={bool(self.telegram_token)}, "
                f"email_ok={self.email_enabled and bool(self.smtp_username)}, "
                f"slack_ok={bool(self.slack_webhook_url)}"
            )
            return False

        if self.email_enabled and self.email_enabled and self.smtp_username:
            success &= self._send_email(title, body, urgency, data)

        return success

    async def _send_telegram(
        self, title: str, body: str, urgency: str, data: Optional[dict]
    ) -> bool:
        """Send alert via Telegram Bot API (instant, free)."""
        try:
            emoji_map = {"red": "🔴", "yellow": "🟡", "green": "🟢", "info": "ℹ️"}
            emoji = emoji_map.get(urgency, "📌")

            message = f"{emoji} *{title}*\n\n{body}"

            if data and data.get("suggestions"):
                message += "\n\n*Suggestions:*\n"
                for i, suggestion in enumerate(data["suggestions"][:3], 1):
                    message += f"  {i}. {suggestion}\n"

            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": self.telegram_chat_id,
                        "text": message,
                        "parse_mode": "Markdown",
                    },
                    timeout=5.0,
                )

            if response.status_code == 200:
                logger.info(f"Telegram alert sent: {title}")
                return True
            else:
                logger.error(
                    f"Telegram send failed: {response.status_code} {response.text[:200]}"
                )
                return False

        except Exception as e:
            logger.error(f"Telegram send error: {type(e).__name__} {str(e)[:200]}")
            return False

    def _send_email(self, title: str, body: str, urgency: str, data: Optional[dict]) -> bool:
        """Send detailed alert report via Gmail SMTP (free backup)."""
        try:
            if not (self.smtp_username and self.email_to):
                logger.warning("Email not fully configured (missing username or recipient)")
                return False

            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"[{urgency.upper()}] {title}"
            msg["From"] = self.email_from or self.smtp_username
            msg["To"] = self.email_to

            urgency_color = {
                "red": "#dc3545",
                "yellow": "#ffc107",
                "green": "#28a745",
                "info": "#17a2b8",
            }.get(urgency, "#6c757d")

            html = f"""
            <html>
              <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <div style="background-color: {urgency_color}; padding: 15px; color: white; border-radius: 5px;">
                  <h2 style="margin: 0;">{title}</h2>
                </div>
                <div style="padding: 20px; background-color: #f9f9f9; border: 1px solid #ddd; border-radius: 5px; margin-top: 10px;">
                  <p>{body.replace(chr(10), '<br>')}</p>
                </div>
            """

            if data and data.get("suggestions"):
                html += "<div style='padding: 20px; background-color: #f0f8ff; border-left: 4px solid #2196F3; margin-top: 10px;'>"
                html += "<h3>Suggestions:</h3><ul>"
                for suggestion in data["suggestions"][:5]:
                    html += f"<li>{suggestion}</li>"
                html += "</ul></div>"

            html += """
              </body>
            </html>
            """

            part = MIMEText(html, "html")
            msg.attach(part)

            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_username, self.smtp_password)
                server.send_message(msg)

            logger.info(f"Email alert sent to {self.email_to}: {title}")
            return True

        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"Email auth failed: check SMTP credentials. {str(e)[:200]}")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error: {str(e)[:200]}")
            return False
        except Exception as e:
            logger.error(f"Email send error: {type(e).__name__} {str(e)[:200]}")
            return False

    async def _send_slack(
        self, title: str, body: str, urgency: str, data: Optional[dict]
    ) -> bool:
        """Send alert to Slack webhook (legacy/deprecated)."""
        try:
            color_map = {"red": "#dc3545", "yellow": "#ffc107", "green": "#28a745", "info": ""}
            color = color_map.get(urgency, "")

            blocks = [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": title, "emoji": True},
                }
            ]

            if color:
                blocks.append(
                    {"type": "divider"},
                )

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": body},
            })

            if data and data.get("suggestions"):
                suggestions_text = "\n".join([f"• {s}" for s in data["suggestions"][:3]])
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Suggestions:*\n{suggestions_text}"},
                })

            payload = {"blocks": blocks}
            if color:
                payload["attachments"] = [{"color": color}]

            async with httpx.AsyncClient() as client:
                response = await client.post(self.slack_webhook_url, json=payload, timeout=5.0)

            if response.status_code == 200:
                logger.info(f"Slack alert sent: {title}")
                return True
            else:
                logger.error(f"Slack send failed: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Slack send error: {type(e).__name__} {str(e)[:200]}")
            return False


# Singleton instance
_dispatcher = None


def get_dispatcher() -> NotificationDispatcher:
    """Get or create dispatcher instance."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = NotificationDispatcher()
    return _dispatcher


# Convenience function for quick alerts
async def send_alert(
    title: str,
    body: str,
    urgency: str = "info",
    data: Optional[dict] = None,
) -> bool:
    """Quick alert send (uses singleton dispatcher)."""
    return await get_dispatcher().send_alert(title, body, urgency, data)
