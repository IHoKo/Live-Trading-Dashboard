"""Logging setup — plan.md §10 Phase 6.

Uvicorn configures its own loggers and nothing else, so every `logger.info` in
this app was silently discarded in the container. The boot-time migration ran
invisibly for three phases because of it.

Output is JSON lines: Fly ships stdout to `fly logs`, and structured records
stay greppable there without a log shipper.
"""

import json
import logging
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, default=str)


def configure(level: str = "info") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Uvicorn installs its own handlers; hand its records to ours so access
    # logs and app logs come out in one format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        log = logging.getLogger(name)
        log.handlers = []
        log.propagate = True

    # The Anthropic and httpx clients are chatty at INFO and log request URLs.
    for name in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)
