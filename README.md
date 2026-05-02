# gmail-slack-reporter

Daily report of email replies sent from Gmail, posted to Slack at 9am.

Tracks:
- Total replies sent during the prior calendar day
- Average response time (time between the last inbound message in a thread and your reply)

A "reply" = a sent message with an `In-Reply-To` or `References` header.

## Setup

### 1. Slack webhook

1. Go to <https://api.slack.com/apps> → Create New App → From scratch.
2. Enable **Incoming Webhooks**, create a webhook for the channel you want.
3. Copy the webhook URL — it's your `SLACK_WEBHOOK_URL`.

### 2. Gmail OAuth credentials

1. Go to <https://console.cloud.google.com/> → create a project.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **APIs & Services → OAuth consent screen** → External, fill required fields, add your Gmail as a test user.
4. **APIs & Services → Credentials** → Create Credentials → OAuth client ID → **Desktop app**.
5. Download the JSON, save as `client_secret.json` in this folder.

### 3. Generate a refresh token (one-time, local)

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python generate_refresh_token.py
```

A browser opens — sign in with the Gmail account to track. The script prints three values.

**Delete `client_secret.json` after this step.** Don't commit it.

### 4. GitHub Actions secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**. Add:

- `GMAIL_CLIENT_ID`
- `GMAIL_CLIENT_SECRET`
- `GMAIL_REFRESH_TOKEN`
- `SLACK_WEBHOOK_URL`

### 5. Schedule

The workflow at `.github/workflows/daily-report.yml` runs at **07:00 UTC** = 9am Madrid time during CEST (most of the year). During CET (late Oct–late Mar) it'll run at 8am local. If the 1h drift bothers you, change the cron seasonally or run the workflow on multiple cron lines and add a tz check inside `main.py`.

You can also trigger it manually: **Actions → Daily Email Report → Run workflow**.

## Local testing

Set the env vars in a `.env` or export them, then:

```bash
python test_report_preview.py            # preview yesterday — does NOT post to Slack
python test_report_preview.py today      # preview partial today
python main.py                           # full run, posts to Slack
```

## File overview

| File | Purpose |
|------|---------|
| `main.py` | Entry point — daily orchestrator |
| `gmail_client.py` | Lists sent messages, filters replies, computes avg response time |
| `slack_client.py` | Builds and posts the Slack Block Kit message |
| `config.py` | Loads env vars, validates them |
| `test_report_preview.py` | Local dry-run, prints stats and Slack payload |
| `generate_refresh_token.py` | One-time OAuth helper |
| `.github/workflows/daily-report.yml` | Scheduled trigger |

## Tweaks

- **Different timezone**: change `REPORT_TIMEZONE` env var (any IANA tz name).
- **Different definition of "reply"**: edit `_is_reply` in `gmail_client.py`. E.g., to require strictly that a prior inbound message exists in the thread, use the thread-based check instead of headers.
- **Add per-recipient breakdown**: `gmail_client.py` already has the data — add a `Counter` over recipient domains and pass it to `slack_client.build_blocks`.
- **Filter out internal/team replies**: in `_is_reply`, check the `To` header against a domain blocklist.
