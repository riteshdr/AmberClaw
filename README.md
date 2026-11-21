# AmberClaw

AmberClaw is a small web app/project wrapper around the [`stream-clips`](vendor/stream-clips/) skill that lets you submit a daily prompt (topic or URLs), generate vertical clips, add creator credit in metadata, and optionally auto-upload clips to your YouTube channel.

> **Important:** clip discovery/generation is powered by the vendored `stream-clips` tool (ffmpeg + yt-dlp + Whisper + OpenCV). Automated YouTube upload requires Google/YouTube API OAuth setup. Review clips and only clip/upload content you have rights/permission to use; credit does not replace permission.

## What it does

- **Web UI / control dashboard** (`/`): preset templates that fill the job form, live job status (queued / processing / done / failed) with filters, stats cards, disk/system health, the daily upload queue with controls, and settings/secret management. Groq keys can be added from the UI and are written only to `.env` (masked in the UI, never logged or committed).
- Runs `stream-clips` in the background as a job and shows live logs/status.
- Collects generated outputs (`clips.json`, `POSTING.md`, `.mp4` clips) from the stream-clips output folder.
- **YouTube upload** (manual per job or auto-upload): uploads generated clips with title/description/tags that include credit to the original creator and source URL.
- Vendored `stream-clips` includes added `--brain groq` (free open-weight GPT-OSS) and `--brain openai` picker backends (OpenAI-compatible Chat Completions API) so jobs can run headlessly on a server without local agent CLIs.

## Repository layout

- `app.py` - FastAPI web UI and job API
- `amberclaw/` - job runner, store, YouTube uploader, daily upload queue and scheduler
- `scripts/daily_upload.py` - run one upload batch from cron / Windows Task Scheduler
- `vendor/stream-clips/` - vendored stream-clips skill/scripts (with `groq`/`openai` brain support added)
- `templates/` - UI templates
- `scripts/youtube_auth.py` - helper to generate YouTube OAuth token
- `data/` - runtime data/jobs and (by default) `StreamClips` outputs (`AMBERCLAW_DATA_DIR`)
- `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore` - container build/runtime
- `deploy/aws/` - AWS EC2 deployment (compose, setup script, runbook) — **recommended**
- `deploy/oracle/` - Oracle Cloud Always Free VM deployment (compose, setup, keepalive, guide)
- `render.yaml` - Render deployment helper (paid plan only)

## Prerequisites

**For clip generation (stream-clips):**
- Python 3.9+
- `ffmpeg` and `ffprobe` on PATH
- A JavaScript runtime for YouTube downloads: `deno` (recommended) or `node`
- Disk space and CPU/RAM for downloads + Whisper transcription + rendering (a few GB recommended; long streams take time)

