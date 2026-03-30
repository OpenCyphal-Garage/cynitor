#!/usr/bin/env python3

import logging
import threading
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Any


_LEVEL_ORDER: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class InMemoryLogStore:
    """Thread-safe in-memory log storage for API retrieval."""

    def __init__(self, max_entries: int = 5000) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def add_record(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{''.join(traceback.format_exception(*record.exc_info)).strip()}"

        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "message": message,
        }

        with self._lock:
            self._entries.append(entry)

    def get_logs(
        self,
        *,
        limit: int = 200,
        level: Optional[str] = None,
        min_level: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            entries = list(self._entries)

        normalized_level = level.upper() if level else None
        normalized_min_level = min_level.upper() if min_level else None

        if normalized_level and normalized_level not in _LEVEL_ORDER:
            raise ValueError(f"Invalid level: {level}")
        if normalized_min_level and normalized_min_level not in _LEVEL_ORDER:
            raise ValueError(f"Invalid min_level: {min_level}")

        filtered: list[dict[str, Any]] = []
        for entry in entries:
            if normalized_level and entry["level"] != normalized_level:
                continue
            if normalized_min_level and _LEVEL_ORDER[entry["level"]] < _LEVEL_ORDER[normalized_min_level]:
                continue
            filtered.append(entry)

        safe_limit = max(1, min(limit, 5000))
        return filtered[-safe_limit:]


class APILogHandler(logging.Handler):
    """Logging handler that writes log records into InMemoryLogStore."""

    def __init__(self, store: InMemoryLogStore) -> None:
        super().__init__()
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.store.add_record(record)
        except Exception:
            self.handleError(record)
