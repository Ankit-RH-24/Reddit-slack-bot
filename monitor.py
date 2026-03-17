import logging
import logging.handlers
import multiprocessing
import os
import sys
import time
from dataclasses import dataclass

import praw
import prawcore
import yaml

from database import Database
from notifier import SlackNotifier, SlackConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ChannelConfig:
    name: str
    webhook_env_var: str
    keywords: list
    case_sensitive: bool = False


@dataclass
class AppConfig:
    scope: str
    blacklist_subreddits: list
    stream_pause_after_exception: int
    max_stream_retries: int
    slack: SlackConfig
    channels: list  # list[ChannelConfig]
    db_path: str
    cleanup_older_than_days: int
    log_level: str
    log_file: str
    log_max_bytes: int
    log_backup_count: int


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> AppConfig:
    with open(path, "r") as fh:
        raw = yaml.safe_load(fh)

    channels = [
        ChannelConfig(
            name=ch["name"],
            webhook_env_var=ch["webhook_env_var"],
            keywords=ch["keywords"],
            case_sensitive=ch.get("case_sensitive", False),
        )
        for ch in raw["channels"]
    ]

    slack_raw = raw.get("slack", {})
    slack_cfg = SlackConfig(
        rate_limit_base_delay=slack_raw.get("rate_limit_base_delay", 1.0),
        rate_limit_max_delay=slack_raw.get("rate_limit_max_delay", 64.0),
        rate_limit_max_retries=slack_raw.get("rate_limit_max_retries", 6),
    )

    reddit_raw = raw.get("reddit", {})
    db_raw = raw.get("database", {})
    log_raw = raw.get("logging", {})

    return AppConfig(
        scope=reddit_raw.get("scope", "all"),
        blacklist_subreddits=reddit_raw.get("blacklist_subreddits", []),
        stream_pause_after_exception=reddit_raw.get("stream_pause_after_exception", 5),
        max_stream_retries=reddit_raw.get("max_stream_retries", -1),
        slack=slack_cfg,
        channels=channels,
        db_path=db_raw.get("path", "data/bot.db"),
        cleanup_older_than_days=db_raw.get("cleanup_older_than_days", 90),
        log_level=log_raw.get("level", "INFO"),
        log_file=log_raw.get("file", "logs/bot.log"),
        log_max_bytes=log_raw.get("max_bytes", 10485760),
        log_backup_count=log_raw.get("backup_count", 5),
    )


# ---------------------------------------------------------------------------
# Keyword matcher
# ---------------------------------------------------------------------------

class KeywordMatcher:
    def __init__(self, channels: list):
        self._rules = []
        for ch in channels:
            for kw in ch.keywords:
                self._rules.append((ch.name, kw, kw.lower(), ch.case_sensitive))

    def match(self, title: str, selftext: str) -> tuple | None:
        """
        Returns (channel_name, keyword) for the first matching rule, or None.
        Searches title + selftext combined.
        """
        combined = f"{title} {selftext}"
        combined_lower = combined.lower()

        for channel_name, original_kw, kw_lower, case_sensitive in self._rules:
            if case_sensitive:
                if original_kw in combined:
                    return (channel_name, original_kw)
            else:
                if kw_lower in combined_lower:
                    return (channel_name, original_kw)

        return None


# ---------------------------------------------------------------------------
# Notification dispatcher — shared by both worker processes
# ---------------------------------------------------------------------------

def send_notification(item, item_type: str, matcher, notifier, db, blacklist_set) -> bool:
    """
    Route a PRAW submission or comment through the full pipeline.

    item_type: "submission" or "comment"
    Returns True if a Slack notification was sent, False otherwise.
    Plug Slack, Discord, or any webhook into notifier to change destination.
    """
    subreddit = item.subreddit.display_name

    if subreddit.lower() in blacklist_set:
        return False

    if item_type == "submission":
        title   = item.title or ""
        body    = item.selftext or ""
    else:  # comment
        title   = ""
        body    = item.body or ""

    item_id = item.id
    url     = f"https://reddit.com{item.permalink}"
    author  = str(item.author) if item.author else "[deleted]"
    created = item.created_utc

    result = matcher.match(title, body)
    if result is None:
        return False

    channel_name, matched_keyword = result

    if db.is_duplicate(item_id):
        logger.debug("Duplicate %s skipped: %s", item_type, item_id)
        return False

    logger.info(
        "Match — %s=%s r/%s channel=%s keyword=%r",
        item_type, item_id, subreddit, channel_name, matched_keyword,
    )

    success = notifier.send(
        channel_name=channel_name,
        post_id=item_id,
        title=title if title else f"[comment in r/{subreddit}]",
        subreddit=subreddit,
        author=author,
        url=url,
        matched_keyword=matched_keyword,
        created_utc=created,
        selftext=body,
    )

    if success:
        db.mark_processed(
            post_id=item_id,
            title=title or body[:100],
            subreddit=subreddit,
            matched_channel=channel_name,
            author=author,
            url=url,
        )
    return success


# ---------------------------------------------------------------------------
# Worker setup helpers
# ---------------------------------------------------------------------------

def _setup_worker_logging(config: AppConfig):
    """Configure logging in a freshly spawned child process."""
    os.makedirs(os.path.dirname(config.log_file), exist_ok=True)
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        config.log_file,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)