**For moment picking on a server (headless):**
- A free `GROQ_API_KEY` (https://console.groq.com/keys) is the default. Groq serves open-weight GPT-OSS models (`openai/gpt-oss-20b` by default) through an OpenAI-compatible API, with a free tier that is fast enough for clip picking. Override with `GROQ_MODEL` if needed. Several free keys can be supplied comma-separated (`GROQ_API_KEY=key1,key2,key3`); AmberClaw round-robins across them for both clip picking and Hinglish metadata, which helps when generating 100 clips.
- Optional paid fallback: `OPENAI_API_KEY` (and `OPENAI_BASE_URL`, `OPENAI_MODEL`) with `STREAMCLIPS_BRAIN=openai`. Any OpenAI-compatible chat API following the Chat Completions format also works.
- Alternatively, local agent CLIs (`claude`, `codex`, `gemini`, `opencode`) if installed/logged in (usually local machines, not Render).

**For YouTube auto-upload:**
- Google Cloud project with **YouTube Data API v3** enabled
- OAuth consent screen + OAuth Client ID (Desktop app) credentials (`client_secrets.json`)
- Authorized token with refresh token (`token.json`)

## Local setup

1. **Install system deps**
   - Windows: `winget install Gyan.FFmpeg` and `winget install DenoLand.Deno` (and Python 3.9+)
   - macOS: `brew install ffmpeg deno`

2. **Install AmberClaw Python deps**
   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Set up the vendored stream-clips runtime** (creates its private env at `~/.streamclips` and downloads Whisper/face models on first run):
   ```bash
   python vendor/stream-clips/scripts/setup_env.py
   python vendor/stream-clips/scripts/streamclips.py doctor
   ```
   Wait for setup to finish/print READY-equivalent success. Install anything reported missing, then re-run.

4. **Configure environment**
   ```bash
   copy .env.example .env   # Windows
   # cp .env.example .env   # macOS/Linux
   ```
   Edit `.env` (at minimum set `GROQ_API_KEY` for the default free/open-weight picker, and YouTube settings if uploading).

5. **(Optional, for uploads) Create YouTube token locally**
   - Download OAuth client credentials as `client_secrets.json` into the project root.
   - Run: `python scripts/youtube_auth.py --client-secrets client_secrets.json --token-file token.json`
   - Complete the browser consent flow. Keep `token.json` and `client_secrets.json` private (they are gitignored).

6. **Run the web UI**
   ```bash
   python app.py
   ```
   Open http://localhost:10000, enter a topic/URLs and prompt, and submit a job. Job logs and results appear on the job page.

## Daily usage

- Open the AmberClaw web UI and submit your daily prompt (topic or URLs + focus). This is the "input prompt" workflow you can run each day.
- Use the default `brain=groq` with a free `GROQ_API_KEY` for automated runs. `brain=agent` will pause waiting for manual answers (exit code 3) and is not suitable for unattended server runs.
- Generated clips, `clips.json`, `POSTING.md`, and check images are written under `STREAMCLIPS_HOME` (default: `<AMBERCLAW_DATA_DIR>/StreamClips/<date>/<topic>/...`).
- Review clips before posting/uploading. Uploads can be triggered per-result from the job page, or auto-uploaded when `AUTO_UPLOAD=true` / checkbox enabled (and YouTube auth is configured). With `UPLOAD_MODE=queue` (default), "auto-upload" means clips are added to the daily queue instead of being uploaded immediately.

### Daily upload queue (20/day default)

YouTube's current Data API quota gives each project a dedicated bucket of **100 `videos.insert` calls/day** (1 unit each) alongside 10,000 units for other endpoints. AmberClaw defaults to a conservative **20 uploads per quota day** (midnight Pacific Time reset); you can raise it further if your channel is phone-verified and your Cloud project has quota.

**Important:** YouTube also enforces a separate *account-level* limit (`uploadLimitExceeded: "The user has exceeded the number of videos they may upload."`). A brand-new or phone-unverified channel can hit this after only a handful of uploads in a rolling 24 h window, regardless of the app cap. When that happens the scheduler automatically pauses uploads for `YOUTUBE_LIMIT_COOLDOWN_HOURS` (default 6), shows the countdown in the UI, and **automatically retries** the affected clips when the cooldown clears — no manual retry needed. Verify your channel's phone number at <https://www.youtube.com/verify> to raise the real limit. AmberClaw decouples generation from uploading:

1. A job generates any number of clips. With `AUTO_UPLOAD=true` and `UPLOAD_MODE=queue`, every finished clip is written to `data/jobs/upload_queue.json` as `pending` — **no YouTube API call is made during generation**.
2. The in-app scheduler wakes every `UPLOAD_TICK_MINUTES` (default 10) and uploads at most `YOUTUBE_DAILY_UPLOAD_LIMIT` (default 20) pending clips, oldest first. Status, links, errors and retry controls are on the **Daily YouTube upload queue** card in the UI (`GET /api/queue`).
3. Clip files must stay on disk until uploaded. The queue stores paths, titles, captions, creator credit and source URL; tags/privacy/template are read from `.env` at upload time, so changing them later applies to not-yet-uploaded clips.

Useful controls: `Run batch now`, `Pause` / `Resume`, `Scan old clips` (enqueue clips from previous jobs), and per-item `retry` / `remove`. The queue is crash-safe: interrupted uploads are recovered, a file lock prevents two processes uploading the same batch, and failed items never consume the daily counter.

Environment options:

| Variable | Default | Meaning |
|---|---|---|
| `UPLOAD_MODE` | `queue` | `queue` = enqueue and upload daily; `immediate` = upload inside the job |
| `UPLOAD_SCHEDULER_ENABLED` | `true` | Start the in-app scheduler thread on web startup |
| `UPLOAD_TICK_MINUTES` | `10` | How often the scheduler checks the queue |
| `YOUTUBE_DAILY_UPLOAD_LIMIT` | `20` | Max successful uploads per quota day (YouTube allows 100 API uploads/day) |
| `UPLOAD_QUEUE_PATH` | `<DATA_DIR>/jobs/upload_queue.json` | Queue state file |
| `UPLOAD_DRY_RUN` | `false` | Log what would be uploaded without spending quota |
| `AMBERCLAW_ENV_FILE` | `./.env` | Path the dashboard writes secrets to; point at a mounted volume in Docker |

For external cron / Windows Task Scheduler, run one batch and exit:

```bash
python scripts/daily_upload.py            # respects the daily limit
python scripts/daily_upload.py --force    # ignore the paused flag
python scripts/daily_upload.py --loop     # run continuously instead
```

### Captions and video quality

Captions use Whisper `small` with a Hinglish prompt and a Devanagari→Roman fallback, so Hindi/Hinglish streams get natural Roman captions (e.g. `bhai dekh lo`) instead of garbled text. This is independent of the UI language field (which controls the whole-stream scan), so entering `hi` for a Hindi stream no longer forces Devanagari captions. For Devanagari captions, set `"caption_language": "hi"` in `STREAMCLIPS_HOME/settings.json` (all settings are listed in `vendor/stream-clips/scripts/sc/settings.py`).

Output is always **1080x1920 at up to 60 fps**. The source is downloaded at up to 4K (`video_height: 2160`) and downscaled with Lanczos, then encoded with `crf: 18` / `x264_preset: "medium"`. On a slower CPU, set `x264_preset: "fast"` or `video_height: 1080` in settings.json — the canvas stays 1080p either way.

## YouTube upload & credit

### Hinglish titles, descriptions and GTA6 tags (every short)

- For **each** clip, `amberclaw/metadata.py` asks the free Groq GPT-OSS model for a fresh **Roman Hinglish** title (with emoji) and a 2–3 line Roman Hinglish hype description based on the Whisper caption. If Groq is unavailable, a deterministic Hinglish fallback is used so uploads never stall. Generated metadata is cached on the queue item, so retries don't regenerate it.
- `{caption}` in `YOUTUBE_DESCRIPTION_TEMPLATE` receives that Hinglish hype text; the template then appends hashtags, creator credit and the source URL.
- **Every upload gets at least 15 GTA6 tags** (hard-coded in `amberclaw/uploader.py`, e.g. `gta6`, `gta 6`, `grand theft auto 6`, `gta vi`, `gta6 shorts`, `gta6 funny`, `gta6 hindi`, `gta6 hinglish`, …), followed by clip hashtags, `YOUTUBE_TAGS_DEFAULT` and the creator name. Tags are de-duplicated, capped at 30 characters each and 500 characters total, so the 15 GTA6 tags always survive. Set `YOUTUBE_MIN_TAGS` higher to require more.
- Groq needs a real User-Agent and a large token budget for gpt-oss reasoning; both are handled in `metadata.py` and in the vendored moment picker (`vendor/stream-clips/scripts/sc/brains.py`). The picker also rotates multiple `GROQ_API_KEY` values and retries 429 rate limits with backoff. Set `YOUTUBE_META_DEBUG=true` to log any metadata fallback reason.

- The uploader builds a title/description from `clips.json` metadata and includes source creator/title/URL plus the generated caption/hashtags using `YOUTUBE_DESCRIPTION_TEMPLATE`.
- Default privacy is `private` (`YOUTUBE_PRIVACY`). Change to `unlisted`/`public` only when ready.
- **Credit and permission:** the generated metadata credits the original creator, but you must still have permission/right to clip and upload the content and comply with YouTube/community/copyright rules.

## Deploy on AWS EC2 (recommended)

Full console steps, copy-paste commands and cost notes: [`deploy/aws/README.md`](deploy/aws/README.md).
Recommended setup: **ap-south-1 (Mumbai), Ubuntu 24.04, m6i.large (2 vCPU / 8 GB), 100 GB gp3,
Elastic IP, HTTP with Basic auth** (~USD 78/month). The Docker image and all code come from GitHub;
secrets (`.env`, `client_secrets.json`, `token.json`) are copied separately via `scp` and are never
committed. Short version:

```bash
# AWS console: launch the instance, open TCP 22 + 10000, allocate an Elastic IP.
# Local machine: scp -i key.pem .env client_secrets.json token.json ubuntu@<ip>:/tmp/
ssh -i key.pem ubuntu@<ip>
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/riteshdr/AmberClaw.git /opt/amberclaw
sudo install -m 600 /tmp/.env /opt/amberclaw/.env
sudo install -m 600 /tmp/client_secrets.json /tmp/token.json /opt/amberclaw/
sudo bash /opt/amberclaw/deploy/aws/setup.sh
```

Open `http://<elastic-ip>:10000`, log in, and use the **GTA 5 — 100 viral shorts** preset.
The queue uploads 6 clips/day on YouTube's free quota.

## Deploy on Oracle Cloud Always Free (alternative, free forever)

A real always-on VM that fits the whole pipeline: **2 OCPU / 12 GB RAM, 200 GB storage,
10 TB/month egress**, free for the life of the account. Free PaaS tiers (512 MB, no disk,
sleep-on-idle) cannot run Whisper + ffmpeg reliably.

Full steps: [`deploy/oracle/README.md`](deploy/oracle/README.md). Short version:

```bash
# In OCI: create an Ubuntu VM.Standard.A1.Flex (2 OCPU / 12 GB), open TCP 10000
ssh -i ~/.ssh/key.pem ubuntu@<public-ip>
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/riteshdr/AmberClaw.git /opt/amberclaw
sudo bash /opt/amberclaw/deploy/oracle/setup-vm.sh
```

The setup script builds the image, prompts for a free `GROQ_API_KEY`, generates a Basic-auth
password, mounts persistent storage at `/var/lib/amberclaw`, starts the service and installs
an hourly keepalive timer so Oracle doesn't reclaim the free instance as idle.

## Deploy on Render (paid plan only)

`render.yaml` and `Dockerfile` are provided as a starting point, but note the constraints:

- **Resources:** Whisper transcription and ffmpeg rendering are CPU/disk intensive and can take a long time. Use a sufficiently large instance/plan.
- **Persistent disk:** mount a disk at `/data` (as in `render.yaml`) so `StreamClips` outputs, models/cache (`~/.streamclips` may also need persistence depending on setup), and job state survive restarts. Model downloads during build/startup increase deploy time.
- **Long-running jobs:** clip jobs can exceed typical HTTP request expectations; AmberClaw runs them in background threads and the UI polls status. Ensure your service plan allows long-lived processes/background work as needed.
- **Secrets/auth:** set `GROQ_API_KEY` (free, default), `AMBERCLAW_BASIC_AUTH`, and provide `client_secrets.json`/`token.json` securely if using uploads (do not commit them). You may need to generate `token.json` locally and supply it via a secure mechanism appropriate to your deployment.
- **Networking/egress:** yt-dlp must be able to fetch from source platforms.

**Basic steps:**
1. Push this repo to GitHub/GitLab.
2. Create a new Render Blueprint/Web Service (Docker) from the repo, or use `render.yaml`.
3. Configure env vars and a persistent disk mounted to `/data`.
4. Deploy and open the service URL to use the web UI.

If Render resources/limits are insufficient for your clip workloads, run AmberClaw on a local machine or a larger VPS with the same configuration.

## Troubleshooting

- `ffmpeg/ffprobe not found` or YouTube download issues: ensure ffmpeg and deno/node are installed and on PATH; rerun `streamclips doctor`.
- `brain=groq` errors: verify `GROQ_API_KEY` (free from https://console.groq.com/keys), `GROQ_BASE_URL` (default `https://api.groq.com/openai/v1`), and `GROQ_MODEL` (default `openai/gpt-oss-20b`). For heavier picking, try `openai/gpt-oss-120b`.
- `brain=openai` errors: verify `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL`.
- `needs_attention` (agent mode): automated server runs cannot answer agent prompts; switch to `brain=groq` or a configured CLI brain.
- YouTube upload auth errors: re-generate `token.json` with `scripts/youtube_auth.py` and confirm client/token paths and API/scopes.
- See stream-clips docs/issues and `vendor/stream-clips/README.md` / `SKILL.md` for tool-specific behavior.

## License / upstream

`vendor/stream-clips/` contains the stream-clips skill code as provided in this workspace; refer to its upstream/source for licensing and updates.
