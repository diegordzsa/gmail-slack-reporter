"""Gmail client.

Computes four metrics for the daily report:
- emails_received: incoming messages landing in the inbox during the report day
- replies_sent:    messages sent during the report day with In-Reply-To/References
- avg_response_seconds: avg time between prior inbound and our reply
- still_waiting:   threads where, as of *now*, the latest message is NOT from us
                   (so if you already replied this morning, it drops out)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import config


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# How far back to look when computing "still waiting" threads. Threads that
# have been quiet for longer than this are considered closed — they won't
# count even if the last message was from someone else, because realistically
# they're stale, not "still waiting".
STILL_WAITING_LOOKBACK_DAYS = 14

SENT_LABEL = "SENT"
INBOX_LABEL = "INBOX"
CHAT_LABEL = "CHAT"


@dataclass
class ReportStats:
    emails_received: int
    total_replies: int
    still_waiting: int
    avg_response_seconds: float | None  # None if no measurable replies
    sample_size: int  # number of replies that contributed to avg

    @property
    def avg_response_human(self) -> str:
        if self.avg_response_seconds is None:
            return "N/A"
        return _format_duration(self.avg_response_seconds)


# Backwards-compat alias — older imports may still reference ReplyStats.
ReplyStats = ReportStats


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


def _list_message_ids(service, query: str) -> list[str]:
    """List all message IDs matching a Gmail search query."""
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


def _list_thread_ids(service, query: str) -> list[str]:
    """List all thread IDs matching a Gmail search query."""
    ids: list[str] = []
    page_token = None
    while True:
        resp = service.users().threads().list(
            userId=config.GMAIL_USER_ID,
            q=query,
            pageToken=page_token,
            maxResults=500,
        ).execute()
        ids.extend(t["id"] for t in resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _get_message(service, message_id: str) -> dict:
    """Fetch message metadata. Includes labelIds, internalDate, headers."""
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


def _is_sent_by_me(msg: dict) -> bool:
    """True if Gmail tagged this message with the SENT label (i.e. we sent it)."""
    return SENT_LABEL in msg.get("labelIds", [])


def _previous_inbound_in_thread(thread_messages: list[dict], reply_msg: dict) -> dict | None:
    """Find the most recent inbound message in the thread that came before reply_msg."""
    reply_ts = _internal_date_ms(reply_msg)
    candidates = [
        m for m in thread_messages
        if _internal_date_ms(m) < reply_ts and not _is_sent_by_me(m)
    ]
    if not candidates:
        return None
    return max(candidates, key=_internal_date_ms)


def _count_emails_received(service, after_ts: int, before_ts: int) -> int:
    """Count messages that landed in the inbox during the window.

    Excludes chats (Gmail surfaces Hangouts/Chat messages via the API too)
    and anything we sent ourselves (rare in inbox, but possible via filters
    or self-addressed mail).
    """
    query = f"in:inbox -in:chats after:{after_ts} before:{before_ts}"
    ids = _list_message_ids(service, query)
    if not ids:
        return 0

    # Defensive filter: drop anything that has the SENT label even if it
    # somehow showed up in inbox (e.g. mailing-list loops).
    count = 0
    for mid in ids:
        msg = _get_message(service, mid)
        if not _is_sent_by_me(msg):
            count += 1
    return count


def _count_still_waiting(service) -> int:
    """Count threads where the latest message is NOT from us, as of now.

    We bound the search to the last STILL_WAITING_LOOKBACK_DAYS so we don't
    scan the entire mailbox. A thread quiet for >2 weeks isn't "waiting" —
    it's abandoned.

    Checks the FULL thread including today's messages, so if you replied
    earlier today the thread correctly drops out of the count.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=STILL_WAITING_LOOKBACK_DAYS)
    after_ts = int(cutoff.timestamp())

    # Threads that received any inbound activity in the lookback window.
    query = f"in:inbox -in:chats after:{after_ts}"
    thread_ids = _list_thread_ids(service, query)

    waiting = 0
    for tid in thread_ids:
        messages = _get_thread_messages(service, tid)
        if not messages:
            continue
        # Gmail returns thread messages chronologically, but sort defensively.
        latest = max(messages, key=_internal_date_ms)
        if not _is_sent_by_me(latest):
            waiting += 1
    return waiting


def _collect_reply_stats(service, target_date) -> tuple[int, float | None, int]:
    """Returns (reply_count, avg_response_seconds, sample_size) for target_date."""
    start_utc, end_utc = _day_bounds_utc(target_date)
    after_ts = int(start_utc.timestamp())
    before_ts = int(end_utc.timestamp())

    sent_query = f"in:sent after:{after_ts} before:{before_ts}"
    sent_ids = _list_message_ids(service, sent_query)

    reply_count = 0
    response_seconds: list[float] = []
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

        full_reply = next((m for m in thread_msgs if m["id"] == mid), msg)
        prior = _previous_inbound_in_thread(thread_msgs, full_reply)
        if prior is not None:
            delta_ms = _internal_date_ms(full_reply) - _internal_date_ms(prior)
            if delta_ms > 0:
                response_seconds.append(delta_ms / 1000.0)

    avg = sum(response_seconds) / len(response_seconds) if response_seconds else None
    return reply_count, avg, len(response_seconds)


def collect_report_stats(target_date) -> ReportStats:
    """Main entry point. Returns full ReportStats for the given local date."""
    service = _build_service()
    start_utc, end_utc = _day_bounds_utc(target_date)
    after_ts = int(start_utc.timestamp())
    before_ts = int(end_utc.timestamp())

    emails_received = _count_emails_received(service, after_ts, before_ts)
    reply_count, avg, sample_size = _collect_reply_stats(service, target_date)
    still_waiting = _count_still_waiting(service)

    return ReportStats(
        emails_received=emails_received,
        total_replies=reply_count,
        still_waiting=still_waiting,
        avg_response_seconds=avg,
        sample_size=sample_size,
    )


# Backwards-compat alias — old name kept so any external call sites don't break.
collect_reply_stats = collect_report_stats
