# Deploy AmberClaw on AWS EC2 — complete step-by-step (ap-south-1 Mumbai)

This guide assumes you are starting from a brand-new AWS root account and are clicking through
the AWS Console in a browser. It ends with the AmberClaw dashboard running and the
**GTA 5 — 100 viral shorts** job submitted.

**What we build:** Ubuntu 24.04 LTS, `m6i.large` (2 vCPU / 8 GB), 100 GiB gp3 disk, Elastic IP,
Docker Compose, Basic auth, and free HTTPS at `https://project7.me` via Caddy / Let's Encrypt.

**Cost:** about **USD 78/month** (m6i.large ~USD 70 + 100 GiB gp3 ~USD 8, Mumbai). Groq is free.
YouTube uploads use your account's free quota (~6/day).

> The app does **not** call AWS APIs and needs **no AWS access keys on the server**. The only
> secrets it uses are `.env` (Groq key, Basic auth, settings) and the two YouTube OAuth JSON files.
> Never commit them or paste them into a chat/terminal log.

---

## STEP 0 — Secure the root account and create an IAM admin user

### 0.1 Enable MFA on the root account
1. Sign in at <https://console.aws.amazon.com/> as the **root user** (email + password).
2. Top-right, click your **account name → My Security Credentials**
   (or open <https://console.aws.amazon.com/iamv2/home#/home> and look for the red
   *"Add MFA"* / *"Your root account is not protected"* banner).
3. Choose **Activate MFA on your root account → Activate MFA**.
4. Choose **Virtual MFA device → Continue**. Open an authenticator app on your phone
   (Google Authenticator, Authy, 1Password, etc.) and scan the QR code.
5. Enter two consecutive 6-digit codes from the app and click **Assign MFA / Finish**.
6. From now on, root sign-in needs password + code. Save the recovery codes offline.

### 0.2 Create an IAM group with admin permissions
1. In the AWS Console, open the **IAM** service (search "IAM" in the top search box).
2. Left sidebar → **User groups** → **Create group**.
3. Group name: `Admins`.
4. In the permissions list, tick **AdministratorAccess**, then **Create group**.
   (For a single personal account this is fine; later you can narrow it down.)

