# Deploying the bot to a 24/7 cloud server

This runs the bot on a small always-on Linux VM so it trades round-the-clock
without your laptop. Budget **~$5–6/month**. Steps you must do yourself are
marked **[you]** (account, payment, and API keys — never share these with
anyone, including in a chat). Everything else is copy-paste.

The bot self-heals: it runs under `systemd`, which restarts it on any crash or
reboot, and on startup it reconciles with Alpaca and resumes managing open
positions with their stops intact. So once this is set up, it genuinely runs
unattended.

---

## Part 1 — Create the server **[you]**

1. Sign up at a VPS provider. **DigitalOcean** is the most beginner-friendly
   (clean UI, good docs); Hetzner is cheaper; AWS Lightsail if you prefer AWS.
2. Create a VM (DigitalOcean calls it a "Droplet"):
   - Image: **Ubuntu 24.04 LTS**
   - Plan: **Basic → Regular → the cheapest ($4–6/mo)**. 1 GB RAM is plenty.
   - Region: closest to you.
   - Authentication: **add an SSH key** if you have one (more secure), else a
     root password.
3. When it finishes, note its **public IP address** (e.g. `203.0.113.42`).

> **Billing note:** you're charged hourly up to the monthly cap while the VM
> exists. *Powering it off does NOT stop billing* — to stop paying you must
> **destroy** the Droplet. Snapshots are cheap if you want to save state.

---

## Part 2 — Log in and create a user

From your laptop's terminal (PowerShell or Git Bash), replace the IP:

```bash
ssh root@203.0.113.42
```

Create a non-root user to run the bot (safer than running as root):

```bash
adduser --gecos "" trader
usermod -aG sudo trader
# optional but recommended: let the new user use your SSH key
rsync --archive --chown=trader:trader ~/.ssh /home/trader
```

A minimal firewall — allow only SSH:

```bash
ufw allow OpenSSH && ufw --force enable
```

Then switch to the new user:

```bash
su - trader
```

---

## Part 3 — Install Python and clone the repo

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
```

Your repo is **private**, so cloning needs auth. Simplest path — a
**fine-grained personal access token (PAT)**:

1. **[you]** On github.com → Settings → Developer settings → Fine-grained
   tokens → Generate. Scope it to **only** `davyn1/trading-bot`, permission
   **Contents: Read-only**. Copy the token (starts `github_pat_…`).
2. Clone using it (paste the token as the password when prompted; username is
   your GitHub username `davyn1`):

```bash
cd ~
git clone https://github.com/davyn1/trading-bot.git
cd trading-bot
```

> The token is a password — don't paste it anywhere public. A read-only,
> single-repo token can only read this one repo, nothing else.

---

## Part 4 — Install dependencies in a virtualenv

On Linux there's no VPN TLS interception and no `--user` quirk, so this is
clean (a venv keeps the bot's packages isolated from the system Python):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## Part 5 — Add your API keys **[you]**

Create the `.env` file **on the server** (it's git-ignored and never leaves
the machine). I never see these values — you type them here directly:

```bash
nano .env
```

Paste, replacing with your real **paper** keys:

```
ALPACA_API_KEY=PK...your key...
ALPACA_SECRET_KEY=...your secret...
# optional, for the Discord briefings:
DISCORD_WEBHOOK_URL=
```

Save (Ctrl+O, Enter, Ctrl+X), then lock the file down:

```bash
chmod 600 .env
```

Quick sanity check that the keys work:

```bash
.venv/bin/python -c "from bot.portfolio import Broker; print(Broker().account().status)"
```

You want to see `AccountStatus.ACTIVE`.

---

## Part 6 — Install the service so it runs 24/7

```bash
sudo cp deploy/trading-bot.service /etc/systemd/system/trading-bot.service
# strip any stray carriage returns just in case:
sudo sed -i 's/\r$//' /etc/systemd/system/trading-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot
```

`enable --now` both starts it immediately and makes it auto-start on reboot.

---

## Part 7 — Verify it's actually trading

```bash
systemctl status trading-bot            # should say "active (running)"
journalctl -u trading-bot -f            # live logs; look for "Reconciliation complete"
tail -f ~/trading-bot/bot.log           # the bot's own log file
```

You should see it reconcile, warm up indicators, and begin evaluating candles
— exactly what you watched locally.

---

## Everyday operations

| Task | Command |
|---|---|
| Live logs | `journalctl -u trading-bot -f` |
| Restart | `sudo systemctl restart trading-bot` |
| Stop (won't auto-restart) | `sudo systemctl stop trading-bot` |
| Update to newest code | `cd ~/trading-bot && git pull && sudo systemctl restart trading-bot` |
| See open positions | `cat ~/trading-bot/state.json` |
| Trade history | `cat ~/trading-bot/trades.csv` |

Stopping via `systemctl stop` is safe: state is saved after every change, so
positions and stops persist and resume on the next start.

---

## Optional — the dashboard (do this only after the bot is solid)

Streamlit has no built-in login, so **don't expose it publicly**. Run it bound
to localhost and view it through an SSH tunnel:

```bash
sudo cp deploy/trading-dashboard.service /etc/systemd/system/
sudo sed -i 's/\r$//' /etc/systemd/system/trading-dashboard.service
sudo systemctl daemon-reload && sudo systemctl enable --now trading-dashboard
```

Then from **your laptop**:

```bash
ssh -N -L 8501:localhost:8501 trader@203.0.113.42
```

Leave that running and open `http://localhost:8501` in your browser — you're
viewing the server's dashboard over the encrypted tunnel, visible only to you.

---

## Optional — the Discord briefings

The morning/evening reports (`morning_briefing.py`, `evening_report.py`) run
best as `cron` jobs on the server. Set `DISCORD_WEBHOOK_URL` in `.env`, then
`crontab -e` and add (times are the server's clock — check with `date`; set
the server to ET with `sudo timedatectl set-timezone America/New_York` first):

```
0 7 * * 1-5  cd /home/trader/trading-bot && .venv/bin/python morning_briefing.py
0 21 * * 1-5 cd /home/trader/trading-bot && .venv/bin/python evening_report.py
```

---

## Security notes

- Your **paper** keys now live on the server. Blast radius is limited — they
  can only touch the paper account, no real money. If you ever switch to live
  trading, revisit this seriously (locked-down firewall, key rotation, non-root
  confirmed, maybe secrets manager).
- Keep `.env` at `chmod 600` and never commit it (`.gitignore` already blocks
  it).
- The firewall (Part 2) allows only SSH. Keep it that way; the bot needs no
  inbound ports.
