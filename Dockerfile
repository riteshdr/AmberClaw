# AmberClaw - web UI + stream-clips + YouTube upload
# Target: small always-on VPS / Oracle Always Free A1 (2 vCPU, 12 GB RAM).
# Clip generation is CPU/disk intensive; free PaaS tiers (512 MB) are not enough.

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STREAMCLIPS_HOME=/data/StreamClips \
    AMBERCLAW_DATA_DIR=/data \
    DENO_INSTALL=/root/.deno

# ffmpeg/ffprobe for media, deno for yt-dlp's YouTube JS challenges.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates unzip git tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="$DENO_INSTALL/bin:${PATH}"

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

# Private venv + Whisper/face models (~220 MB) are baked into the image.
# Fail the build loudly if this breaks: a silent failure makes every job fail at runtime.
RUN python vendor/stream-clips/scripts/setup_env.py

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-10000}/health" >/dev/null || exit 1

ENTRYPOINT ["sh", "/app/docker-entrypoint.sh"]
CMD ["python", "app.py"]
