"""Gmail client. Counts replies sent during a given calendar day and
computes average response time (time between the previous inbound message
in the thread and the user's reply)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import config


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class ReplyStats:
    total_replies: int
    avg_response_seconds: float | None  # None if no measurable replies
    sample_size: int  # number of replies that contributed to avg

    @property
    def avg_response_human(self) -> str:
        if self.avg_response_seconds is None:
            return "N/A"
        return _format_duration(self.avg_response_seconds)


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours == 0:
        return f"{minutes}m"
    return f"{hours}h {minutes}m"


def _build_service():
    creds = Credentials(
        token=None,
        refresh_token=config.GMAIL_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.GMAIL_CLIENT_ID,
        client_secret=config.GMAIL_CLIENT_SECRET,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _day_bounds_utc(target_date) -> tuple[datetime, datetime]:
    """Return UTC datetime bounds for a given local calendar date."""
    start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=config.REPORT_TIMEZONE)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _list_sent_message_ids(service, after_ts: int, before_ts: int) -> list[str]:
    """List all SENT message IDs within the timestamp range (epoch seconds)."""
    query = f"in:sent after:{after_ts} before:{before_ts}"
    ids: list[str] = []
    page_token = None
    while True:
        resp = service.users().messages().list(
            userId=config.GMAIL_USER_ID,
            q=query,
            pageToken=page_token,
            maxResults=500,
        ).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _get_message(service, message_id: str) -> dict:
    return service.users().messages().get(
        userId=config.GMAIL_USER_ID,
        id=message_id,
        format="metadata",
        metadataHeaders=["In-Reply-To", "References", "Date", "From"],
    ).execute()


def _get_thread_messages(service, thread_id: str) -> list[dict]:
    resp = service.users().threads().get(
        userId=config.GMAIL_USER_ID,
        id=thread_id,
        format="metadata",
        metadataHeaders=["From", "Date"],
    ).execute()
    return resp.get("messages", [])


def _header(msg: dict, name: str) -> str | None:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def _is_reply(msg: dict) -> bool:
    """A message is a reply if it has In-Reply-To or References header."""
    return bool(_header(msg, "In-Reply-To") or _header(msg, "References"))


def _internal_date_ms(msg: dict) -> int:
    return int(msg["internalDate"])


def _previous_inbound_in_thread(thread_messages: list[dict], reply_msg: dict) -> dict | None:
    """Find the most recent inbound message in the thread that came before reply_msg."""
    reply_ts = _internal_date_ms(reply_msg)
    sent_label = "SENT"
    candidates = [
        m for m in thread_messages
        if _internal_date_ms(m) < reply_ts and sent_label not in m.get("labelIds", [])
    ]
    if not candidates:
        return None
    return max(candidates, key=_internal_date_ms)


def collect_reply_stats(target_date) -> ReplyStats:
    """Main entry point. Returns ReplyStats for the given local date."""
    service = _build_service()
    start_utc, end_utc = _day_bounds_utc(target_date)
    after_ts = int(start_utc.timestamp())
    before_ts = int(end_utc.timestamp())

    sent_ids = _list_sent_message_ids(service, after_ts, before_ts)

    reply_count = 0
    response_seconds: list[float] = []

    # Cache thread fetches — many replies may belong to the same thread
    thread_cache: dict[str, list[dict]] = {}

    for mid in sent_ids:
        msg = _get_message(service, mid)
        if not _is_reply(msg):
            continue
        reply_count += 1

        thread_id = msg["threadId"]
        if thread_id not in thread_cache:
            thread_cache[thread_id] = _get_thread_messages(service, thread_id)
        thread_msgs = thread_cache[thread_id]

        # The msg from list() doesn't include internalDate when fetched with metadata only,
        # but threads().get returns it — find this message inside the thread for accurate ts
        full_reply = next((m for m in thread_msgs if m["id"] == mid), msg)
        prior = _previous_inbound_in_thread(thread_msgs, full_reply)
        if prior is not None:
            delta_ms = _internal_date_ms(full_reply) - _internal_date_ms(prior)
            if delta_ms > 0:
                response_seconds.append(delta_ms / 1000.0)

    avg = sum(response_seconds) / len(response_seconds) if response_seconds else None
    return ReplyStats(
        total_replies=reply_count,
        avg_response_seconds=avg,
        sample_size=len(response_seconds),
    )
