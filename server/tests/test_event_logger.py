"""Tests for EventLogger (SQLite persistence)."""

import asyncio
import os
import tempfile
import pytest
from event_logger import EventLogger, FilterMatcher


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
    el = EventLogger(db_path=db_path, max_events=1000)
    el.init_db_sync()
    return el


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
        """Safety-cap pruning. Disable time-based retention so only the count
        cap can trigger; write enough events to clear the trigger threshold."""
        logger = EventLogger(db_path=db_path, retention_seconds=0, max_events=3)
        logger.init_db_sync()
        # Need >= 1000 events to cross the prune trigger; only the newest 3 survive.
        events = [_sample_event(subject_id=i) for i in range(1005)]
        logger._write_events_sync(events)
        count = logger._get_event_count_sync()
        assert count == 3
        # It is the newest three that survive.
        assert sorted(e["subject_id"] for e in logger._get_events_sync()) == [1002, 1003, 1004]

    @pytest.mark.asyncio
    async def test_time_based_pruning(self, db_path):
        """Events older than retention_seconds are deleted on the next prune."""
        import time as _t
        logger = EventLogger(db_path=db_path, retention_seconds=60, max_events=0)
        logger.init_db_sync()
        now = _t.time()
        # 1000 old (well past retention) + 5 fresh
        old = [{**_sample_event(subject_id=i), "timestamp_unix": now - 3600} for i in range(1000)]
        fresh = [{**_sample_event(subject_id=i + 1000), "timestamp_unix": now} for i in range(5)]
        logger._write_events_sync(old + fresh)
        # Prune should have fired (>=1000 writes since last prune).
        count = logger._get_event_count_sync()
        assert count == 5
        # All survivors are within retention.
        events = logger._get_events_sync(limit=10)
        for ev in events:
            assert ev["timestamp_unix"] >= now - 60

    @pytest.mark.asyncio
    async def test_buffer_stats(self, logger):
        import time as _t
        now = _t.time()
        logger._write_events_sync([
            {**_sample_event(subject_id=1), "timestamp_unix": now - 10},
            {**_sample_event(subject_id=2), "timestamp_unix": now},
        ])
        stats = await logger.get_buffer_stats()
        assert stats["event_count"] == 2
        assert stats["oldest_event_unix"] is not None
        assert stats["newest_event_unix"] >= stats["oldest_event_unix"]
        assert stats["retention_seconds"] > 0
        assert stats["db_size_bytes"] > 0

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

    # ------------------------------------------------------------------
    # Recordings (named time-range bookmarks)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_create_and_get_recording(self, logger):
        rec_id = await logger.create_recording(name="boot sequence")
        assert isinstance(rec_id, int) and rec_id > 0
        rec = await logger.get_recording(rec_id)
        assert rec["name"] == "boot sequence"
        assert rec["end_unix"] is None
        assert rec["filter"] == {}

    @pytest.mark.asyncio
    async def test_create_recording_with_filter(self, logger):
        rec_id = await logger.create_recording(
            name="filtered",
            filter_spec={"subject_ids": [100, 200], "node_ids": [42], "message_types": ["Heartbeat_1_0"]},
            notes="hello",
        )
        rec = await logger.get_recording(rec_id)
        assert rec["filter"]["subject_ids"] == [100, 200]
        assert rec["filter"]["node_ids"] == [42]
        assert rec["filter"]["message_types"] == ["Heartbeat_1_0"]
        assert rec["notes"] == "hello"

    @pytest.mark.asyncio
    async def test_create_recording_filter_rejects_garbage(self, logger):
        rec_id = await logger.create_recording(
            name="x",
            filter_spec={"subject_ids": ["abc", 100, True], "garbage_key": [1, 2], "message_types": ["", "ok", 5]},
        )
        rec = await logger.get_recording(rec_id)
        # True is bool — rejected; "abc" — rejected; True is bool not int — rejected
        assert rec["filter"]["subject_ids"] == [100]
        assert "garbage_key" not in rec["filter"]
        assert rec["filter"]["message_types"] == ["ok"]

    @pytest.mark.asyncio
    async def test_stop_recording(self, logger):
        rec_id = await logger.create_recording(name="x")
        stopped = await logger.stop_recording(rec_id)
        assert stopped is True
        rec = await logger.get_recording(rec_id)
        assert rec["end_unix"] is not None
        # Stopping again should be a no-op
        assert await logger.stop_recording(rec_id) is False

    @pytest.mark.asyncio
    async def test_update_recording(self, logger):
        rec_id = await logger.create_recording(name="orig")
        assert await logger.update_recording(rec_id, name="renamed", notes="why") is True
        rec = await logger.get_recording(rec_id)
        assert rec["name"] == "renamed"
        assert rec["notes"] == "why"
        # Empty update returns False
        assert await logger.update_recording(rec_id) is False

    @pytest.mark.asyncio
    async def test_list_recordings_orders_by_start_desc(self, logger):
        import time as _time
        a = await logger.create_recording(name="a", start_unix=_time.time() - 100)
        b = await logger.create_recording(name="b", start_unix=_time.time())
        recs = await logger.list_recordings()
        ids = [r["id"] for r in recs]
        assert ids.index(b) < ids.index(a)

    @pytest.mark.asyncio
    async def test_delete_recording_metadata_only(self, logger):
        # Write some events inside the window
        import time as _time
        now = _time.time()
        logger._write_events_sync([
            {**_sample_event(subject_id=100), "timestamp_unix": now},
        ])
        rec_id = await logger.create_recording(name="x", start_unix=now - 1, end_unix=now + 1, events_source="global")
        assert await logger.delete_recording(rec_id, purge_events=False) is True
        assert await logger.get_recording(rec_id) is None
        # Events remain
        assert logger._get_event_count_sync() == 1

    @pytest.mark.asyncio
    async def test_delete_recording_purge_events(self, logger):
        import time as _time
        now = _time.time()
        logger._write_events_sync([
            {**_sample_event(subject_id=100), "timestamp_unix": now},
            {**_sample_event(subject_id=200), "timestamp_unix": now + 0.5},
            {**_sample_event(subject_id=300), "timestamp_unix": now + 1000},  # outside window
        ])
        rec_id = await logger.create_recording(name="x", start_unix=now - 1, end_unix=now + 1, events_source="global")
        assert await logger.delete_recording(rec_id, purge_events=True) is True
        assert logger._get_event_count_sync() == 1  # only the out-of-window one survives

    @pytest.mark.asyncio
    async def test_delete_recording_purge_respects_filter(self, logger):
        import time as _time
        now = _time.time()
        logger._write_events_sync([
            {**_sample_event(subject_id=100), "timestamp_unix": now},
            {**_sample_event(subject_id=200), "timestamp_unix": now},
        ])
        rec_id = await logger.create_recording(
            name="x", start_unix=now - 1, end_unix=now + 1,
            filter_spec={"subject_ids": [100]},
            events_source="global",
        )
        await logger.delete_recording(rec_id, purge_events=True)
        events = logger._get_events_sync()
        assert [e["subject_id"] for e in events] == [200]

    @pytest.mark.asyncio
    async def test_recording_stats(self, logger):
        import time as _time
        now = _time.time()
        logger._write_events_sync([
            {**_sample_event(subject_id=100, node_id=1), "timestamp_unix": now},
            {**_sample_event(subject_id=200, node_id=1), "timestamp_unix": now + 0.5},
            {**_sample_event(subject_id=100, node_id=2), "timestamp_unix": now + 0.7},
        ])
        rec_id = await logger.create_recording(name="x", start_unix=now - 1, end_unix=now + 1, events_source="global")
        stats = await logger.get_recording_stats(rec_id)
        assert stats["event_count"] == 3
        assert stats["subjects"] == [100, 200]
        assert stats["duration_seconds"] > 0

    @pytest.mark.asyncio
    async def test_recording_stats_with_filter(self, logger):
        import time as _time
        now = _time.time()
        logger._write_events_sync([
            {**_sample_event(subject_id=100, node_id=1), "timestamp_unix": now},
            {**_sample_event(subject_id=200, node_id=2), "timestamp_unix": now},
        ])
        rec_id = await logger.create_recording(
            name="x", start_unix=now - 1, end_unix=now + 1,
            filter_spec={"subject_ids": [100]},
            events_source="global",
        )
        stats = await logger.get_recording_stats(rec_id)
        assert stats["event_count"] == 1
        assert stats["subjects"] == [100]

    @pytest.mark.asyncio
    async def test_recording_events_ordered(self, logger):
        import time as _time
        now = _time.time()
        logger._write_events_sync([
            {**_sample_event(subject_id=200), "timestamp_unix": now + 0.5},
            {**_sample_event(subject_id=100), "timestamp_unix": now},
        ])
        rec_id = await logger.create_recording(name="x", start_unix=now - 1, end_unix=now + 1, events_source="global")
        events = await logger.get_recording_events(rec_id)
        # ascending timestamp_unix
        assert [e["subject_id"] for e in events] == [100, 200]

    @pytest.mark.asyncio
    async def test_recording_live_end_defaults_to_now(self, logger):
        """An unstopped legacy bookmark's stats include events up to 'now'."""
        import time as _time
        now = _time.time()
        rec_id = await logger.create_recording(name="x", start_unix=now - 1, events_source="global")
        logger._write_events_sync([
            {**_sample_event(subject_id=100), "timestamp_unix": now},
        ])
        stats = await logger.get_recording_stats(rec_id)
        assert stats["event_count"] == 1
        assert stats["end_unix"] is None

    # ------------------------------------------------------------------
    # Phase 2: filter matcher, dedicated mode, live routing, auto-stop
    # ------------------------------------------------------------------

    def test_filter_matcher_empty_matches_all(self):
        m = FilterMatcher({})
        assert m.matches_subject({"subject_id": 1, "publisher_node_id": 1, "message_type": "X"})
        assert m.matches_service({"service_id": 1, "node_id": 1})

    def test_filter_matcher_or_across_dimensions(self):
        m = FilterMatcher({"subject_ids": [100], "node_ids": [42]})
        # Subject hit
        assert m.matches_subject({"subject_id": 100, "publisher_node_id": 1, "message_type": "X"})
        # Node hit
        assert m.matches_subject({"subject_id": 999, "publisher_node_id": 42, "message_type": "X"})
        # No match
        assert not m.matches_subject({"subject_id": 999, "publisher_node_id": 1, "message_type": "X"})

    def test_filter_matcher_service_dimension(self):
        m = FilterMatcher({"service_ids": [384], "node_ids": [42]})
        assert m.matches_service({"service_id": 384, "node_id": 99})
        assert m.matches_service({"service_id": 1, "node_id": 42})
        assert not m.matches_service({"service_id": 1, "node_id": 99})

    @pytest.mark.asyncio
    async def test_dedicated_recording_routes_live_events(self, logger):
        await logger.start()
        try:
            rec_id = await logger.create_recording(
                name="live", filter_spec={"subject_ids": [100]},
            )
            logger._write_events_sync([
                _sample_event(subject_id=100),
                _sample_event(subject_id=200),  # filtered out
                _sample_event(subject_id=100),
            ])
            events = await logger.get_recording_events(rec_id)
            assert len(events) == 2
            assert all(e["subject_id"] == 100 for e in events)
            stats = await logger.get_recording_stats(rec_id)
            assert stats["event_count"] == 2
        finally:
            await logger.stop()

    @pytest.mark.asyncio
    async def test_recording_event_count_column_across_batches(self, logger):
        # The count is written once per batch; it must still add up.
        await logger.start()
        try:
            rec_id = await logger.create_recording(name="count", filter_spec={"subject_ids": [100]})
            logger._write_events_sync([_sample_event(subject_id=100)] * 3 + [_sample_event(subject_id=200)])
            logger._write_events_sync([_sample_event(subject_id=100)] * 2)
            rec = await logger.get_recording(rec_id)
            assert rec["event_count"] == 5
        finally:
            await logger.stop()

    @pytest.mark.asyncio
    async def test_dedicated_recording_no_filter_captures_all(self, logger):
        await logger.start()
        try:
            rec_id = await logger.create_recording(name="all")
            logger._write_events_sync([
                _sample_event(subject_id=100),
                _sample_event(subject_id=200),
            ])
            events = await logger.get_recording_events(rec_id)
            assert len(events) == 2
        finally:
            await logger.stop()

    @pytest.mark.asyncio
    async def test_stopped_recording_stops_receiving(self, logger):
        await logger.start()
        try:
            rec_id = await logger.create_recording(name="x")
            logger._write_events_sync([_sample_event(subject_id=100)])
            await logger.stop_recording(rec_id)
            logger._write_events_sync([_sample_event(subject_id=100)])
            events = await logger.get_recording_events(rec_id)
            assert len(events) == 1  # only the pre-stop event
        finally:
            await logger.stop()

    @pytest.mark.asyncio
    async def test_max_events_auto_stop(self, logger):
        await logger.start()
        try:
            rec_id = await logger.create_recording(
                name="capped", max_events=3, stop_on_limit=True,
            )
            await logger._write_events([_sample_event(subject_id=100) for _ in range(5)])
            # First 3 fire auto-stop; events 4-5 arrive after unregister so they don't get captured.
            rec = await logger.get_recording(rec_id)
            assert rec["end_unix"] is not None
            assert rec["auto_stopped"] is True
            assert rec["event_count"] == 3
        finally:
            await logger.stop()

    @pytest.mark.asyncio
    async def test_max_events_soft_no_auto_stop(self, logger):
        """stop_on_limit=False: limits are targets, recording stays open."""
        await logger.start()
        try:
            rec_id = await logger.create_recording(
                name="soft", max_events=2, stop_on_limit=False,
            )
            await logger._write_events([_sample_event(subject_id=100) for _ in range(5)])
            rec = await logger.get_recording(rec_id)
            assert rec["end_unix"] is None
            assert rec["auto_stopped"] is False
            assert rec["event_count"] == 5
        finally:
            await logger.stop()

    @pytest.mark.asyncio
    async def test_quick_save_copies_from_global(self, logger):
        """Quick-save (end_unix set, events_source='dedicated') snapshots
        matching events from the global buffer."""
        import time as _t
        now = _t.time()
        logger._write_events_sync([
            {**_sample_event(subject_id=100), "timestamp_unix": now - 5},
            {**_sample_event(subject_id=200), "timestamp_unix": now - 4},
            {**_sample_event(subject_id=100), "timestamp_unix": now - 3},
        ])
        rec_id = await logger.create_recording(
            name="quick", start_unix=now - 10, end_unix=now,
            filter_spec={"subject_ids": [100]},
        )
        events = await logger.get_recording_events(rec_id)
        assert len(events) == 2
        assert all(e["subject_id"] == 100 for e in events)
        rec = await logger.get_recording(rec_id)
        assert rec["event_count"] == 2

    @pytest.mark.asyncio
    async def test_service_call_routes_to_recording(self, logger):
        await logger.start()
        try:
            rec_id = await logger.create_recording(
                name="svc", filter_spec={"service_ids": [384]},
            )
            await logger.log_node_event(
                node_id=42, event_type="service_call",
                detail={"service_id": 384, "service_type": "GetInfo", "status": "ok",
                        "latency_ms": 5.0, "response": {"x": 1}},
                unique_id="aaa",
            )
            await asyncio.sleep(0.05)
            events = await logger.get_recording_events(rec_id)
            assert len(events) == 1
            assert events[0]["kind"] == "service_call"
            assert events[0]["service_id"] == 384
            assert events[0]["message_type"] == "GetInfo"
        finally:
            await logger.stop()

    @pytest.mark.asyncio
    async def test_dedicated_purge_does_not_touch_global(self, logger):
        """Deleting a dedicated recording removes its dedicated rows but
        leaves the global events table alone."""
        await logger.start()
        try:
            rec_id = await logger.create_recording(name="x")
            logger._write_events_sync([_sample_event(subject_id=100)])
            assert logger._get_event_count_sync() == 1
            await logger.delete_recording(rec_id, purge_events=True)
            # Dedicated rows gone; global event remains.
            assert logger._get_event_count_sync() == 1
        finally:
            await logger.stop()

    @pytest.mark.asyncio
    async def test_active_recordings_rehydrate_on_start(self, db_path):
        """Live recordings persist their registration via the DB so a
        backend restart picks them back up."""
        el1 = EventLogger(db_path=db_path)
        el1.init_db_sync()
        await el1.start()
        try:
            rec_id = await el1.create_recording(name="alive")
        finally:
            await el1.stop()
        # Simulate restart with a fresh EventLogger pointing at the same DB.
        el2 = EventLogger(db_path=db_path)
        await el2.start()
        try:
            assert rec_id in el2._active_recordings
            # Live ingest still routes:
            el2._write_events_sync([_sample_event(subject_id=100)])
            events = await el2.get_recording_events(rec_id)
            assert len(events) == 1
        finally:
            await el2.stop()

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


class TestLoggerThroughput:

    @pytest.mark.asyncio
    async def test_full_queue_counts_dropped_events(self, db_path):
        el = EventLogger(db_path=db_path)
        el._queue = asyncio.Queue(maxsize=2)
        for i in range(5):
            await el.log_event(_sample_event(subject_id=i))
        assert el.dropped_events == 3

    @pytest.mark.asyncio
    async def test_queued_events_are_written_in_large_batches(self, db_path):
        el = EventLogger(db_path=db_path)
        el.init_db_sync()
        batches = []
        async def record(events):
            batches.append(len(events))
        el._write_events = record
        for i in range(1200):
            el._queue.put_nowait(_sample_event(subject_id=i))
        el._running = False  # drain what is queued, then exit
        await el._log_loop()
        assert batches == [EventLogger.BATCH_SIZE, EventLogger.BATCH_SIZE, 200]
