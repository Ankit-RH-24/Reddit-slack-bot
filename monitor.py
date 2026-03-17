import logging
import os
import time
from dataclasses import dataclass, field

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
    subreddits: list
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
        subreddits=reddit_raw.get("subreddits", []),
        stream_pause_after_exception=reddit_raw.get("stream_pause_after_exception", 5),
        max_stream_retries=reddit_raw.get("max_stream_retries", 10),
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
        # Pre-process: store (channel_name, original_keyword, lowered_keyword, case_sensitive)
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
# Reddit monitor
# ---------------------------------------------------------------------------

class RedditMonitor:
    def __init__(self, config: AppConfig):
        self._config = config
        self._db = Database(config.db_path)
        self._notifier = SlackNotifier(config.channels, config.slack)
        self._matcher = KeywordMatcher(config.channels)
        self._processed_count = 0
        self._shutdown_requested = False

        self._reddit = praw.Reddit(
            client_id=os.environ["REDDIT_CLIENT_ID"],
            client_secret=os.environ["REDDIT_CLIENT_SECRET"],
            user_agent=os.environ["REDDIT_USER_AGENT"],
        )

    def shutdown(self):
        self._shutdown_requested = True
        self._db.close()
        logger.info("RedditMonitor: shutdown complete")

    def run(self):
        cfg = self._config
        max_retries = cfg.max_stream_retries
        retry_count = 0

        while not self._shutdown_requested:
            if max_retries != -1 and retry_count >= max_retries:
                logger.error(
                    "Reached max stream retries (%d). Stopping.", max_retries
                )
                break

            try:
                logger.info(
                    "Starting submission stream (attempt %d)...",
                    retry_count + 1,
                )
                self._stream_submissions()

            except prawcore.exceptions.ResponseException as exc:
                if exc.response.status_code == 401:
                    logger.critical(
                        "Reddit authentication failed (401). Check credentials. Stopping."
                    )
                    break
                logger.warning("Reddit ResponseException: %s. Pausing %ds.", exc, cfg.stream_pause_after_exception)
                time.sleep(cfg.stream_pause_after_exception)
                retry_count += 1

            except prawcore.exceptions.RequestException as exc:
                logger.warning("Reddit RequestException: %s. Pausing %ds.", exc, cfg.stream_pause_after_exception)
                time.sleep(cfg.stream_pause_after_exception)
                retry_count += 1

            except Exception as exc:
                logger.exception("Unexpected error in stream: %s", exc)
                time.sleep(cfg.stream_pause_after_exception)
                retry_count += 1

    def _stream_submissions(self):
        subreddit_str = "+".join(self._config.subreddits)
        subreddit = self._reddit.subreddit(subreddit_str)

        for submission in subreddit.stream.submissions(skip_existing=True):
            if self._shutdown_requested:
                break
            self._process_submission(submission)
            # Reset retry counter on any successful iteration
            # (we're inside the generator loop, so we track resets via a flag)

    def _process_submission(self, submission):
        post_id = submission.id
        title = submission.title or ""
        selftext = submission.selftext or ""
        subreddit = submission.subreddit.display_name
        author = str(submission.author) if submission.author else "[deleted]"
        url = f"https://reddit.com{submission.permalink}"
        created_utc = submission.created_utc

        result = self._matcher.match(title, selftext)
        if result is None:
            return

        channel_name, matched_keyword = result

        if self._db.is_duplicate(post_id):
            logger.debug("Duplicate post skipped: %s", post_id)
            return

        logger.info(
            "Match found — post=%s subreddit=%s channel=%s keyword=%r",
            post_id, subreddit, channel_name, matched_keyword,
        )

        success = self._notifier.send(
            channel_name=channel_name,
            post_id=post_id,
            title=title,
            subreddit=subreddit,
            author=author,
            url=url,
            matched_keyword=matched_keyword,
            created_utc=created_utc,
        )

        if success:
            self._db.mark_processed(
                post_id=post_id,
                title=title,
                subreddit=subreddit,
                matched_channel=channel_name,
                author=author,
                url=url,
            )
            self._processed_count += 1

            if self._processed_count % 1000 == 0:
                logger.info(
                    "Processed %d posts total. Running cleanup...",
                    self._processed_count,
                )
                self._db.cleanup_old_records(self._config.cleanup_older_than_days)
        else:
            logger.warning("Failed to send Slack notification for post %s", post_id)
