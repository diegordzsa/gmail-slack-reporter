"""Gmail client.

Computes four metrics for the daily report:
- emails_received: incoming messages landing in the inbox during the report day
- replies_sent:    messages sent during the report day with In-Reply-To/References
- avg_response_seconds: avg time between prior inbound and our reply
- still_waiting:   threads where, as of *now*, the latest message is NOT from us
                   (so if you already replied this morning, it drops out)

Performance: the still_waiting check fans out one threads.get per active
thread. We batch those requests in small chunks with retry-on-429 to stay
under Gmail's per-user concurrency limit.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# How far back to look when computing "still waiting" threads.
STILL_WAITING_LOOKBACK_DAYS = 10

# Gmail enforces a per-user concurrency limit (~10–25 simultaneous requests)
# on top of the documented 100/batch limit. Stay well below it.
BATCH_SIZE = 10

# Gentle pause between batches to avoid stacking concurrency.
BATCH_PAUSE_SECONDS = 0.4

# Retry config for 429/5xx responses.
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0

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


# Backwards-compat alias.
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
    start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=config.REPORT_TIMEZONE)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _is_retryable_http_error(exc: Exception) -> bool:
    """429 (rate limit) and 5xx (server) are retryable. Everything else is not."""
    if not isinstance(exc, HttpError):
        return False
    status = getattr(exc.resp, "status", None)
    if status is None:
        return False
    return status == 429 or 500 <= status < 600


def _execute_with_retry(request):
    """Execute a single API request with exponential backoff on 429/5xx."""
    backoff = INITIAL_BACKOFF_SECONDS
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return request.execute()
        except HttpError as exc:
            last_exc = exc
            if not _is_retryable_http_error(exc):
                raise
            sleep_for = backoff + random.uniform(0, 0.5)
            time.sleep(sleep_for)
            backoff *= 2
    assert last_exc is not None
    raise last_exc


def _list_message_ids(service, query: str) -> list[str]:
    ids: list[str] = []
    page_token = None
    while True:
        resp = _execute_with_retry(
            service.users().messages().list(
                userId=config.GMAIL_USER_ID,
                q=query,
                pageToken=page_token,
                maxResults=500,
            )
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _list_thread_ids(service, query: str) -> list[str]:
    ids: list[str] = []
    page_token = None
    while True:
        resp = _execute_with_retry(
            service.users().threads().list(
                userId=config.GMAIL_USER_ID,
                q=query,
                pageToken=page_token,
                maxResults=500,
            )
        )
        ids.extend(t["id"] for t in resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _run_batch(service, items: list[str], request_factory) -> dict[str, dict]:
    """Generic batch runner with per-batch retry on 429/5xx.

    request_factory(item_id) returns the prepared API request to add to the batch.
    """
    results: dict[str, dict] = {}

    for chunk_start in range(0, len(items), BATCH_SIZE):
        chunk = items[chunk_start:chunk_start + BATCH_SIZE]
        # Track which items in this chunk still need to be fetched.
        pending = list(chunk)

        backoff = INITIAL_BACKOFF_SECONDS
        for attempt in range(MAX_RETRIES):
            errors_in_chunk: dict[str, Exception] = {}

            def _callback(request_id, response, exception, _errors=errors_in_chunk):
                if exception is not None:
                    _errors[request_id] = exception
                else:
                    results[request_id] = response

            batch = service.new_batch_http_request(callback=_callback)
            for item_id in pending:
                batch.add(request_factory(item_id), request_id=item_id)
            batch.execute()

            # Determine which items hit retryable errors.
            retryable = [rid for rid, exc in errors_in_chunk.items() if _is_retryable_http_error(exc)]
            non_retryable = {rid: exc for rid, exc in errors_in_chunk.items() if not _is_retryable_http_error(exc)}

            if non_retryable:
                # Fail loud on real errors (auth, not-found, etc.) — don't publish wrong stats.
                raise next(iter(non_retryable.values()))

            if not retryable:
                break  # whole chunk done

            # Wait, then retry only the items that were rate-limited.
            pending = retryable
            time.sleep(backoff + random.uniform(0, 0.5))
            backoff *= 2
        else:
            raise RuntimeError(
                f"Gave up after {MAX_RETRIES} retries on rate-limited batch "
                f"({len(pending)} items still failing)"
            )

        time.sleep(BATCH_PAUSE_SECONDS)

    return results


def _batch_get_messages(service, ids: list[str]) -> dict[str, dict]:
    """Fetch many messages with batched HTTP and 429 retry."""
    def factory(mid: str):
        return service.users().messages().get(
            userId=config.GMAIL_USER_ID,
            id=mid,
            format="metadata",
            metadataHeaders=["In-Reply-To", "References", "Date", "From"],
        )
    return _run_batch(service, ids, factory)


def _batch_get_threads(service, ids: list[str]) -> dict[str, list[dict]]:
    """Fetch many threads with batched HTTP and 429 retry. Returns {tid: [messages]}."""
    def factory(tid: str):
        return service.users().threads().get(
            userId=config.GMAIL_USER_ID,
            id=tid,
            format="metadata",
            metadataHeaders=["From", "Date"],
        )
    raw = _run_batch(service, ids, factory)
    return {tid: resp.get("messages", []) for tid, resp in raw.items()}


def _header(msg: dict, name: str) -> str | None:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def _is_reply(msg: dict) -> bool:
    return bool(_header(msg, "In-Reply-To") or _header(msg, "References"))


def _internal_date_ms(msg: dict) -> int:
    return int(msg["internalDate"])


def _is_sent_by_me(msg: dict) -> bool:
    return SENT_LABEL in msg.get("labelIds", [])


def _previous_inbound_in_thread(thread_messages: list[dict], reply_msg: dict) -> dict | None:
    reply_ts = _internal_date_ms(reply_msg)
    candidates = [
        m for m in thread_messages
        if _internal_date_ms(m) < reply_ts and not _is_sent_by_me(m)
    ]
    if not candidates:
        return None
    return max(candidates, key=_internal_date_ms)


def _count_emails_received(service, after_ts: int, before_ts: int) -> int:
    query = f"in:inbox -in:chats after:{after_ts} before:{before_ts}"
    ids = _list_message_ids(service, query)
    if not ids:
        return 0
    messages = _batch_get_messages(service, ids)
    return sum(1 for m in messages.values() if not _is_sent_by_me(m))


def _count_still_waiting(service) -> int:
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


# Backwards-compat alias.
collect_reply_stats = collect_report_stats
