import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from slack_sdk.webhook import WebhookClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


@dataclass
class SlackConfig:
    rate_limit_base_delay: float
    rate_limit_max_delay: float
    rate_limit_max_retries: int


class SlackNotifier:
    def __init__(self, channels: list, slack_config: SlackConfig):
        """
        channels: list of ChannelConfig dataclasses (name, webhook_env_var, ...)
        Raises EnvironmentError if any webhook URL env var is missing.
        """
        self._config = slack_config
        self._clients: dict[str, WebhookClient] = {}

        for ch in channels:
            url = os.environ.get(ch.webhook_env_var)
            if not url:
                raise EnvironmentError(
                    f"Missing environment variable '{ch.webhook_env_var}' "
                    f"required for Slack channel '{ch.name}'"
                )
            self._clients[ch.name] = WebhookClient(url)

    def send(
        self,
        channel_name: str,
        post_id: str,
        title: str,
        subreddit: str,
        author: str,
        url: str,
        matched_keyword: str,
        created_utc: float,
    ) -> bool:
        client = self._clients.get(channel_name)
        if client is None:
            logger.error("No Slack client configured for channel '%s'", channel_name)
            return False

        blocks = self._build_blocks(
            title=title,
            subreddit=subreddit,
            author=author,
            url=url,
            matched_keyword=matched_keyword,
            created_utc=created_utc,
        )
        return self._send_with_retry(client, blocks, channel_name)

    def _build_blocks(
        self,
        title: str,
        subreddit: str,
        author: str,
        url: str,
        matched_keyword: str,
        created_utc: float,
    ) -> list:
        posted_dt = datetime.fromtimestamp(created_utc, tz=timezone.utc)
        posted_str = posted_dt.strftime("%Y-%m-%d %H:%M UTC")

        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"New Reddit Mention — r/{subreddit}",
                    "emoji": False,
                },
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Title*\n<{url}|{title}>",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Author*\nu/{author}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Subreddit*\nr/{subreddit}",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Matched Keyword*\n`{matched_keyword}`",
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Posted*\n{posted_str}",
                    },
                ],
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Detected by Reddit Monitor • Rocket Health Bot",
                    }
                ],
            },
        ]

    def _send_with_retry(
        self, client: WebhookClient, blocks: list, channel_name: str
    ) -> bool:
        cfg = self._config
        delay = cfg.rate_limit_base_delay

        for attempt in range(cfg.rate_limit_max_retries + 1):
            try:
                response = client.send(blocks=blocks)

                if response.status_code == 200:
                    return True

                if response.status_code == 429:
                    retry_after = float(
                        response.headers.get("Retry-After", delay) if response.headers else delay
                    )
                    wait = min(retry_after, cfg.rate_limit_max_delay)
                    logger.warning(
                        "Slack rate limited (channel=%s), retrying in %.1fs (attempt %d/%d)",
                        channel_name, wait, attempt + 1, cfg.rate_limit_max_retries,
                    )
                    time.sleep(wait)
                    delay = min(delay * 2, cfg.rate_limit_max_delay)
                    continue

                if 500 <= response.status_code < 600:
                    logger.warning(
                        "Slack 5xx error %d (channel=%s), retrying in %.1fs (attempt %d/%d)",
                        response.status_code, channel_name, delay,
                        attempt + 1, cfg.rate_limit_max_retries,
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, cfg.rate_limit_max_delay)
                    continue

                # 4xx non-429: unrecoverable
                logger.error(
                    "Slack rejected message (channel=%s, status=%d): %s",
                    channel_name, response.status_code, response.body,
                )
                return False

            except SlackApiError as exc:
                logger.error("SlackApiError sending to '%s': %s", channel_name, exc)
                return False
            except Exception as exc:
                logger.error("Unexpected error sending to Slack channel '%s': %s", channel_name, exc)
                if attempt < cfg.rate_limit_max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, cfg.rate_limit_max_delay)
                    continue
                return False

        logger.error("Exhausted retries sending to Slack channel '%s'", channel_name)
        return False
