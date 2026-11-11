"""Structured JSON logs, one object per line, each with the run ID (NFR-08).

Log calls pass IDs, file names, statuses and numbers through `extra`. Report text and
personal names are never passed to a logger (NFR-09).
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

LOGGER_NAME = "defect_automation"
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self._run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "run_id": self._run_id,
            "event": record.getMessage(),
        }
        entry.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            entry["error"] = repr(record.exc_info[1])
        return json.dumps(entry, default=str)


def configure_logging(log_path: Path, run_id: str) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(JsonFormatter(run_id))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
