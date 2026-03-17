# Reddit Slack Bot

Monitors Reddit (`r/all`) for keywords and routes matching posts and comments to Slack channels via webhooks. Runs two parallel worker processes — one for submissions, one for comments.

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Set environment variables

Copy the example and fill in your credentials:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `REDDIT_CLIENT_ID` | From https://www.reddit.com/prefs/apps |
| `REDDIT_CLIENT_SECRET` | From the same Reddit app page |
| `REDDIT_USER_AGENT` | Any string, e.g. `reddit-slack-bot/1.0 by u/yourname` |
| `SLACK_WEBHOOK_A` | Incoming Webhook URL for the first Slack channel |
| `SLACK_WEBHOOK_B` | Incoming Webhook URL for the second Slack channel |

To create a Reddit app: go to https://www.reddit.com/prefs/apps → "create another app" → choose **script**.

To create a Slack webhook: go to your Slack workspace → Apps → Incoming Webhooks → Add New Webhook.

### 3. Configure keywords

Edit `config.yaml` to set keywords, Slack channel routing, and the subreddit blacklist. No code changes are needed to add channels — just add a block under `channels:` and a new env var.

### 4. Run

```bash
python main.py
```

On startup you should see two worker PIDs logged:

```
Started submission worker (pid=XXXX) and comment worker (pid=YYYY)
```

Stop with `Ctrl+C`.

## Deploying to Koyeb

1. Push this repo to GitHub (see below).
2. In the Koyeb dashboard: **Create Service → GitHub → select repo**.
3. Set the **Run command** to `python main.py` (or leave it — Koyeb reads the `Procfile`).
4. Set **Service Type: Web Service** (free Nano tier only supports Web).
5. **Disable health checks** — this is a background worker with no HTTP port.
6. Add all five env vars (`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, `SLACK_WEBHOOK_A`, `SLACK_WEBHOOK_B`) under **Environment Variables**.
7. Deploy. Monitor logs in the Koyeb dashboard.

> **Note:** Koyeb's free tier uses ephemeral storage. The SQLite deduplication database (`data/bot.db`) is wiped on every redeploy. Duplicate protection only covers the current deployment's lifetime.

## Pushing to GitHub

```bash
# If you haven't already initialised git:
git init
git branch -M main

# Add your GitHub repo as origin (replace with your URL):
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git

# Stage, commit, and push:
git add .
git commit -m "Initial commit"
git push -u origin main
```

If the repo already exists on GitHub and you just want to push latest changes:

```bash
git add .
git commit -m "describe your changes"
git push
```

## Project structure

```
main.py          # Entry point — spawns two worker processes
monitor.py       # Worker logic, config dataclasses, send_notification()
notifier.py      # SlackNotifier — Block Kit webhook POST with retry
database.py      # SQLite deduplication store
config.yaml      # Keywords, channels, blacklist, tuning knobs
.env             # Secrets (never commit this)
.env.example     # Template for secrets
Procfile         # Koyeb / Heroku process declaration
requirements.txt # Pinned dependencies
```
