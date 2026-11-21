#!/bin/sh
# Runtime fixups: the host /data mount hides build-time files, so recreate them.
set -e

DATA_DIR="${AMBERCLAW_DATA_DIR:-/data}"
STREAMCLIPS_HOME="${STREAMCLIPS_HOME:-$DATA_DIR/StreamClips}"

mkdir -p "$DATA_DIR" "$STREAMCLIPS_HOME"

# YouTube OAuth refresh writes the token back; /app/token.json is mounted read-only,
# so keep a writable copy on the persistent data volume.
TOKEN_SOURCE="${YOUTUBE_TOKEN_SOURCE:-/app/token.json}"
TOKEN_TARGET="${YOUTUBE_TOKEN_FILE:-$DATA_DIR/token.json}"
if [ ! -f "$TOKEN_TARGET" ] && [ -f "$TOKEN_SOURCE" ]; then
  cp "$TOKEN_SOURCE" "$TOKEN_TARGET"
fi

if [ ! -f "$STREAMCLIPS_HOME/watchlist.txt" ] && [ -f /app/vendor/stream-clips/watchlist.txt ]; then
  cp /app/vendor/stream-clips/watchlist.txt "$STREAMCLIPS_HOME/watchlist.txt"
fi

exec "$@"
