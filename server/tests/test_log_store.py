"""Tests for InMemoryLogStore and APILogHandler."""

import logging
import pytest
from log_store import InMemoryLogStore, APILogHandler


@pytest.fixture
def store():
    return InMemoryLogStore(max_entries=100)


def _make_record(message: str, level: int = logging.INFO, name: str = "test") -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname="test.py",
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


class TestInMemoryLogStore:

    def test_add_and_get(self, store):
        store.add_record(_make_record("hello"))
        logs = store.get_logs()
        assert len(logs) == 1
        assert logs[0]["message"] == "hello"
        assert logs[0]["level"] == "INFO"
        assert logs[0]["logger"] == "test"
        assert "timestamp" in logs[0]

    def test_level_filter_exact(self, store):
        store.add_record(_make_record("debug msg", logging.DEBUG))
        store.add_record(_make_record("info msg", logging.INFO))
        store.add_record(_make_record("error msg", logging.ERROR))

        logs = store.get_logs(level="ERROR")
        assert len(logs) == 1
        assert logs[0]["message"] == "error msg"

    def test_level_filter_case_insensitive(self, store):
        store.add_record(_make_record("warn", logging.WARNING))
        logs = store.get_logs(level="warning")
        assert len(logs) == 1

    def test_min_level_filter(self, store):
        store.add_record(_make_record("debug", logging.DEBUG))
        store.add_record(_make_record("info", logging.INFO))
        store.add_record(_make_record("warning", logging.WARNING))
        store.add_record(_make_record("error", logging.ERROR))

        logs = store.get_logs(min_level="WARNING")
        assert len(logs) == 2
        levels = {log["level"] for log in logs}
        assert levels == {"WARNING", "ERROR"}

    def test_limit(self, store):
        for i in range(10):
            store.add_record(_make_record(f"msg {i}"))
        logs = store.get_logs(limit=3)
        assert len(logs) == 3
        # Should return the last 3
        assert logs[0]["message"] == "msg 7"

    def test_limit_clamped(self, store):
        store.add_record(_make_record("msg"))
        logs = store.get_logs(limit=0)
        # limit is clamped to min 1
        assert len(logs) == 1

    def test_max_entries_enforced(self):
        store = InMemoryLogStore(max_entries=3)
        for i in range(5):
            store.add_record(_make_record(f"msg {i}"))
        logs = store.get_logs()
        assert len(logs) == 3
        assert logs[0]["message"] == "msg 2"

    def test_invalid_level_raises(self, store):
        with pytest.raises(ValueError, match="Invalid level"):
            store.get_logs(level="BOGUS")

    def test_invalid_min_level_raises(self, store):
        with pytest.raises(ValueError, match="Invalid min_level"):
            store.get_logs(min_level="BOGUS")

    def test_empty_store(self, store):
        assert store.get_logs() == []


class TestAPILogHandler:

    def test_handler_routes_to_store(self, store):
        handler = APILogHandler(store)
        record = _make_record("from handler")
        handler.emit(record)
        logs = store.get_logs()
        assert len(logs) == 1
        assert logs[0]["message"] == "from handler"