### 0.3 Create an IAM admin user
1. IAM → **Users** → **Add users** (or **Create user**).
2. User name: `amberclaw-admin`.
3. Select **Console access (AWS Management Console access)** → **Custom password** →
   set a strong password. Leave *"Require password reset"* checked (you'll set a new one at first login).
4. Also tick **Programmatic access (Access key)** — optional but useful if you later want to use
   the AWS CLI. You will not need it for this deployment.
5. Click **Next: Permissions** → select the `Admins` group → **Next** → **Create user**.
6. **Download the .csv** with the access key (shown only once) and store it safely. Never commit it.
7. On the final screen, note the **console sign-in URL** — it looks like
   `https://<12-digit-account-id>.signin.aws.amazon.com/console`.
8. Sign out of root. Sign in again using that URL as `amberclaw-admin` (you'll be asked to set a
   new password, then MFA is not required for IAM users unless you enable it separately —
   consider enabling it under IAM → Users → amberclaw-admin → Security credentials).

**All remaining steps are done as `amberclaw-admin`.**

---

## STEP 1 — Select the Mumbai region

Top-right of the console, the region dropdown currently says something like *N. Virginia*.
Click it and choose **Asia Pacific (Mumbai) ap-south-1**. Everything you create must be in this
region (IAM is global, but EC2/EIP/security groups are regional).

---

## STEP 2 — Create the SSH key pair

1. Open **EC2** → left sidebar, under *Network & Security*, click **Key pairs**.
2. Click **Create key pair**.
3. Name: `amberclaw-key`.
4. Key pair type: **RSA**. Private key file format: **.pem** (works with Windows OpenSSH and macOS/Linux).
5. Click **Create key pair**. The `.pem` file downloads automatically — move it next to the repo,
   e.g. `I:\4th sem\gta\amby\amberclaw-key.pem`, and keep it private.

---

## STEP 3 — Create the security group (firewall)

1. EC2 → left sidebar → **Security groups** → **Create security group**.
2. Security group name: `amberclaw-sg`; description: `AmberClaw UI and SSH`.
3. VPC: keep the default VPC.
4. **Inbound rules → Add rule**, twice:
   | Rule | Type | Port | Source |
   |---|---|---|---|
   | SSH | SSH | 22 | **My IP** |
   | HTTP | HTTP | 80 | `0.0.0.0/0` and `::/0` |
   | HTTPS | HTTPS | 443 | `0.0.0.0/0` and `::/0` |
   | App UI (fallback) | Custom TCP | 10000 | **My IP** |
5. *My IP* is your current public IP as AWS detects it. Ports 80/443 must be public so Caddy can
   issue and renew the Let's Encrypt certificate for `project7.me`. Port 10000 stays restricted
   to your IP as a direct-HTTP fallback; normal access is `https://project7.me`.
6. **Outbound rules**: leave the default *Allow all*.
7. Click **Create security group**.

---

## STEP 4 — Launch the EC2 instance

1. EC2 → **Instances** → **Launch instances**.
2. **Name**: `amberclaw`.
3. **Application and OS Images**: **Ubuntu → Ubuntu Server 24.04 LTS (HVM), SSD Volume Type**,
   architecture **64-bit (x86)**. (Do *not* pick the ARM/A1 image — we want x86 for m6i.)
4. **Instance type**: **m6i.large** (2 vCPU, 8 GB RAM).
5. **Key pair (login)**: select `amberclaw-key`.
6. **Network settings → Edit**:
   - VPC: default; Subnet: any (e.g. `ap-south-1a`).
   - **Auto-assign public IP: Enable**.
   - Firewall (security groups): **Select existing security group** → `amberclaw-sg`.
7. **Configure storage**: change the root volume from 8 GiB to **100 GiB**, volume type **gp3**.
8. Leave *Advanced details* at defaults. On the right, the summary should show ~USD 0.10/hour.
9. Click **Launch instances**. If AWS asks about "Acceptable use" / SSH key confirmation, confirm.
10. In **Instances**, wait until *Instance state* = **Running** and *Status check* = **2/2 checks passed**.

---

## STEP 5 — Allocate an Elastic IP (stable address)

1. EC2 → **Elastic IPs** (under *Network & Security*) → **Allocate Elastic IP address**.
2. Network border group: `ap-south-1` (default) → **Allocate**.
3. Select the new address → **Actions → Associate IP address**.
4. Resource type: **Instance**; Instance: `amberclaw`; Private IP: the one shown → **Associate**.
5. Copy the **Elastic IP** — this is the address you'll use everywhere from now on.
   (The original auto-assigned public IP is released.)

---

## STEP 6 — Your local PC: copy secrets and connect

Open PowerShell **in the repo root** (`I:\4th sem\gta\amby`):

```powershell
cd "I:\4th sem\gta\amby"
$KEY = ".\amberclaw-key.pem"
$IP  = "<your-elastic-ip>"

# If Windows complains the key permissions are too open, run these two lines once:
icacls $KEY /inheritance:r
icacls $KEY /grant:r "$($env:USERNAME):F"

scp -i $KEY .env client_secrets.json token.json ubuntu@$IP:/tmp/

# YouTube cookies (REQUIRED on AWS/EC2): YouTube flags datacenter IPs as bots.
# Install the "Get cookies.txt LOCALLY" Brave/Chrome extension, open youtube.com
# while signed in, click the extension -> Export, then save the file.
scp -i $KEY "C:\Users\<you>\Downloads\www.youtube.com_cookies.txt" ubuntu@$IP:/tmp/youtube_cookies.txt

ssh -i $KEY ubuntu@$IP
```

- On the first connection, type `yes` when asked about host authenticity.
- `.env` already contains your Groq key, `YOUTUBE_PRIVACY=public`, GTA6 tags, `--min 50 --max 60`
  and queue settings. The AWS compose file overrides the Windows-specific paths with Linux ones.

---

## STEP 7 — On the instance: install and start AmberClaw

Paste these commands one block at a time into the SSH session:

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/riteshdr/AmberClaw.git /opt/amberclaw
```

```bash
sudo install -m 600 /tmp/.env                /opt/amberclaw/.env
sudo install -m 600 /tmp/client_secrets.json /opt/amberclaw/client_secrets.json
sudo install -m 600 /tmp/token.json          /opt/amberclaw/token.json
sudo install -m 600 -o root -g root /tmp/youtube_cookies.txt /var/lib/amberclaw/youtube_cookies.txt
```

```bash
sudo bash /opt/amberclaw/deploy/aws/setup.sh
```

The script installs Docker, creates `/var/lib/amberclaw` (persistent jobs, clips, cache), builds
the image (first build downloads ~550 MB of Whisper models — allow 5–15 minutes), starts the
container and waits for `/health`. When it finishes you should see `healthy` and a *Done* banner.

If it exits with "health check did not pass", run:

```bash
cd /opt/amberclaw
docker compose -f deploy/aws/docker-compose.yml logs -f
```

---

## STEP 8 — Verify in the browser

1. Open **http://<your-elastic-ip>:10000** in your browser.
2. Log in with `AMBERCLAW_BASIC_AUTH` from your `.env` (format `user:password`). To view it
   without showing other secrets, run locally:
   ```powershell
   Select-String -Path .env -Pattern '^AMBERCLAW_BASIC_AUTH='
   ```
3. Check the dashboard:
   - Stats cards show numbers (jobs total, clips generated, etc.).
   - Job table lists existing jobs with **All / Queued / Processing / Done / Failed** filters.
   - **Daily YouTube upload queue** says *Scheduler running*, with quota day and slots left.
   - **Settings & secrets** shows your Groq key masked, e.g. `gsk_S...w1xa`.
4. Optional, from SSH: `curl http://127.0.0.1:10000/health` should return `{"ok":true}`.

---

## STEP 9 — Submit the first real job

1. In **New job**, open the **Preset template** dropdown → choose **GTA 6 — 100 viral shorts**.
2. Confirm the filled values: topic `GTA 6`, focus about funny/hype Roman Hinglish moments,
   streams `20`, clips per stream `5`, window `week`, brain `groq`,
   **Find streams** checked, **Add clips to the daily YouTube upload queue** checked.
   Only Hindi/Hinglish/English creators are kept; other languages are skipped automatically.
3. Click **Run AmberClaw job**. The job appears in the table as *processing*.
4. Expect **~12–17 hours** of generation for 20 long streams. Generated clips are queued;
   the scheduler uploads **6/day** within YouTube's free quota. Monitor everything from the
   dashboard — no SSH needed.

---

## STEP 10 — Day-to-day operations (SSH)

```bash
cd /opt/amberclaw
docker compose -f deploy/aws/docker-compose.yml ps                  # container status
docker compose -f deploy/aws/docker-compose.yml logs -f             # live logs
docker compose -f deploy/aws/docker-compose.yml restart             # restart app
git pull && docker compose -f deploy/aws/docker-compose.yml up -d --build   # update from GitHub
df -h /var/lib/amberclaw                                            # disk usage
```

Back up the secrets after any change:

```bash
sudo tar czf /root/amberclaw-secrets.tgz -C /opt/amberclaw .env client_secrets.json token.json
```

**Billing safety (recommended):** AWS Console → **Billing and Cost Management → Budgets →
Create budget** → Cost budget, monthly, **USD 100**, add your email for an alert at 80% and 100%.

---

## STEP 11 — Domain `project7.me` + free HTTPS (Caddy / Let's Encrypt)

The Caddyfile in this repo is already configured for `project7.me` and `www.project7.me`.
Caddy runs by default in the compose file, redirects HTTP → HTTPS, and auto-renews the certificate.

### 11.1 Point DNS at the server (do this where you bought the domain)

Log in to your domain registrar's DNS management page (e.g. GoDaddy, Namecheap, Cloudflare,
BigRock, etc.) and set these **A records**. If a record already exists, **edit** its value
rather than adding a duplicate.

