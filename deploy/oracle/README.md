# Deploy AmberClaw on Oracle Cloud Always Free

This gives you an always-on instance big enough for the whole pipeline —
**2 OCPU / 12 GB RAM, 200 GB storage, 10 TB/month egress, free for the life of the account**.

## 1. Create the VM in the OCI console

1. Sign up at `cloud.oracle.com` (card required for verification, but Always Free resources are not charged).
2. Pick your **home region** carefully — Always Free resources live there and the home region can't easily be changed.
3. **Networking → Virtual Cloud Networks → VCN Wizard** → create a VCN with a public subnet.
4. **Compute → Instances → Create Instance**:
   - Image: **Ubuntu 22.04 / 24.04** (must be labelled *Always Free Eligible*)
   - Shape: **VM.Standard.A1.Flex** (Ampere ARM)
   - Set **2 OCPUs / 12 GB RAM** (uses the full free allowance)
   - Boot volume: 50 GB (default). Add a **150 GB block volume** if you want clips to have room — total free block storage is 200 GB.
   - Attach your SSH public key.
5. If you get `Out of host capacity`, retry another Availability Domain or later in the day. It's common on free ARM shapes.
6. In the instance's **Subnet → Security List → Ingress Rules**, add:
   - `TCP 10000` from `0.0.0.0/0` (the app)
   - `TCP 80` and `TCP 443` from `0.0.0.0/0` (only if you want HTTPS via Caddy)
7. Note the **public IP**.

## 2. Install on the VM

```bash
ssh -i ~/.ssh/your_key.pem ubuntu@<public-ip>

sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/riteshdr/AmberClaw.git /opt/amberclaw
sudo bash /opt/amberclaw/deploy/oracle/setup-vm.sh
```

The script installs Docker, creates `/var/lib/amberclaw` (persistent data), prompts for a free
`GROQ_API_KEY` (open-weight GPT-OSS), generates a strong Basic-auth password, opens port 10000
in the host firewalls, builds the image (downloads ~550 MB of Whisper models: `tiny` + `small`) and starts the app.

Open `http://<public-ip>:10000` and log in with the printed credentials.

## 3. Configuration

Everything is in `/opt/amberclaw/.env` (`chmod 600`):

| Variable | Notes |
|---|---|
| `GROQ_API_KEY` | Free key from https://console.groq.com/keys; required for the default `--brain groq` picker. |
| `GROQ_BASE_URL` / `GROQ_MODEL` | Default `https://api.groq.com/openai/v1` and `openai/gpt-oss-20b`. Try `openai/gpt-oss-120b` for harder jobs. |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | Optional paid fallback with `STREAMCLIPS_BRAIN=openai`. |
| `AMBERCLAW_BASIC_AUTH` | `user:pass,user2:pass2`. Keep this set — the UI is public. |
| `AMBERCLAW_DATA_DIR` / `STREAMCLIPS_HOME` | Must stay `/data` and `/data/StreamClips` (mounted to `/var/lib/amberclaw`). |
| `AUTO_UPLOAD` | Leave `false` until upload credentials are mounted. |

Captions default to Roman Hinglish (Whisper `small` + Hinglish prompt + Devanagari→Roman fallback)
and output is 1080x1920 at up to 60 fps, encoded at `crf 18` / `medium` from up to a 4K source.
No extra config is needed. To regenerate clips with new settings, re-run the same job; if the old
captions are cached, delete `STREAMCLIPS_HOME/.cache/<stream>/clips/*.ass` first (and set
`AUTO_UPLOAD=false` while testing to avoid duplicates).

### Enabling YouTube uploads
The OAuth files are gitignored, so copy them to the VM and uncomment the two mounts in
`deploy/oracle/docker-compose.yml`:

```bash
scp client_secrets.json token.json ubuntu@<public-ip>:/tmp/
sudo cp /tmp/client_secrets.json /tmp/token.json /opt/amberclaw/
cd /opt/amberclaw
sudo docker compose -f deploy/oracle/docker-compose.yml up -d
```

Then set `AUTO_UPLOAD=true` in `.env` if you want automatic uploads, and restart.

### HTTPS (recommended)
Add `DOMAIN=claw.yourdomain.com` to `.env`, point DNS at the public IP, open ports 80/443
in both the OCI security list and the host firewall, then:

```bash
sudo iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
sudo iptables-save > /etc/iptables/rules.v4
cd /opt/amberclaw
docker compose -f deploy/oracle/docker-compose.yml --profile tls up -d
```

Caddy obtains a Let's Encrypt certificate automatically.

## 4. Keeping it free

Oracle can reclaim an Always Free VM that is idle for 7 days (CPU p95 < 20%, network < 20%,
memory < 20% on A1). The setup script installs an hourly keepalive timer that loads one core
for ~4 minutes and hits `/health`. Real clip jobs also count as activity.

```bash
systemctl status amberclaw-keepalive.timer   # check
sudo systemctl disable --now amberclaw-keepalive.timer  # disable if you run jobs often
KEEPALIVE_LOAD_SECONDS=120 ...                # tune in /usr/local/bin/amberclaw-keepalive
```

## 5. Day-to-day operations

```bash
cd /opt/amberclaw
docker compose -f deploy/oracle/docker-compose.yml logs -f
docker compose -f deploy/oracle/docker-compose.yml restart
git pull && docker compose -f deploy/oracle/docker-compose.yml up -d --build
df -h /var/lib/amberclaw
```

## 6. Troubleshooting

- **`out of host capacity`** — retry another AD / time, or use the 1 GB AMD micro instance for the UI only.
- **Build fails in `setup_env.py`** — check network to PyPI/HuggingFace and free disk; the build now fails loudly on purpose.
- **Jobs fail with "stream-clips is not set up yet"** — the private venv is missing; rebuild the image.
- **`ffmpeg not found` / yt-dlp errors** — they're inside the container; check `docker compose ... logs`.
- **ARM wheel issues** — verify `ctranslate2`, `av` and `opencv-python-headless` install on aarch64; the Dockerfile uses `python:3.12-slim` which has arm64 images.
- **`502`/timeout through Caddy** — make sure `DOMAIN` is set and the app is healthy on 10000.
