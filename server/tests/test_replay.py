"""Tests for ReplayManager — state machine, timing, batch reads, broadcast,
plus REST endpoint contracts on the WebSocketServer routes."""

import asyncio
import datetime
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from replay import ReplayManager, _row_to_event
from websocket_server import WebSocketServer
from log_store import InMemoryLogStore


# ---------------------------------------------------------------------------
# Fixtures: build a minimal recording_events DB on a tmp path
# ---------------------------------------------------------------------------


@pytest.fixture
def replay_db(tmp_path: Path) -> Path:
    db = tmp_path / "replay.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE recording_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                timestamp_unix REAL NOT NULL,
                timestamp_iso TEXT,
                subject_or_service_id INTEGER,
                publisher_node_id INTEGER,
                unique_id TEXT,
                message_type TEXT,
                rate INTEGER,
                attributes_json TEXT
            )
            """
        )
    return db


def _insert_event(db: Path, rec_id: int, t_unix: float, subject_id: int = 7509,
                  node_id: int = 42, message_type: str = "Heartbeat_1_0",
                  attrs: list | None = None, kind: str = "subject") -> None:
    iso = datetime.datetime.fromtimestamp(t_unix, tz=datetime.timezone.utc).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO recording_events"
            " (recording_id, kind, timestamp_unix, timestamp_iso,"
            "  subject_or_service_id, publisher_node_id, unique_id,"
            "  message_type, rate, attributes_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec_id, kind, t_unix, iso, subject_id, node_id, "u1",
             message_type, 1, json.dumps(attrs or [])),
        )


# ---------------------------------------------------------------------------
# Row translation
# ---------------------------------------------------------------------------


class TestRowToEvent:

    def test_translates_required_fields(self):
        row = {
            "id": 1,
            "timestamp_unix": 1748908800.0,
            "timestamp_iso": "2026-06-03T00:00:00",
            "subject_or_service_id": 7509,
            "publisher_node_id": 42,
            "unique_id": "abc",
            "message_type": "Heartbeat_1_0",
            "rate": 5,
            "attributes_json": json.dumps([{"name": "uptime", "value": 12}]),
        }
        ev = _row_to_event(row)
        assert ev["subject_id"] == 7509
        assert ev["publisher_node_id"] == 42
        assert ev["message_type"] == "Heartbeat_1_0"
        assert ev["rate"] == 5
        assert ev["replay"] is True
        assert ev["attributes"] == [{"name": "uptime", "value": 12}]

    def test_malformed_attributes_json_becomes_empty_list(self):
        row = {
            "id": 1, "timestamp_unix": 0.0, "timestamp_iso": None,
            "subject_or_service_id": 1, "publisher_node_id": 1, "unique_id": None,
            "message_type": "X", "rate": 0, "attributes_json": "{not valid",
        }
        ev = _row_to_event(row)
        assert ev["attributes"] == []


# ---------------------------------------------------------------------------
# Start preconditions
# ---------------------------------------------------------------------------


class TestStart:

    @pytest.mark.asyncio
    async def test_empty_recording_raises(self, replay_db):
        mgr = ReplayManager(replay_db, recording_id=1)
        with pytest.raises(ValueError, match="no replayable events"):
            await mgr.start()

    @pytest.mark.asyncio
    async def test_only_service_events_count_as_empty(self, replay_db):
        # MVP plays only kind='subject' — service_call rows must not unblock start
        _insert_event(replay_db, rec_id=1, t_unix=1.0, kind="service_call")
        mgr = ReplayManager(replay_db, recording_id=1)
        with pytest.raises(ValueError, match="no replayable events"):
            await mgr.start()


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------


class TestStatus:

    @pytest.mark.asyncio
    async def test_status_before_start(self, replay_db):
        mgr = ReplayManager(replay_db, recording_id=1, speed=2.0)
        s = mgr.status()
        assert s["active"] is False
        assert s["recording_id"] == 1
        assert s["speed"] == 2.0
        assert s["duration_s"] == 0.0

    @pytest.mark.asyncio
    async def test_duration_after_start(self, replay_db):
        _insert_event(replay_db, 1, t_unix=100.0)
        _insert_event(replay_db, 1, t_unix=110.0)
        _insert_event(replay_db, 1, t_unix=115.0)
        mgr = ReplayManager(replay_db, recording_id=1, speed=50.0)
        sub = mgr.subscribe()
        try:
            await mgr.start()
            # Drain the subscriber so the run loop can finish quickly
            for _ in range(3):
                await asyncio.wait_for(sub.get(), timeout=2.0)
        finally:
            await mgr.stop()
        s = mgr.status()
        assert s["duration_s"] == 15.0
        assert s["total_events"] == 3


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------


class TestBroadcast:

    @pytest.mark.asyncio
    async def test_all_events_reach_subscriber(self, replay_db):
        for i, ts in enumerate([10.0, 10.5, 11.0, 11.5]):
            _insert_event(replay_db, 1, t_unix=ts, subject_id=7509 + i)
        mgr = ReplayManager(replay_db, 1, speed=50.0)
        sub = mgr.subscribe()
        try:
            await mgr.start()
            received = []
            for _ in range(4):
                ev = await asyncio.wait_for(sub.get(), timeout=2.0)
                received.append(ev)
        finally:
            await mgr.stop()
        assert [e["subject_id"] for e in received] == [7509, 7510, 7511, 7512]
        assert all(e["replay"] is True for e in received)

    @pytest.mark.asyncio
    async def test_multiple_subscribers_each_get_all_events(self, replay_db):
        _insert_event(replay_db, 1, 10.0)
        _insert_event(replay_db, 1, 10.2)
        mgr = ReplayManager(replay_db, 1, speed=50.0)
        a, b = mgr.subscribe(), mgr.subscribe()
        try:
            await mgr.start()
            assert (await asyncio.wait_for(a.get(), 2.0))["timestamp_unix"] == 10.0
            assert (await asyncio.wait_for(b.get(), 2.0))["timestamp_unix"] == 10.0
            assert (await asyncio.wait_for(a.get(), 2.0))["timestamp_unix"] == 10.2
            assert (await asyncio.wait_for(b.get(), 2.0))["timestamp_unix"] == 10.2
        finally:
            await mgr.stop()


# ---------------------------------------------------------------------------
# Stop / pause / seek / speed
# ---------------------------------------------------------------------------


class TestControl:

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, replay_db):
        _insert_event(replay_db, 1, 10.0)
        mgr = ReplayManager(replay_db, 1, speed=50.0)
        await mgr.stop()  # before start → no-op
        sub = mgr.subscribe()
        await mgr.start()
        await asyncio.wait_for(sub.get(), 2.0)
        await mgr.stop()
        await mgr.stop()  # double stop → no-op

    @pytest.mark.asyncio
    async def test_seek_jumps_past_early_events(self, replay_db):
        # 10 events at t = 10..19. Seek to position 5s → first event seen is t=15.
        for ts in range(10, 20):
            _insert_event(replay_db, 1, t_unix=float(ts), subject_id=7509)
        mgr = ReplayManager(replay_db, 1, speed=50.0)
        sub = mgr.subscribe()
        try:
            await mgr.start(start_offset_s=5.0)
            ev = await asyncio.wait_for(sub.get(), timeout=2.0)
            assert ev["timestamp_unix"] >= 15.0
        finally:
            await mgr.stop()

    @pytest.mark.asyncio
    async def test_speed_is_clamped(self, replay_db):
        _insert_event(replay_db, 1, 10.0)
        mgr = ReplayManager(replay_db, 1, speed=999.0)
        assert mgr.status()["speed"] == 50.0
        mgr.set_speed(0.001)
        assert mgr.status()["speed"] == 0.1

    @pytest.mark.asyncio
    async def test_pause_blocks_emission(self, replay_db):
        # 5 events, pause immediately after start, expect no events after a brief delay
        for i, ts in enumerate([10.0, 10.1, 10.2, 10.3, 10.4]):
            _insert_event(replay_db, 1, t_unix=ts, subject_id=7509 + i)
        mgr = ReplayManager(replay_db, 1, speed=1.0)  # slow speed so pause has a chance
        sub = mgr.subscribe()
        try:
            await mgr.start()
            mgr.pause()
            # Drain whatever was already emitted before pause kicked in
            try:
                while True:
                    await asyncio.wait_for(sub.get(), timeout=0.05)
            except asyncio.TimeoutError:
                pass
            # Now with no further emission, the queue should stay empty for a moment
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(sub.get(), timeout=0.2)
            mgr.resume()
            ev = await asyncio.wait_for(sub.get(), timeout=2.0)
            assert ev["replay"] is True
        finally:
            await mgr.stop()


# ---------------------------------------------------------------------------
# Auto-finish
# ---------------------------------------------------------------------------


class TestAutoFinish:

    @pytest.mark.asyncio
    async def test_finished_flag_set_after_last_event(self, replay_db):
        _insert_event(replay_db, 1, 10.0)
        _insert_event(replay_db, 1, 10.1)
        mgr = ReplayManager(replay_db, 1, speed=50.0)
        sub = mgr.subscribe()
        try:
            await mgr.start()
            await asyncio.wait_for(sub.get(), 2.0)
            await asyncio.wait_for(sub.get(), 2.0)
            # Run loop should exit shortly after the last event
            for _ in range(20):
                if mgr.is_finished:
                    break
                await asyncio.sleep(0.05)
            assert mgr.is_finished
        finally:
            await mgr.stop()


# ---------------------------------------------------------------------------
# REST endpoint contracts (smoke level — full state-machine coverage is via
# the ReplayManager unit tests above)
# ---------------------------------------------------------------------------


def _make_session_for_rest(replay_db, is_running: bool = False):
    s = MagicMock()
    s.is_running = is_running
    s.can_interface = None
    s.can_bitrate = None
    s.telemetry = None
    s.bus_load = None
    s.last_error = None
    s.dropped_events.return_value = None
    s.replay = None
    # event_logger is a real object so the /api/nodes synthesis path works
    el = MagicMock()
    el.db_path = replay_db
    s.event_logger = el

    async def _start_replay(rec_id, speed=1.0, start_offset_s=0.0):
        if s.is_running:
            raise RuntimeError("CAN is connected — disconnect before starting replay")
        if s.replay is not None:
            raise RuntimeError("Replay already in progress")
        from replay import ReplayManager
        mgr = ReplayManager(replay_db, recording_id=rec_id, speed=speed)
        try:
            status = await mgr.start(start_offset_s=start_offset_s)
        except ValueError:
            raise
        s.replay = mgr
        return status

    async def _stop_replay():
        if s.replay is not None:
            await s.replay.stop()
            s.replay = None

    s.start_replay = _start_replay
    s.stop_replay = _stop_replay
    s.connect = AsyncMock()
    s.disconnect = AsyncMock()
    return s


@pytest.fixture
async def rest_client(replay_db):
    _insert_event(replay_db, rec_id=1, t_unix=10.0)
    _insert_event(replay_db, rec_id=1, t_unix=10.1)
    session = _make_session_for_rest(replay_db)
    server = WebSocketServer(session=session, host="127.0.0.1", port=0,
                              log_store=InMemoryLogStore())
    async with TestClient(TestServer(server.app)) as c:
        server._running = True
        yield c, session


class TestReplayRestRoutes:

    @pytest.mark.asyncio
    async def test_status_no_replay(self, rest_client):
        c, _ = rest_client
        resp = await c.get("/api/replay/status")
        assert resp.status == 200
        body = await resp.json()
        assert body["active"] is False

    @pytest.mark.asyncio
    async def test_start_returns_active_status(self, rest_client):
        c, session = rest_client
        try:
            resp = await c.post("/api/replay/start", json={"recording_id": 1, "speed": 50.0})
            assert resp.status == 200
            body = await resp.json()
            assert body["active"] is True
            assert body["recording_id"] == 1
            assert body["total_events"] == 2
        finally:
            await session.stop_replay()

    @pytest.mark.asyncio
    async def test_start_404_on_empty_recording(self, rest_client):
        c, _ = rest_client
        resp = await c.post("/api/replay/start", json={"recording_id": 999})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_start_409_when_can_connected(self, rest_client):
        c, session = rest_client
        session.is_running = True
        resp = await c.post("/api/replay/start", json={"recording_id": 1})
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_start_409_when_already_replaying(self, rest_client):
        c, session = rest_client
        try:
            await c.post("/api/replay/start", json={"recording_id": 1, "speed": 50.0})
            resp = await c.post("/api/replay/start", json={"recording_id": 1, "speed": 50.0})
            assert resp.status == 409
        finally:
            await session.stop_replay()

    @pytest.mark.asyncio
    async def test_control_actions(self, rest_client):
        c, session = rest_client
        try:
            await c.post("/api/replay/start", json={"recording_id": 1, "speed": 0.5})
            resp = await c.post("/api/replay/control", json={"action": "pause"})
            assert resp.status == 200
            assert (await resp.json())["paused"] is True

            resp = await c.post("/api/replay/control", json={"action": "resume"})
            assert resp.status == 200
            assert (await resp.json())["paused"] is False

            resp = await c.post("/api/replay/control", json={"action": "stop"})
            assert resp.status == 200
            assert (await resp.json())["active"] is False
        finally:
            await session.stop_replay()

    @pytest.mark.asyncio
    async def test_control_unknown_action(self, rest_client):
        c, session = rest_client
        try:
            await c.post("/api/replay/start", json={"recording_id": 1, "speed": 50.0})
            resp = await c.post("/api/replay/control", json={"action": "fly"})
            assert resp.status == 400
        finally:
            await session.stop_replay()

    @pytest.mark.asyncio
    async def test_seek_404_no_replay(self, rest_client):
        c, _ = rest_client
        resp = await c.post("/api/replay/seek", json={"position_s": 1.0})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_nodes_synthesised_during_replay(self, rest_client):
        c, session = rest_client
        try:
            await c.post("/api/replay/start", json={"recording_id": 1, "speed": 50.0})
            resp = await c.get("/api/nodes")
            assert resp.status == 200
            body = await resp.json()
            # The fixture used publisher_node_id=42 for both inserted events
            assert body["node_count"] == 1
            assert "42" in body["nodes"] or 42 in body["nodes"]
        finally:
            await session.stop_replay()