def _build_worker_deps(config: AppConfig):
    """Create per-process PRAW, DB, Notifier, Matcher, and blacklist set."""
    from dotenv import load_dotenv
    load_dotenv()

    os.makedirs(os.path.dirname(config.db_path), exist_ok=True)

    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
    )
    db = Database(config.db_path)
    notifier = SlackNotifier(config.channels, config.slack)
    matcher = KeywordMatcher(config.channels)
    blacklist_set = {s.lower() for s in config.blacklist_subreddits}
    return reddit, db, notifier, matcher, blacklist_set


# ---------------------------------------------------------------------------
# Module-level worker functions (must be picklable for multiprocessing)
# ---------------------------------------------------------------------------

def _submission_worker(config: AppConfig, shutdown_event):
    """Worker process: streams r/all (or configured scope) submissions."""
    _setup_worker_logging(config)
    log = logging.getLogger(__name__)
    log.info("Submission worker started (pid=%d)", os.getpid())

    reddit, db, notifier, matcher, blacklist_set = _build_worker_deps(config)
    processed_count = 0
    retry_count = 0

    while not shutdown_event.is_set():
        if config.max_stream_retries != -1 and retry_count >= config.max_stream_retries:
            log.error("Submission worker reached max retries (%d). Stopping.", config.max_stream_retries)
            break

        try:
            log.info("Submission worker: starting stream (attempt %d)...", retry_count + 1)
            for submission in reddit.subreddit(config.scope).stream.submissions(skip_existing=True):
                if shutdown_event.is_set():
                    break
                if send_notification(submission, "submission", matcher, notifier, db, blacklist_set):
                    processed_count += 1
                    retry_count = 0
                    if processed_count % 1000 == 0:
                        log.info("Submission worker: processed %d items. Running cleanup...", processed_count)
                        db.cleanup_old_records(config.cleanup_older_than_days)

        except prawcore.exceptions.ResponseException as e:
            if e.response.status_code == 401:
                log.critical("Reddit 401 in submission worker. Check credentials. Stopping.")
                break
            log.warning("ResponseException in submission worker: %s. Pausing %ds.", e, config.stream_pause_after_exception)
            time.sleep(config.stream_pause_after_exception)
            retry_count += 1

        except prawcore.exceptions.RequestException as e:
            log.warning("RequestException in submission worker: %s. Pausing %ds.", e, config.stream_pause_after_exception)
            time.sleep(config.stream_pause_after_exception)
            retry_count += 1

        except Exception as e:
            log.exception("Unexpected error in submission worker: %s", e)
            time.sleep(config.stream_pause_after_exception)
            retry_count += 1

    log.info("Submission worker exiting (pid=%d)", os.getpid())
    db.close()


def _comment_worker(config: AppConfig, shutdown_event):
    """Worker process: streams r/all (or configured scope) comments."""
    _setup_worker_logging(config)
    log = logging.getLogger(__name__)
    log.info("Comment worker started (pid=%d)", os.getpid())

    reddit, db, notifier, matcher, blacklist_set = _build_worker_deps(config)
    processed_count = 0
    retry_count = 0

    while not shutdown_event.is_set():
        if config.max_stream_retries != -1 and retry_count >= config.max_stream_retries:
            log.error("Comment worker reached max retries (%d). Stopping.", config.max_stream_retries)
            break

        try:
            log.info("Comment worker: starting stream (attempt %d)...", retry_count + 1)
            for comment in reddit.subreddit(config.scope).stream.comments(skip_existing=True):
                if shutdown_event.is_set():
                    break
                if send_notification(comment, "comment", matcher, notifier, db, blacklist_set):
                    processed_count += 1
                    retry_count = 0
                    if processed_count % 1000 == 0:
                        log.info("Comment worker: processed %d items. Running cleanup...", processed_count)
                        db.cleanup_old_records(config.cleanup_older_than_days)

        except prawcore.exceptions.ResponseException as e:
            if e.response.status_code == 401:
                log.critical("Reddit 401 in comment worker. Check credentials. Stopping.")
                break
            log.warning("ResponseException in comment worker: %s. Pausing %ds.", e, config.stream_pause_after_exception)
            time.sleep(config.stream_pause_after_exception)
            retry_count += 1

        except prawcore.exceptions.RequestException as e:
            log.warning("RequestException in comment worker: %s. Pausing %ds.", e, config.stream_pause_after_exception)
            time.sleep(config.stream_pause_after_exception)
            retry_count += 1

        except Exception as e:
            log.exception("Unexpected error in comment worker: %s", e)
            time.sleep(config.stream_pause_after_exception)
            retry_count += 1

    log.info("Comment worker exiting (pid=%d)", os.getpid())
    db.close()


# ---------------------------------------------------------------------------
# RedditMonitor — thin process orchestrator
# ---------------------------------------------------------------------------

class RedditMonitor:
    def __init__(self, config: AppConfig):
        self._config = config
        self._shutdown_event = multiprocessing.Event()

    def run(self):
        sub_proc = multiprocessing.Process(
            target=_submission_worker,
            args=(self._config, self._shutdown_event),
            name="submission-worker",
            daemon=True,
        )
        cmt_proc = multiprocessing.Process(
            target=_comment_worker,
            args=(self._config, self._shutdown_event),
            name="comment-worker",
            daemon=True,
        )

        sub_proc.start()
        cmt_proc.start()
        logger.info(
            "Started submission worker (pid=%d) and comment worker (pid=%d)",
            sub_proc.pid, cmt_proc.pid,
        )

        sub_proc.join()
        cmt_proc.join()

    def shutdown(self):
        logger.info("Shutdown requested — signalling workers...")
        self._shutdown_event.set()
