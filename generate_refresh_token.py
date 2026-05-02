"""ONE-TIME local script. Run on your laptop to generate a refresh token,
then store it as a GitHub Actions secret (GMAIL_REFRESH_TOKEN).

Setup:
    1. Download your OAuth client JSON from Google Cloud Console
       (APIs & Services → Credentials → OAuth client ID, Desktop app type).
    2. Save it as `client_secret.json` in this folder.
    3. Run: python generate_refresh_token.py
    4. A browser window opens — sign in with the Gmail account you want to track.
    5. The script prints CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN. Copy them
       into GitHub Actions secrets, then DELETE client_secret.json and token.json.

This file is for local setup only — do NOT commit secrets.
"""
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CLIENT_SECRET_FILE = "client_secret.json"


def main() -> None:
    if not Path(CLIENT_SECRET_FILE).exists():
        raise SystemExit(
            f"Missing {CLIENT_SECRET_FILE}. Download it from Google Cloud Console."
        )

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    with open(CLIENT_SECRET_FILE) as f:
        client_data = json.load(f)
    installed = client_data.get("installed") or client_data.get("web") or {}

    print("\n" + "=" * 60)
    print("COPY THESE INTO GITHUB ACTIONS SECRETS:")
    print("=" * 60)
    print(f"GMAIL_CLIENT_ID:     {installed.get('client_id', '')}")
    print(f"GMAIL_CLIENT_SECRET: {installed.get('client_secret', '')}")
    print(f"GMAIL_REFRESH_TOKEN: {creds.refresh_token}")
    print("=" * 60)
    print("\nThen delete client_secret.json from this folder.")


if __name__ == "__main__":
    main()
