#!/bin/sh
# Runtime fixups: the host /data mount hides build-time files, so recreate them.
set -e

DATA_DIR="${AMBERCLAW_DATA_DIR:-/data}"
STREAMCLIPS_HOME="${STREAMCLIPS_HOME:-$DATA_DIR/StreamClips}"

mkdir -p "$DATA_DIR" "$STREAMCLIPS_HOME"

if [ ! -f "$STREAMCLIPS_HOME/watchlist.txt" ] && [ -f /app/vendor/stream-clips/watchlist.txt ]; then
  cp /app/vendor/stream-clips/watchlist.txt "$STREAMCLIPS_HOME/watchlist.txt"
fi

exec "$@"
