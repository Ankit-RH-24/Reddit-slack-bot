import sqlite3
import logging
from datetime import datetime, timezone, timedelta

# NOTE: Koyeb free tier uses ephemeral storage — the SQLite database at `data/bot.db`
# will be wiped on every redeploy or container restart. Duplicate-post protection
# therefore only persists for the lifetime of the current deployment. This is an
# accepted trade-off on the free Nano tier; upgrade to persistent storage (e.g. a
# managed Postgres) if continuity across redeploys is required.

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._configure()
        self._migrate()

    def _configure(self):
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    def _migrate(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_posts (
                post_id         TEXT PRIMARY KEY,
                title           TEXT NOT NULL,
                subreddit       TEXT NOT NULL,
                matched_channel TEXT NOT NULL,
                author          TEXT,
                url             TEXT NOT NULL,
                processed_at    TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_processed_at
            ON processed_posts (processed_at)
        """)
        self.conn.commit()

    def is_duplicate(self, post_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM processed_posts WHERE post_id = ?", (post_id,)
        )
        return cur.fetchone() is not None

    def mark_processed(
        self,
        post_id: str,
        title: str,
        subreddit: str,
        matched_channel: str,
        author: str,
        url: str,
    ):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO processed_posts
                (post_id, title, subreddit, matched_channel, author, url, processed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (post_id, title, subreddit, matched_channel, author, url, now),
        )
        self.conn.commit()

    def cleanup_old_records(self, days: int):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self.conn.execute(
            "DELETE FROM processed_posts WHERE processed_at < ?", (cutoff,)
        )
        self.conn.commit()
        if cur.rowcount:
            logger.info("Cleaned up %d old records (older than %d days)", cur.rowcount, days)

    def close(self):
        self.conn.close()
