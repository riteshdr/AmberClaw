#!/usr/bin/env bash
# Keeps the Oracle Always Free A1 instance above Oracle's idle thresholds.
# Oracle may reclaim an instance idle for 7 days (<20% CPU p95, <20% network,
# <20% memory on A1). A short load burst each hour keeps it active.
set -uo pipefail

LOAD_SECONDS="${KEEPALIVE_LOAD_SECONDS:-240}"

if command -v stress-ng >/dev/null 2>&1; then
  stress-ng --cpu 1 --timeout "${LOAD_SECONDS}s" --quiet >/dev/null 2>&1 || true
else
  end=$((SECONDS + LOAD_SECONDS))
  while [ "$SECONDS" -lt "$end" ]; do
    openssl speed -seconds 20 sha256 >/dev/null 2>&1 || true
  done
fi

curl -fsS --max-time 15 http://127.0.0.1:10000/health >/dev/null 2>&1 || true
curl -fsS --max-time 15 https://example.com >/dev/null 2>&1 || true
