"""Tests for EventLogger (SQLite persistence)."""

import asyncio
import os
import tempfile
import pytest
from event_logger import EventLogger


def _sample_event(subject_id=100, node_id=42, msg_type="Heartbeat_1_0", unique_id=None):
    return {
        "subject_id": subject_id,
        "timestamp": "2026-03-19T10:00:00",
        "timestamp_unix": 1774018800.0,
        "rate": 1,
        "message_type": msg_type,
        "publisher_node_id": node_id,
        "unique_id": unique_id,
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

    @pytest.mark.asyncio
    async def test_unique_id_stored_in_events(self, logger):
        uid = "aabbccdd" * 4
        logger._write_events_sync([_sample_event(unique_id=uid)])
        events = logger._get_events_sync()
        assert events[0]["unique_id"] == uid

    @pytest.mark.asyncio
    async def test_query_events_by_unique_id(self, logger):
        uid = "aabbccdd" * 4
        logger._write_events_sync([
            _sample_event(node_id=1, unique_id=uid),
            _sample_event(node_id=2, unique_id=uid),
            _sample_event(node_id=3, unique_id="other_uid"),
        ])
        events = logger._get_events_sync(unique_id=uid)
        assert len(events) == 2
        assert all(e["unique_id"] == uid for e in events)

    @pytest.mark.asyncio
    async def test_unique_id_prefers_over_node_id(self, logger):
        """When unique_id is provided, node_id filter is ignored."""
        uid = "aabbccdd" * 4
        logger._write_events_sync([
            _sample_event(node_id=1, unique_id=uid),
            _sample_event(node_id=2, unique_id=uid),
            _sample_event(node_id=1, unique_id="other_uid"),
        ])
        events = logger._get_events_sync(node_id=1, unique_id=uid)
        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_node_history_with_unique_id(self, logger):
        uid = "aabbccdd" * 4
        logger._log_node_event_sync(node_id=10, event_type="first_seen", unique_id=uid)
        logger._log_node_event_sync(node_id=20, event_type="reappeared", unique_id=uid)
        logger._log_node_event_sync(node_id=10, event_type="health_change", unique_id="other")
        history = logger._get_node_history_sync(node_id=10, unique_id=uid)
        assert len(history) == 2
        assert all(h["unique_id"] == uid for h in history)

    @pytest.mark.asyncio
    async def test_node_history_fallback_to_node_id(self, logger):
        logger._log_node_event_sync(node_id=10, event_type="first_seen")
        logger._log_node_event_sync(node_id=10, event_type="reappeared")
        history = logger._get_node_history_sync(node_id=10)
        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_subject_summary_by_unique_id(self, logger):
        uid = "aabbccdd" * 4
        logger._write_events_sync([
            _sample_event(subject_id=100, node_id=1, unique_id=uid),
            _sample_event(subject_id=100, node_id=2, unique_id=uid),
            _sample_event(subject_id=200, node_id=1, unique_id="other"),
        ])
        summary = logger._get_subject_summary_sync(node_id=1, unique_id=uid)
        assert len(summary) == 1
        assert summary[0]["subject_id"] == 100
        assert summary[0]["total_events"] == 2

    @pytest.mark.asyncio
    async def test_save_and_load_identity_map(self, logger):
        records = [
            {
                "unique_id": "aaa111",
                "current_node_id": 5,
                "last_seen_unix": 1700000000.0,
                "node_name": "my_node",
                "previous_node_ids": [3, 5],
            },
            {
                "unique_id": "bbb222",
                "current_node_id": None,
                "last_seen_unix": 1700000100.0,
                "node_name": None,
                "previous_node_ids": [10],
            },
        ]
        logger._save_identity_map_sync(records)
        loaded = logger._load_identity_map_sync()
        assert len(loaded) == 2

        by_uid = {r["unique_id"]: r for r in loaded}
        assert by_uid["aaa111"]["current_node_id"] == 5
        assert by_uid["aaa111"]["node_name"] == "my_node"
        assert by_uid["aaa111"]["previous_node_ids"] == [3, 5]
        assert by_uid["bbb222"]["current_node_id"] is None
        assert by_uid["bbb222"]["node_name"] is None

    @pytest.mark.asyncio
    async def test_save_identity_map_replaces_previous(self, logger):
        logger._save_identity_map_sync([
            {"unique_id": "aaa111", "current_node_id": 5, "previous_node_ids": [5]},
        ])
        logger._save_identity_map_sync([
            {"unique_id": "bbb222", "current_node_id": 10, "previous_node_ids": [10]},
        ])
        loaded = logger._load_identity_map_sync()
        assert len(loaded) == 1
        assert loaded[0]["unique_id"] == "bbb222"

    @pytest.mark.asyncio
    async def test_load_identity_map_empty(self, logger):
        loaded = logger._load_identity_map_sync()
        assert loaded == []

    @pytest.mark.asyncio
    async def test_save_load_identity_map_async(self, logger):
        records = [
            {
                "unique_id": "ccc333",
                "current_node_id": 42,
                "last_seen_unix": 1700000200.0,
                "node_name": "async_node",
                "previous_node_ids": [42],
            },
        ]
        await logger.save_identity_map(records)
        loaded = await logger.load_identity_map()
        assert len(loaded) == 1
        assert loaded[0]["unique_id"] == "ccc333"
        assert loaded[0]["node_name"] == "async_node"

    # ------------------------------------------------------------------
    # Node data persistence
    # ------------------------------------------------------------------

    def test_save_and_load_node_data(self, logger):
        snapshot = {
            "unique_id": [1, 2, 3],
            "name": "my.node",
            "software_version": {"major": 2, "minor": 5},
            "publishers": [100, 200],
            "subscribers": [300],
            "servers": [430],
            "clients": [],
            "uptime": 12345,
            "last_seen": "2026-03-19T10:00:00",
        }
        logger._save_node_data_sync("aaa111", snapshot)
        loaded = logger._load_all_node_data_sync()
        assert "aaa111" in loaded
        s = loaded["aaa111"]
        assert s["name"] == "my.node"
        assert s["software_version"] == {"major": 2, "minor": 5}
        assert s["publishers"] == [100, 200]
        assert s["subscribers"] == [300]
        assert s["servers"] == [430]
        assert s["clients"] == []
        assert s["unique_id"] == [1, 2, 3]
        assert s["uptime"] == 12345
        assert s["last_seen"] == "2026-03-19T10:00:00"

    def test_save_node_data_upsert(self, logger):
        snap1 = {"name": "old.name", "publishers": [100]}
        logger._save_node_data_sync("uid1", snap1)
        snap2 = {"name": "new.name", "publishers": [200, 300]}
        logger._save_node_data_sync("uid1", snap2)
        loaded = logger._load_all_node_data_sync()
        assert len(loaded) == 1
        assert loaded["uid1"]["name"] == "new.name"
        assert loaded["uid1"]["publishers"] == [200, 300]

    def test_load_node_data_empty(self, logger):
        loaded = logger._load_all_node_data_sync()
        assert loaded == {}

    def test_save_node_data_no_version(self, logger):
        snap = {"name": "bare.node"}
        logger._save_node_data_sync("uid2", snap)
        loaded = logger._load_all_node_data_sync()
        assert loaded["uid2"]["software_version"] is None
        assert loaded["uid2"]["publishers"] == []

    @pytest.mark.asyncio
    async def test_save_load_node_data_async(self, logger):
        snapshot = {
            "unique_id": [10, 20],
            "name": "async.node",
            "software_version": {"major": 1, "minor": 0},
            "publishers": [500],
            "subscribers": [],
            "servers": [384, 385, 430],
            "clients": [430],
            "uptime": 99,
            "last_seen": "2026-05-08T12:00:00",
        }
        await logger.save_node_data("uid_async", snapshot)
        loaded = await logger.load_all_node_data()
        assert "uid_async" in loaded
        assert loaded["uid_async"]["name"] == "async.node"
        assert loaded["uid_async"]["servers"] == [384, 385, 430]
