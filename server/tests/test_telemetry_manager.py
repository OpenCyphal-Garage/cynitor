"""Tests for TelemetryManager (subscribe, cache, broadcast)."""

import asyncio
import datetime
import pytest
from unittest.mock import MagicMock
from telemetry_manager import TelemetryManager


def _sample_event(subject_id=100, node_id=42):
    return {
        "subject_id": subject_id,
        "timestamp": "2026-03-19T10:00:00",
        "timestamp_unix": 1774018800.0,
        "rate": 1.0,
        "message_type": "Heartbeat_1_0",
        "publisher_node_id": node_id,
        "attributes": [],
    }


@pytest.fixture
def mock_scanner():
    scanner = MagicMock()
    scanner.message_queue = asyncio.Queue()
    scanner.all_nodes = {}
    return scanner


@pytest.fixture
def manager(mock_scanner):
    return TelemetryManager(mock_scanner)


class TestSubscribeUnsubscribe:

    def test_subscribe_creates_queue(self, manager):
        q = manager.subscribe()
        assert isinstance(q, asyncio.Queue)
        assert q in manager.subscribers

    def test_unsubscribe_removes_queue(self, manager):
        q = manager.subscribe()
        manager.unsubscribe(q)
        assert q not in manager.subscribers

    def test_unsubscribe_nonexistent_is_safe(self, manager):
        q = asyncio.Queue()
        manager.unsubscribe(q)  # Should not raise


class TestStateCache:

    def test_update_state_by_subject(self, manager):
        event = _sample_event(subject_id=100)
        manager._update_state(event)
        cached = manager.get_latest_subject(100)
        assert cached is not None
        assert cached["subject_id"] == 100

    def test_update_state_by_node(self, manager):
        event = _sample_event(subject_id=100, node_id=42)
        manager._update_state(event)
        cached = manager.get_latest_node(42)
        assert 100 in cached
        assert cached[100]["publisher_node_id"] == 42

    def test_get_latest_subject_missing(self, manager):
        assert manager.get_latest_subject(999) is None

    def test_get_latest_node_missing(self, manager):
        assert manager.get_latest_node(999) == {}

    def test_multiple_subjects_per_node(self, manager):
        manager._update_state(_sample_event(subject_id=100, node_id=1))
        manager._update_state(_sample_event(subject_id=200, node_id=1))
        cached = manager.get_latest_node(1)
        assert len(cached) == 2
        assert 100 in cached
        assert 200 in cached


class TestBroadcast:

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_subscribers(self, manager):
        q = manager.subscribe()
        event = _sample_event()
        await manager._broadcast(event)
        result = q.get_nowait()
        assert result["subject_id"] == 100

    @pytest.mark.asyncio
    async def test_broadcast_to_multiple_subscribers(self, manager):
        q1 = manager.subscribe()
        q2 = manager.subscribe()
        await manager._broadcast(_sample_event())
        assert not q1.empty()
        assert not q2.empty()

    @pytest.mark.asyncio
    async def test_broadcast_full_queue_drops_oldest(self, manager):
        q = manager.subscribe(max_queue=1)
        await manager._broadcast(_sample_event(subject_id=1))
        await manager._broadcast(_sample_event(subject_id=2))
        result = q.get_nowait()
        assert result["subject_id"] == 2


class TestJsonNormalization:

    def test_datetime_converted(self, manager):
        event = {"subject_id": 1, "timestamp": datetime.datetime(2026, 1, 1), "publisher_node_id": 1}
        manager._update_state(event)
        cached = manager.get_latest_subject(1)
        assert isinstance(cached["timestamp"], str)

    def test_bytes_converted(self, manager):
        event = {"subject_id": 2, "publisher_node_id": 1, "data": b"hello"}
        manager._update_state(event)
        cached = manager.get_latest_subject(2)
        assert cached["data"] == "hello"

    def test_nested_dict(self, manager):
        event = {"subject_id": 3, "publisher_node_id": 1, "inner": {"ts": datetime.datetime(2026, 1, 1)}}
        manager._update_state(event)
        cached = manager.get_latest_subject(3)
        assert isinstance(cached["inner"]["ts"], str)


class TestTelemetryLoop:

    @pytest.mark.asyncio
    async def test_loop_consumes_and_caches(self, manager, mock_scanner):
        await manager.start()
        await mock_scanner.message_queue.put(_sample_event(subject_id=500))
        await asyncio.sleep(0.1)
        await manager.stop()
        assert manager.get_latest_subject(500) is not None

    @pytest.mark.asyncio
    async def test_loop_broadcasts_to_subscriber(self, manager, mock_scanner):
        q = manager.subscribe()
        await manager.start()
        await mock_scanner.message_queue.put(_sample_event())
        await asyncio.sleep(0.1)
        await manager.stop()
        assert not q.empty()


class TestDroppedCounts:

    @pytest.mark.asyncio
    async def test_drops_are_counted_by_subscriber_label(self, manager):
        client = manager.subscribe(max_queue=1)
        logger_q = manager.subscribe(max_queue=3, label="logger")
        for i in range(4):
            await manager._broadcast({"subject_id": i})
        assert manager.dropped["client"] == 3
        assert manager.dropped["logger"] == 1
        # The newest event is the one kept.
        assert client.get_nowait()["subject_id"] == 3
        assert logger_q.qsize() == 3
