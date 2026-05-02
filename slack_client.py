"""Slack client. Sends the daily report via Incoming Webhook using Block Kit."""
from __future__ import annotations

from datetime import date

import requests

import config
from gmail_client import ReplyStats


def build_blocks(stats: ReplyStats, report_date: date) -> list[dict]:
    date_str = report_date.strftime("%A, %b %d %Y")
    sample_note = (
        f"_Avg based on {stats.sample_size} reply(ies) with prior inbound message_"
        if stats.sample_size > 0
        else "_No measurable response times today_"
    )
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📬 Daily Email Report"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"*{date_str}*"}],
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Replies sent*\n{stats.total_replies}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Avg response time*\n{stats.avg_response_human}",
                },
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": sample_note}],
        },
    ]


def send_report(stats: ReplyStats, report_date: date) -> None:
    payload = {
        "text": f"Daily Email Report — {stats.total_replies} replies sent",
        "blocks": build_blocks(stats, report_date),
    }
    response = requests.post(config.SLACK_WEBHOOK_URL, json=payload, timeout=15)
    response.raise_for_status()
