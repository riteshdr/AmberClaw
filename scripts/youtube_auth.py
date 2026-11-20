"""One-time helper to create/refresh token.json for YouTube uploads.

Run locally (where a browser is available):
  python scripts/youtube_auth.py --client-secrets client_secrets.json --token-file token.json

Prereqs:
- Google Cloud project with YouTube Data API v3 enabled
- OAuth consent screen configured
- OAuth client ID credentials (Desktop app) downloaded as client_secrets.json
"""

import argparse

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-secrets", default="client_secrets.json")
    ap.add_argument("--token-file", default="token.json")
    args = ap.parse_args()
    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(args.token_file, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print(f"Saved YouTube token to {args.token_file}")


if __name__ == "__main__":
    main()
