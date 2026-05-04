"""Gmail client.

Computes four metrics for the daily report:
- emails_received: incoming messages landing in the inbox during the report day
- replies_sent:    messages sent during the report day with In-Reply-To/References
- avg_response_seconds: avg time between prior inbound and our reply
- still_waiting:   threads where, as of *now*, the latest message is NOT from us
                   (so if you already replied this morning, it drops out)

Performance: the still_waiting check fans out one threads.get per active
thread. We batch those requests in chunks of BATCH_SIZE to keep total
runtime under a minute even with hundreds of active threads.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import BatchHttpRequest

import config


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# How far back to look when computing "still waiting" threads. Threads quiet
# longer than this are considered closed — they won't count even if the last
# message was from someone else, because realistically they're stale.
STILL_WAITING_LOOKBACK_DAYS = 10

# Gmail API allows up to 100 sub-requests per batch. Keep some headroom.
BATCH_SIZE = 50

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


def _batch_get_messages(service, ids: list[str]) -> dict[str, dict]:
    """Fetch many messages in parallel using Gmail's HTTP batch endpoint."""
    results: dict[str, dict] = {}
    errors: list[Exception] = []

    def _callback(request_id, response, exception):
        if exception is not None:
            errors.append(exception)
            return
        results[request_id] = response

    for chunk_start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[chunk_start:chunk_start + BATCH_SIZE]
        batch = service.new_batch_http_request(callback=_callback)
        for mid in chunk:
            batch.add(
                service.users().messages().get(
                    userId=config.GMAIL_USER_ID,
                    id=mid,
                    format="metadata",
                    metadataHeaders=["In-Reply-To", "References", "Date", "From"],
                ),
                request_id=mid,
            )
        batch.execute()

    if errors:
        # Surface the first error rather than silently skipping; better to fail
        # loud than to publish wrong stats.
        raise errors[0]
    return results


def _batch_get_threads(service, ids: list[str]) -> dict[str, list[dict]]:
    """Fetch many threads in parallel. Returns {thread_id: [messages...]}."""
    results: dict[str, list[dict]] = {}
    errors: list[Exception] = []

    def _callback(request_id, response, exception):
        if exception is not None:
            errors.append(exception)
            return
        results[request_id] = response.get("messages", [])

    for chunk_start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[chunk_start:chunk_start + BATCH_SIZE]
        batch = service.new_batch_http_request(callback=_callback)
        for tid in chunk:
            batch.add(
                service.users().threads().get(
                    userId=config.GMAIL_USER_ID,
                    id=tid,
                    format="metadata",
                    metadataHeaders=["From", "Date"],
                ),
                request_id=tid,
            )
        batch.execute()

    if errors:
        raise errors[0]
    return results


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
    """Count messages that landed in the inbox during the window."""
    query = f"in:inbox -in:chats after:{after_ts} before:{before_ts}"
    ids = _list_message_ids(service, query)
    if not ids:
        return 0

    messages = _batch_get_messages(service, ids)
    return sum(1 for m in messages.values() if not _is_sent_by_me(m))


def _count_still_waiting(service) -> int:
    """Count threads where the latest message is NOT from us, as of now.

    Bounded to STILL_WAITING_LOOKBACK_DAYS so we don't scan the entire mailbox.
    Uses batched threads.get to keep runtime bounded.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=STILL_WAITING_LOOKBACK_DAYS)
    after_ts = int(cutoff.timestamp())

    query = f"in:inbox -in:chats after:{after_ts}"
    thread_ids = _list_thread_ids(service, query)
    if not thread_ids:
        return 0

    threads = _batch_get_threads(service, thread_ids)

    waiting = 0
    for messages in threads.values():
        if not messages:
            continue
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
    if not sent_ids:
        return 0, None, 0

    sent_messages = _batch_get_messages(service, sent_ids)

    reply_msgs = {mid: m for mid, m in sent_messages.items() if _is_reply(m)}
    reply_count = len(reply_msgs)

    if reply_count == 0:
        return 0, None, 0

    # Batch-fetch the unique threads we need for response-time calculation.
    thread_ids = list({m["threadId"] for m in reply_msgs.values()})
    thread_cache = _batch_get_threads(service, thread_ids)

    response_seconds: list[float] = []
    for mid, msg in reply_msgs.items():
        thread_msgs = thread_cache.get(msg["threadId"], [])
        full_reply = next((m for m in thread_msgs if m["id"] == mid), None)
        if full_reply is None:
            continue
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
