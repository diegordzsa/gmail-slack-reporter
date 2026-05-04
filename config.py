"""Configuration loaded from environment variables."""
import os
from zoneinfo import ZoneInfo

# Gmail OAuth credentials
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")

# Slack webhook
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Reporting timezone — the report covers a calendar day in this timezone
REPORT_TIMEZONE = ZoneInfo(os.environ.get("REPORT_TIMEZONE", "Europe/Madrid"))

# Gmail user to read from. "me" = the authenticated user.
GMAIL_USER_ID = "me"


def validate() -> None:
    """Fail fast if any required env var is missing."""
    missing = [
        name for name, value in {
            "GMAIL_CLIENT_ID": GMAIL_CLIENT_ID,
            "GMAIL_CLIENT_SECRET": GMAIL_CLIENT_SECRET,
            "GMAIL_REFRESH_TOKEN": GMAIL_REFRESH_TOKEN,
            "SLACK_WEBHOOK_URL": SLACK_WEBHOOK_URL,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")
