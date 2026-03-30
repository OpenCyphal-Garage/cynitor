"""Tests for EventLogger (SQLite persistence)."""

import asyncio
import os
import tempfile
import pytest
from event_logger import EventLogger


def _sample_event(subject_id=100, node_id=42, msg_type="Heartbeat_1_0"):
    return {
        "subject_id": subject_id,
        "timestamp": "2026-03-19T10:00:00",
        "timestamp_unix": 1774018800.0,
        "rate": 1,
        "message_type": msg_type,
        "publisher_node_id": node_id,
        "attributes": [{"attribute": "uptime", "value": 999, "unit": "s"}],
    }


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_events.db")


@pytest.fixture
def logger(db_path):
    return EventLogger(db_path=db_path, max_events=1000)


class TestEventLogger:

    @pytest.mark.asyncio
    async def test_write_and_query(self, logger):
        logger._write_events_sync([_sample_event()])
        events = logger._get_events_sync()
        assert len(events) == 1
        assert events[0]["subject_id"] == 100
        assert events[0]["publisher_node_id"] == 42
        assert events[0]["message_type"] == "Heartbeat_1_0"

    @pytest.mark.asyncio
    async def test_query_by_subject(self, logger):
        logger._write_events_sync([
            _sample_event(subject_id=100),
            _sample_event(subject_id=200),
        ])
        events = logger._get_events_sync(subject_id=200)
        assert len(events) == 1
        assert events[0]["subject_id"] == 200

    @pytest.mark.asyncio
    async def test_query_by_node(self, logger):
        logger._write_events_sync([
            _sample_event(node_id=1),
            _sample_event(node_id=2),
        ])
        events = logger._get_events_sync(node_id=1)
        assert len(events) == 1
        assert events[0]["publisher_node_id"] == 1

    @pytest.mark.asyncio
    async def test_query_by_message_type(self, logger):
        logger._write_events_sync([
            _sample_event(msg_type="Heartbeat_1_0"),
            _sample_event(msg_type="PortList_1_0"),
        ])
        events = logger._get_events_sync(message_type="PortList_1_0")
        assert len(events) == 1
        assert events[0]["message_type"] == "PortList_1_0"

    @pytest.mark.asyncio
    async def test_event_count(self, logger):
        logger._write_events_sync([_sample_event(), _sample_event()])
        count = logger._get_event_count_sync()
        assert count == 2

    @pytest.mark.asyncio
    async def test_clear_events(self, logger):
        logger._write_events_sync([_sample_event()])
        cleared = logger._clear_events_sync()
        assert cleared == 1
        assert logger._get_event_count_sync() == 0

    @pytest.mark.asyncio
    async def test_max_events_pruning(self, db_path):
        logger = EventLogger(db_path=db_path, max_events=3)
        events = [_sample_event(subject_id=i) for i in range(5)]
        logger._write_events_sync(events)
        count = logger._get_event_count_sync()
        assert count == 3

    @pytest.mark.asyncio
    async def test_query_limit_and_offset(self, logger):
        events = [_sample_event(subject_id=i) for i in range(10)]
        logger._write_events_sync(events)
        result = logger._get_events_sync(limit=3, offset=0)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_attributes_stored_as_json(self, logger):
        logger._write_events_sync([_sample_event()])
        events = logger._get_events_sync()
        attrs = events[0]["attributes"]
        assert isinstance(attrs, list)
        assert attrs[0]["attribute"] == "uptime"

    @pytest.mark.asyncio
    async def test_async_log_and_query(self, logger):
        await logger.start()
        await logger.log_event(_sample_event())
        # Give the background loop time to process
        await asyncio.sleep(2.0)
        await logger.stop()
        count = await logger.get_event_count()
        assert count == 1
