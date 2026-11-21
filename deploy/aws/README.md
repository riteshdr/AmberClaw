# Deploy AmberClaw on AWS EC2 (ap-south-1 Mumbai)

Recommended production setup: **Ubuntu 24.04 LTS, m6i.large (2 vCPU / 8 GB), 100 GB gp3 EBS,
Docker Compose, HTTP on an Elastic IP with Basic auth**. HTTPS via Caddy can be added later.

Approximate cost: **~USD 78/month** (m6i.large ~USD 70 + 100 GB gp3 ~USD 8, Mumbai).
Groq API is free. YouTube uploads use your account's free quota (~6/day).

> Security: enable MFA on the AWS root account. For day-to-day work prefer an IAM admin user.
> Never commit `.env`, `client_secrets.json` or `token.json` — they are gitignored and excluded
> from the Docker image by `.dockerignore`.

---

## 1. AWS console — create the instance

1. Sign in to the AWS console and switch the region to **ap-south-1 (Mumbai)**.
2. **EC2 → Key pairs → Create key pair**
   - Name: `amberclaw-key`, type **RSA**, format **.pem** (OpenSSH also works on Windows).
   - Download the file and keep it safe locally. On Linux/macOS: `chmod 400 amberclaw-key.pem`.
3. **EC2 → Security groups → Create security group**
   - Name: `amberclaw-sg`, VPC: default.
   - Inbound rules:
     - `SSH (22)` → Source **My IP** (your current public IP).
     - `Custom TCP (10000)` → Source **My IP**.
       (Only use `0.0.0.0/0` if you accept Basic-auth credentials travelling over plain HTTP.
       Add your phone's IP later if you need mobile access.)
   - Outbound: allow all (default).
4. **EC2 → Instances → Launch instance**
   - Name: `amberclaw`
   - AMI: **Ubuntu Server 24.04 LTS (amd64)** — the free-tier-eligible label is fine.
   - Instance type: **m6i.large**
   - Key pair: `amberclaw-key`
   - Network: default VPC, subnet: any, **Auto-assign public IP: Enable**
   - Storage: **100 GiB gp3** (root volume; holds code, Docker image, models and clips)
   - Security group: select `amberclaw-sg`
   - Launch.
5. **EC2 → Elastic IPs → Allocate Elastic IP address** → select it →
   **Actions → Associate IP address** → choose the `amberclaw` instance.
   Note this IP — it stays the same across instance stop/start.

Opening port 22 and 10000 in the **security group** is all that is needed on Ubuntu EC2;
there is no host firewall to edit (ufw is inactive by default).

---

## 2. Local machine — copy secrets and connect

From PowerShell in the repo root (`I:\4th sem\gta\amby`):

```powershell
$KEY = ".\amberclaw-key.pem"     # path to the downloaded .pem
$IP  = "<elastic-ip>"

scp -i $KEY .env client_secrets.json token.json ubuntu@$IP:/tmp/
ssh -i $KEY ubuntu@$IP
```

`.env` from your local machine already has the Groq key, public privacy, Hinglish metadata,
GTA6 tags, `--min 50 --max 60` and queue settings. The AWS compose file overrides the
Windows-specific paths with Linux equivalents automatically.

---

## 3. On the instance — one-time setup

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/riteshdr/AmberClaw.git /opt/amberclaw

sudo install -m 600 /tmp/.env                  /opt/amberclaw/.env
sudo install -m 600 /tmp/client_secrets.json   /opt/amberclaw/client_secrets.json
sudo install -m 600 /tmp/token.json            /opt/amberclaw/token.json

sudo bash /opt/amberclaw/deploy/aws/setup.sh
```

The script installs Docker, creates `/var/lib/amberclaw` (persistent jobs/clips/cache),
builds the image (downloads ~550 MB of Whisper models on first build), starts the service and
waits for `/health`.

Then open **http://<elastic-ip>:10000** and sign in with `AMBERCLAW_BASIC_AUTH` from your `.env`.

---

## 4. First run

1. Open the dashboard and confirm the stats cards, job table and **Daily YouTube upload queue**
   all load; check that **Scheduler running** is shown.
2. In **Settings & secrets**, confirm your Groq key appears masked (e.g. `gsk_S...w1xa`).
3. Submit the **GTA 5 — 100 viral shorts** preset. With `UPLOAD_MODE=queue`, generation starts
   immediately and clips enter the upload queue; the scheduler uploads 6/day within free quota.
4. Monitor from the dashboard — no SSH needed for day-to-day work.

---

## 5. Day-to-day operations (SSH)

```bash
cd /opt/amberclaw
docker compose -f deploy/aws/docker-compose.yml logs -f          # live logs
docker compose -f deploy/aws/docker-compose.yml ps               # container status
docker compose -f deploy/aws/docker-compose.yml restart          # restart app
git pull && docker compose -f deploy/aws/docker-compose.yml up -d --build   # update from GitHub
df -h /var/lib/amberclaw                                          # disk usage
```

Back up these three files after any change (they contain all secrets/config):

```bash
sudo tar czf /root/amberclaw-secrets.tgz -C /opt/amberclaw .env client_secrets.json token.json
```

---

## 6. Add HTTPS later (optional, free)

1. Add `DOMAIN=claw.yourdomain.com` to `/opt/amberclaw/.env`.
2. Create a DNS **A** record for that hostname pointing to the Elastic IP.
3. In the EC2 security group, allow inbound **80** and **443** from `0.0.0.0/0`.
4. Run:
   ```bash
   cd /opt/amberclaw
   docker compose -f deploy/aws/docker-compose.yml --profile tls up -d
   ```
   Caddy obtains a Let's Encrypt certificate automatically. You can then restrict port 10000
   to `My IP` only.

---

## 7. Troubleshooting

- **SSH refused** — check the security group inbound rule for `22` and that you used the right key.
- **Port 10000 times out** — check the security group inbound rule for `10000` and that the instance
  has a public/Elastic IP; `docker compose ... ps` and `logs -f` on the instance.
- **401 at the UI** — credentials come from `AMBERCLAW_BASIC_AUTH` in `.env`.
- **Jobs fail with "stream-clips is not set up yet"** — the baked private venv is missing; rebuild the image.
- **Build fails in `setup_env.py`** — check free disk and network access to PyPI/HuggingFace.
- **Queue not uploading** — check quota day / uploaded-today on the dashboard; free YouTube quota allows ~6/day.
- **Windows paths in jobs** — the AWS compose file overrides `STREAMCLIPS_PYTHON`,
  `STREAMCLIPS_PY`, `AMBERCLAW_DATA_DIR` and YouTube credential paths; don't remove those overrides.