| Type | Name / Host | Value / Points to | TTL |
|---|---|---|---|
| A | `project7.me` (often `@` or "root") | `13.127.103.201` | 300 (or Automatic) |
| A | `www` (creates `www.project7.me`) | `13.127.103.201` | 300 (or Automatic) |

- If your provider also shows an AAAA (IPv6) record for these names, **delete it** — the server
  has no public IPv6, and a stale AAAA can break certificate issuance.
- If the registrar offers "Cloudflare proxy" (orange cloud), leave it **DNS only / grey cloud**
  until HTTPS is verified; proxying can interfere with Let's Encrypt's HTTP challenge.
- DNS changes can take a few minutes to a few hours. Verify from your PC with:
  ```powershell
  nslookup project7.me
  nslookup www.project7.me
  ```
  Both must show `13.127.103.201` before continuing.

### 11.2 Open ports 80 and 443 in AWS

AWS Console → **EC2 → Security Groups → `amberclaw-sg` → Inbound rules → Edit inbound rules**:
- **Add rule** → Type **HTTP** → Port `80` → Source **Anywhere-IPv4** (`0.0.0.0/0`).
- **Add rule** → Type **HTTPS** → Port `443` → Source **Anywhere-IPv4** (`0.0.0.0/0`).
- (IPv6 optional: add **Anywhere-IPv6** `::/0` for both if you added AAAA records later.)
- Save rules. Keep port `10000` restricted to **My IP** as a fallback.

