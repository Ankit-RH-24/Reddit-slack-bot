import http.server
import logging
import multiprocessing
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from monitor import RedditMonitor, load_config


def _start_health_server(port: int = 8000):
    """
    Tiny HTTP server that replies 200 OK to every request.
    Runs in a daemon thread — satisfies Koyeb's TCP/HTTP health check
    without affecting the bot's logic.
    """
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass  # suppress per-request access logs

    server = http.server.HTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server

_monitor: RedditMonitor | None = None


def setup_logging(config):
    os.makedirs(os.path.dirname(config.log_file), exist_ok=True)

    level = getattr(logging, config.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)


def _handle_signal(signum, frame):
    global _monitor
    logging.getLogger(__name__).info("Received signal %d, shutting down...", signum)
    if _monitor is not None:
        _monitor.shutdown()
    sys.exit(0)


def main():
    global _monitor

    load_dotenv()

    config = load_config("config.yaml")

    os.makedirs(os.path.dirname(config.db_path), exist_ok=True)
    setup_logging(config)

    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("Reddit Slack Bot starting up")
    logger.info("Scope: r/%s", config.scope)
    logger.info("Blacklisted subreddits: %d", len(config.blacklist_subreddits))
    logger.info(
        "Routing to %d channel(s): %s",
        len(config.channels),
        ", ".join(ch.name for ch in config.channels),
    )
    logger.info("=" * 60)

    signal.signal(signal.SIGTERM, _handle_signal)

    _start_health_server(port=8000)
    logger.info("Health check server listening on port 8000")

    _monitor = RedditMonitor(config)

    try:
        _monitor.run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down...")
    finally:
        if _monitor is not None:
            _monitor.shutdown()
        logger.info("Reddit Slack Bot stopped.")


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    main()
