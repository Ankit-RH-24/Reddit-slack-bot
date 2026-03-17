# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env

# Run
python main.py
```

The bot reads `config.yaml` from the working directory and `.env` for secrets. `data/` and `logs/` directories are created automatically at startup.

## Architecture

Single-process, blocking event loop — no threads, no async:

```
main.py
  └── RedditMonitor.run()           # outer restart loop (monitor.py)
        └── _stream_submissions()   # PRAW generator, blocks indefinitely
              └── _process_submission()
                    ├── KeywordMatcher.match()     # first-match wins across all channels
                    ├── Database.is_duplicate()    # SQLite dedupe check
                    ├── SlackNotifier.send()       # Block Kit webhook POST
                    └── Database.mark_processed()
```

**Config → code flow:** `config.yaml` is parsed by `load_config()` in `monitor.py` into `AppConfig` and `ChannelConfig` dataclasses. `SlackConfig` is defined in `notifier.py` and passed through. Adding a new Slack channel requires only a new block in `config.yaml` and a new env var — no code changes.

**Secrets indirection:** `config.yaml` stores the *name* of the env var (`webhook_env_var: SLACK_WEBHOOK_A`), not the URL. `SlackNotifier.__init__` resolves `os.environ[webhook_env_var]` at startup and raises `EnvironmentError` immediately if any are missing.

**Keyword matching (`monitor.py:KeywordMatcher`):** Keywords are pre-lowercased at init. `match()` concatenates `title + selftext` and walks rules in declaration order — first match wins. A post is routed to exactly one channel.

**Stream restart loop (`monitor.py:RedditMonitor.run`):** Wraps `_stream_submissions()` in a retry loop. `prawcore.ResponseException(401)` is fatal; all other `prawcore` exceptions pause for `stream_pause_after_exception` seconds and retry up to `max_stream_retries` (-1 = infinite). Set `max_stream_retries: -1` in `config.yaml` for production.

**Deduplication:** Two safety nets — `is_duplicate()` check before sending, then `INSERT OR IGNORE` in `mark_processed()`. Both are needed to handle stream restarts.

**DB cleanup:** Called inline every 1000 processed posts (not on a timer). Deletes records older than `cleanup_older_than_days`.

## Deployment (Koyeb)

Deployed as a **Web Service** on Koyeb's free Nano tier via `Procfile`. Health checks must be **disabled** in the Koyeb UI — this is a background worker, not an HTTP server.

**Ephemeral storage:** Koyeb's free tier does not persist the filesystem across redeploys. `data/bot.db` is wiped on every restart. Duplicate protection only covers the current deployment lifetime. For cross-restart deduplication, replace `Database` with a managed Postgres instance.

**Logs:** Both `stdout` (visible in Koyeb dashboard) and `logs/bot.log` (rotating, ephemeral) receive all log output. The Koyeb dashboard is the primary log source in production.

## Extending channels

1. Add a block under `channels:` in `config.yaml`:
   ```yaml
   - name: my_channel
     webhook_env_var: SLACK_WEBHOOK_C
     keywords: ["keyword one", "keyword two"]
     case_sensitive: false
   ```
2. Add `SLACK_WEBHOOK_C=https://...` to `.env` (local) and the Koyeb environment variables (prod).

No code changes required.
