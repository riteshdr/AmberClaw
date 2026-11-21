#!/usr/bin/env bash
# One-time setup for AmberClaw on an AWS EC2 Ubuntu instance.
# Usage (as root): sudo bash /opt/amberclaw/deploy/aws/setup.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
DATA_DIR=/var/lib/amberclaw
HTTP_PORT=10000
TARGET_USER="${SUDO_USER:-$(whoami)}"
COMPOSE="deploy/aws/docker-compose.yml"

echo "==> AmberClaw AWS EC2 setup"
echo "    repo : $REPO_DIR"
echo "    data : $DATA_DIR"

# --- secrets must be copied from the local machine before setup ---
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE is missing. From your local machine (repo root), run:" >&2
  echo "  scp -i amberclaw-key.pem .env client_secrets.json token.json ubuntu@<elastic-ip>:/tmp/" >&2
  echo "  sudo install -m 600 /tmp/.env /opt/amberclaw/.env" >&2
  exit 1
fi
chmod 600 "$ENV_FILE"

for f in client_secrets.json token.json; do
  if [ ! -f "$REPO_DIR/$f" ]; then
    echo "ERROR: $REPO_DIR/$f is missing (YouTube uploads need it). Copy it from your local machine." >&2
    exit 1
  fi
  chmod 600 "$REPO_DIR/$f"
done

# --- install Docker ---
echo "==> Installing Docker + utilities"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-plugin git curl ca-certificates openssl >/dev/null
systemctl enable --now docker
id -nG "$TARGET_USER" | grep -qw docker || usermod -aG docker "$TARGET_USER"

echo "==> Preparing persistent data directory"
mkdir -p "$DATA_DIR"

# --- replace placeholder Basic auth password if the example value is present ---
if grep -q 'replace-with-a-strong-password' "$ENV_FILE"; then
  ui_pass="$(openssl rand -base64 18 | tr -d '/+=')"
  python3 - "$ENV_FILE" "$ui_pass" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1]); value = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()
out = [f"AMBERCLAW_BASIC_AUTH=admin:{value}" if l.startswith("AMBERCLAW_BASIC_AUTH=") else l for l in lines]
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  echo "    UI login generated: admin / $ui_pass"
fi

# --- build and start ---
echo "==> Building and starting AmberClaw (first build downloads ~550 MB of Whisper models)"
cd "$REPO_DIR"
docker compose -f "$COMPOSE" up -d --build

echo "==> Waiting for /health"
healthy=0
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${HTTP_PORT}/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done
if [ "$healthy" -ne 1 ]; then
  echo "    health check did not pass yet. See: docker compose -f $COMPOSE logs -f" >&2
  exit 1
fi
echo "    healthy"

cat <<EOF

==> Done.
    UI      : http://<elastic-ip>:${HTTP_PORT}  (Basic auth from AMBERCLAW_BASIC_AUTH in .env)
    Logs    : docker compose -f $COMPOSE logs -f
    Restart : docker compose -f $COMPOSE restart
    Update  : cd $REPO_DIR && git pull && docker compose -f $COMPOSE up -d --build
    Disk    : df -h $DATA_DIR

    Optional HTTPS later:
      1. Add DOMAIN=your.domain to .env and point a DNS A record at the Elastic IP.
      2. Open TCP 80/443 in the EC2 security group.
      3. docker compose -f $COMPOSE --profile tls up -d
EOF
