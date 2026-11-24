"""Write one JSON record per line to the console and rotating file."""

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from .config import LogConfig


class JsonFormatter(logging.Formatter):
    """Format both bot and library log records as JSON."""

    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        event.update(getattr(record, "context", {}))
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        return json.dumps(event, ensure_ascii=True)


def configure_logging(config: LogConfig) -> None:
    """Install the root handlers before Discord starts."""
    config.file_path.parent.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(config.file_path, maxBytes=config.max_bytes,
                            backupCount=config.backup_count, encoding="utf-8"),
    ]
    formatter = JsonFormatter()
    for handler in handlers:
        handler.setFormatter(formatter)
        # Library records can expose gateway payloads or token-bearing URLs, even at WARNING.
        handler.addFilter(lambda record: record.name != "discord"
                          and not record.name.startswith(("discord.", "aiohttp.client")))
    logging.basicConfig(level=config.level, handlers=handlers, force=True)
