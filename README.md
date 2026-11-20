# AmberClaw

AmberClaw is a small web app/project wrapper around the [`stream-clips`](vendor/stream-clips/) skill that lets you submit a daily prompt (topic or URLs), generate vertical clips, add creator credit in metadata, and optionally auto-upload clips to your YouTube channel.

> **Important:** clip discovery/generation is powered by the vendored `stream-clips` tool (ffmpeg + yt-dlp + Whisper + OpenCV). Automated YouTube upload requires Google/YouTube API OAuth setup. Review clips and only clip/upload content you have rights/permission to use; credit does not replace permission.

## What it does

- **Web UI** (`/`) to submit a daily prompt: topic/search (e.g. "Valorant clutch moments") and/or specific stream/VOD URLs, focus, clip count, search window, language, etc.
- Runs `stream-clips` in the background as a job and shows live logs/status.
- Collects generated outputs (`clips.json`, `POSTING.md`, `.mp4` clips) from the stream-clips output folder.
- **YouTube upload** (manual per job or auto-upload): uploads generated clips with title/description/tags that include credit to the original creator and source URL.
- Vendored `stream-clips` includes added `--brain groq` (free open-weight GPT-OSS) and `--brain openai` picker backends (OpenAI-compatible Chat Completions API) so jobs can run headlessly on a server without local agent CLIs.

## Repository layout

- `app.py` - FastAPI web UI and job API
- `amberclaw/` - job runner, store, YouTube uploader
- `vendor/stream-clips/` - vendored stream-clips skill/scripts (with `groq`/`openai` brain support added)
- `templates/` - UI templates
- `scripts/youtube_auth.py` - helper to generate YouTube OAuth token
- `data/` - runtime data/jobs and (by default) `StreamClips` outputs (`AMBERCLAW_DATA_DIR`)
- `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore` - container build/runtime
- `deploy/oracle/` - Oracle Cloud Always Free VM deployment (compose, setup, keepalive, guide)
- `render.yaml` - Render deployment helper (paid plan only)

## Prerequisites

**For clip generation (stream-clips):**
- Python 3.9+
- `ffmpeg` and `ffprobe` on PATH
- A JavaScript runtime for YouTube downloads: `deno` (recommended) or `node`
- Disk space and CPU/RAM for downloads + Whisper transcription + rendering (a few GB recommended; long streams take time)

**For moment picking on a server (headless):**
- A free `GROQ_API_KEY` (https://console.groq.com/keys) is the default. Groq serves open-weight GPT-OSS models (`openai/gpt-oss-20b` by default) through an OpenAI-compatible API, with a free tier that is fast enough for clip picking. Override with `GROQ_MODEL` if needed.
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
- Review clips before posting/uploading. Uploads can be triggered per-result from the job page, or auto-uploaded when `AUTO_UPLOAD=true` / checkbox enabled (and YouTube auth is configured).

### Captions and video quality

Captions use Whisper `small` with a Hinglish prompt and a Devanagari→Roman fallback, so Hindi/Hinglish streams get natural Roman captions (e.g. `bhai dekh lo`) instead of garbled text. This is independent of the UI language field (which controls the whole-stream scan), so entering `hi` for a Hindi stream no longer forces Devanagari captions. For Devanagari captions, set `"caption_language": "hi"` in `STREAMCLIPS_HOME/settings.json` (all settings are listed in `vendor/stream-clips/scripts/sc/settings.py`).

Output is always **1080x1920 at up to 60 fps**. The source is downloaded at up to 4K (`video_height: 2160`) and downscaled with Lanczos, then encoded with `crf: 18` / `x264_preset: "medium"`. On a slower CPU, set `x264_preset: "fast"` or `video_height: 1080` in settings.json — the canvas stays 1080p either way.

## YouTube upload & credit

- The uploader builds a title/description from `clips.json` metadata and includes source creator/title/URL plus the generated caption/hashtags using `YOUTUBE_DESCRIPTION_TEMPLATE`.
- Default privacy is `private` (`YOUTUBE_PRIVACY`). Change to `unlisted`/`public` only when ready.
- **Credit and permission:** the generated metadata credits the original creator, but you must still have permission/right to clip and upload the content and comply with YouTube/community/copyright rules.

## Deploy on Oracle Cloud Always Free (recommended, free forever)

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
