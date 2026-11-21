# AmberClaw project notes

## Deployment
- Server: `ubuntu@13.127.103.201`, key `C:\Users\rites\.ssh\amberclaw-key.pem`
- UI: `http://13.127.103.201:10000`, Basic auth `admin` (password in server `.env`)
- Repo on server: `/opt/amberclaw`; deploy: `sudo git pull --ff-only && sudo docker compose -f deploy/aws/docker-compose.yml up -d --build`
- Container `amberclaw`, image `amberclaw:local`; `AMBERCLAW_DATA_DIR=/data` (host `/var/lib/amberclaw`)
- HEAD after viewer work: `fc36173`

## Clip viewer (added 2026-11-21)
- `GET /view?path=<data-path>&title=...` -> `templates/view.html` with `<video>` player
- `GET /media?path=<data-path>` -> inline `FileResponse`, correct video mime, Range/seeking supported (206)
- `GET /download?path=<data-path>` -> attachment download
- View/Download buttons added in `templates/job.html` (per clip) and `templates/index.html` (queue table)
- Paths validated by `_safe_data_path` (must be under `DATA_DIR`); auth via `require_auth`

## Prod bugs fixed
- Job detail page 500 "Internal Server Error": Jinja resolves `u.items` to dict `.items()` method.
  Failed upload dicts have no `items` key, so `u['items']` also falls back to the method.
  Fix: `{% set uitems = u.get('items') %}` then iterate `uitems`.
- OAuth token was mounted read-only at `/app/token.json`; entrypoint copies it to writable
  `/data/token.json` (`YOUTUBE_TOKEN_SOURCE` / `YOUTUBE_TOKEN_FILE`).
- Picker prompt rewritten to always return >=1 moment; Groq 429/empty picks led to agent mode.
- Home-IP HTTP CONNECT proxy over SSH reverse tunnel bypasses YouTube bot-check on AWS IP.

## Known blocker (external, not a code bug)
- YouTube upload fails with `uploadLimitExceeded` ("The user has exceeded the number of videos they may upload.").
  Requires phone verification on the YouTube account (youtube.com/verify) or waiting for the account limit to clear.
  Queue item `5399ee1450d0` holds clip `01_gta-6-what-happens-next.mp4` (76 MB) in
  `/data/StreamClips/2026-11-21/gta-6/unq-gaming__.../`.

## User preferences
- Wants working results fast, minimal explanations; controls `count`/`clips` per job.
- GTA 6 only; Roman Hinglish metadata; never reveal raw Groq keys (masked only).