### 11.3 Deploy (starts the Caddy container)

```bash
cd /opt/amberclaw
sudo git pull --ff-only
sudo docker compose -f deploy/aws/docker-compose.yml up -d --build
sudo docker compose -f deploy/aws/docker-compose.yml ps
```

You should see both `amberclaw` and `caddy` running. Caddy obtains the certificate on first
request, so visit `https://project7.me/health` once and wait a few seconds.

### 11.4 Verify

- `https://project7.me/health` → `{"ok":true}` (no browser certificate warning).
- `https://www.project7.me/` → dashboard; log in with the `AMBERCLAW_BASIC_AUTH` user/password.
- `http://project7.me/` → automatically redirects to `https://project7.me/`.

If the certificate does not appear: confirm DNS resolves to `13.127.103.201`, ports 80/443 are
open, then check `sudo docker compose -f deploy/aws/docker-compose.yml logs caddy`.

Once HTTPS is confirmed you can delete or restrict the port `10000` inbound rule; all normal
access is through `https://project7.me`.

---

## STEP 12 — Troubleshooting

- **SSH connection refused/timeout** → check SG inbound `22` from *My IP*, correct `.pem`, correct
  Elastic IP, instance running.
- **"Private key permissions too open"** (Windows) → the two `icacls` commands in Step 6.
- **Port 10000 times out** → SG inbound `10000`, Elastic IP associated,
  `docker compose ... ps` and `logs -f` on the instance.
- **401 at the UI** → credentials are `AMBERCLAW_BASIC_AUTH` in `/opt/amberclaw/.env`.
- **Jobs fail with "stream-clips is not set up yet"** → the private venv is missing; rebuild:
  `docker compose -f deploy/aws/docker-compose.yml up -d --build`.
- **Build fails in `setup_env.py`** → check disk (`df -h /`) and network to PyPI/HuggingFace.
- **Jobs fail with "YouTube needs a login" / "not a bot"** → the cookie file is missing or expired.
  Re-export youtube.com cookies with the browser extension and reinstall:
  `sudo install -m 600 -o root -g root /tmp/youtube_cookies.txt /var/lib/amberclaw/youtube_cookies.txt`
  then restart: `docker compose -f deploy/aws/docker-compose.yml restart`.
- **Queue not uploading** → check *quota day* / *uploaded today* on the dashboard; free YouTube
  quota allows ~6 uploads/day.
- **Windows paths in jobs** → the AWS compose file overrides `STREAMCLIPS_PYTHON`,
  `STREAMCLIPS_PY`, `AMBERCLAW_DATA_DIR` and the YouTube token paths. Do not remove those lines.
