#!/usr/bin/env bash
# One-time setup for AmberClaw on an Oracle Cloud Always Free Ubuntu VM.
# Usage (as root): sudo bash /opt/amberclaw/deploy/oracle/setup-vm.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
DATA_DIR=/var/lib/amberclaw
HTTP_PORT=10000
TARGET_USER="${SUDO_USER:-$(whoami)}"

echo "==> AmberClaw VM setup"
echo "    repo : $REPO_DIR"
echo "    data : $DATA_DIR"

echo "==> Installing Docker + utilities"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  docker.io docker-compose-plugin iptables git curl ca-certificates openssl stress-ng >/dev/null
systemctl enable --now docker
id -nG "$TARGET_USER" | grep -qw docker || usermod -aG docker "$TARGET_USER"

echo "==> Preparing persistent data directory"
mkdir -p "$DATA_DIR"

echo "==> Preparing .env"
if [ ! -f "$ENV_FILE" ]; then
  cp "$REPO_DIR/deploy/oracle/env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "    created $ENV_FILE"
else
  echo "    keeping existing $ENV_FILE"
fi

set_env() {
  python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
key, value = sys.argv[2], sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()
out, replaced = [], False
for line in lines:
    if line.startswith(key + "="):
        out.append(f"{key}={value}")
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

key_value="$(grep -E '^GROQ_API_KEY=' "$ENV_FILE" | cut -d= -f2- || true)"
if [ -z "${key_value:-}" ] || [ "$key_value" = "replace-me" ]; then
  echo "Free Groq API key (for open-weight GPT-OSS): get one at https://console.groq.com/keys"
  read -rsp "Groq API key (needed for headless clipping): " typed_key
  echo
  set_env GROQ_API_KEY "$typed_key"
fi

if grep -q 'replace-with-a-strong-password' "$ENV_FILE"; then
  ui_pass="$(openssl rand -base64 18 | tr -d '/+=')"
  set_env AMBERCLAW_BASIC_AUTH "admin:$ui_pass"
  echo "    UI login generated: admin / $ui_pass"
fi

if grep -q '^DOMAIN=claw.example.com' "$ENV_FILE"; then
  read -rp "Domain for optional HTTPS (leave empty to skip Caddy): " typed_domain
  if [ -n "${typed_domain:-}" ]; then
    set_env DOMAIN "$typed_domain"
  fi
fi

echo "==> Opening port $HTTP_PORT on the host firewall"
iptables -C INPUT -p tcp --dport "$HTTP_PORT" -j ACCEPT 2>/dev/null \
  || iptables -I INPUT 1 -p tcp --dport "$HTTP_PORT" -j ACCEPT
mkdir -p /etc/iptables
iptables-save > /etc/iptables/rules.v4
echo "    IMPORTANT: also open TCP $HTTP_PORT in the OCI VCN security list (console)."

echo "==> Building and starting AmberClaw (first build downloads ~550 MB of Whisper models)"
cd "$REPO_DIR"
docker compose -f deploy/oracle/docker-compose.yml up -d --build

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
  echo "    health check did not pass yet. See: docker compose -f deploy/oracle/docker-compose.yml logs -f"
else
  echo "    healthy"
fi

echo "==> Installing hourly keepalive timer (prevents OCI idle reclamation)"
install -m 755 "$REPO_DIR/deploy/oracle/keepalive.sh" /usr/local/bin/amberclaw-keepalive
cp "$REPO_DIR/deploy/oracle/amberclaw-keepalive.service" /etc/systemd/system/
cp "$REPO_DIR/deploy/oracle/amberclaw-keepalive.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now amberclaw-keepalive.timer

cat <<EOF

==> Done.
    UI      : http://<public-ip>:${HTTP_PORT}  (Basic auth is enabled)
    Logs    : docker compose -f deploy/oracle/docker-compose.yml logs -f
    Restart : docker compose -f deploy/oracle/docker-compose.yml restart
    Update  : cd $REPO_DIR && git pull && docker compose -f deploy/oracle/docker-compose.yml up -d --build

    Optional HTTPS (needs DNS + ports 80/443 open in OCI and host firewall):
      docker compose -f deploy/oracle/docker-compose.yml --profile tls up -d

    Keepalive runs hourly for ~4 minutes. If you run real clip jobs regularly,
    you can disable it: sudo systemctl disable --now amberclaw-keepalive.timer
EOF
