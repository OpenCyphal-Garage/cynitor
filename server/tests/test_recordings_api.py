"""Tests for the /api/recordings REST endpoints."""

import csv
import io
import json
import sqlite3
import time
import pytest
from unittest.mock import AsyncMock, MagicMock
from aiohttp.test_utils import TestClient, TestServer
from websocket_server import WebSocketServer
from event_logger import EventLogger
from data_dir import EVENTS_DB


@pytest.fixture
def event_logger_real(tmp_path):
    el = EventLogger(db_path=str(tmp_path / "rec.db"), max_events=1000)
    el.init_db_sync()
    return el


@pytest.fixture
def session_with_logger(event_logger_real):
    session = MagicMock()
    session.is_running = False
    session.event_logger = event_logger_real
    session.ensure_event_logger = AsyncMock(return_value=event_logger_real)
    session.telemetry = None
    return session


@pytest.fixture
async def client(session_with_logger):
    server = WebSocketServer(session=session_with_logger, host="127.0.0.1", port=0)
    async with TestClient(TestServer(server.app)) as c:
        server._running = True
        yield c


@pytest.fixture
async def client_can_idle(tmp_path):
    """A real CANSession that never connected CAN, over a data folder holding
    a recording ("boot", two events) saved in an earlier session."""
    from main import CANSession
    earlier = EventLogger(db_path=tmp_path / EVENTS_DB)
    earlier.init_db_sync()
    now = time.time()
    earlier._write_events_sync([_sample_event(subject_id=100, ts=now - 30), _sample_event(subject_id=200, ts=now - 29)])
    await earlier.create_recording(name="boot", start_unix=now - 31, end_unix=now - 28)
    session = CANSession(data_dir=tmp_path)
    server = WebSocketServer(session=session, host="127.0.0.1", port=0)
    async with TestClient(TestServer(server.app)) as c:
        server._running = True
        yield c
    if session.event_logger is not None:
        await session.event_logger.stop()


def _sample_event(subject_id=100, ts=1700000000.0, node_id=42):
    return {
        "subject_id": subject_id,
        "timestamp": "2026-03-19T10:00:00",
        "timestamp_unix": ts,
        "rate": 1,
        "message_type": "Heartbeat_1_0",
        "publisher_node_id": node_id,
        "unique_id": None,
        "attributes": [{"attribute": "uptime", "value": 999, "unit": "s"}],
    }


class TestRecordingsCRUD:

    @pytest.mark.asyncio
    async def test_list_empty(self, client):
        resp = await client.get("/api/recordings")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"recordings": []}

    @pytest.mark.asyncio
    async def test_create_recording(self, client):
        resp = await client.post("/api/recordings", json={"name": "boot"})
        assert resp.status == 201
        data = await resp.json()
        assert data["recording"]["name"] == "boot"
        assert data["recording"]["end_unix"] is None

    @pytest.mark.asyncio
    async def test_create_requires_name(self, client):
        resp = await client.post("/api/recordings", json={})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_invalid_json(self, client):
        resp = await client.post("/api/recordings", data="not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_get_single(self, client):
        created = await (await client.post("/api/recordings", json={"name": "x"})).json()
        rid = created["recording"]["id"]
        resp = await client.get(f"/api/recordings/{rid}")
        assert resp.status == 200
        data = await resp.json()
        assert data["recording"]["name"] == "x"
        assert "event_count" in data["recording"]

    @pytest.mark.asyncio
    async def test_get_missing(self, client):
        resp = await client.get("/api/recordings/999999")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_stop(self, client):
        created = await (await client.post("/api/recordings", json={"name": "x"})).json()
        rid = created["recording"]["id"]
        resp = await client.post(f"/api/recordings/{rid}/stop")
        assert resp.status == 200
        data = await resp.json()
        assert data["recording"]["end_unix"] is not None
        # Second stop is a no-op
        resp2 = await client.post(f"/api/recordings/{rid}/stop")
        assert resp2.status == 404

    @pytest.mark.asyncio
    async def test_patch(self, client):
        created = await (await client.post("/api/recordings", json={"name": "x"})).json()
        rid = created["recording"]["id"]
        resp = await client.patch(f"/api/recordings/{rid}", json={"name": "renamed", "notes": "n"})
        assert resp.status == 200
        data = await resp.json()
        assert data["recording"]["name"] == "renamed"
        assert data["recording"]["notes"] == "n"

    @pytest.mark.asyncio
    async def test_patch_empty_body_400(self, client):
        created = await (await client.post("/api/recordings", json={"name": "x"})).json()
        rid = created["recording"]["id"]
        resp = await client.patch(f"/api/recordings/{rid}", json={})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_delete(self, client):
        created = await (await client.post("/api/recordings", json={"name": "x"})).json()
        rid = created["recording"]["id"]
        resp = await client.delete(f"/api/recordings/{rid}")
        assert resp.status == 200
        # gone
        resp2 = await client.get(f"/api/recordings/{rid}")
        assert resp2.status == 404


class TestWithoutCan:
    """Recordings are kept in the data folder: with CAN disconnected they are
    still listed, exported, renamed and deleted (and replayed, which needs CAN
    disconnected)."""

    @pytest.mark.asyncio
    async def test_saved_recording_is_listed_exported_renamed_and_deleted(self, client_can_idle):
        recs = (await (await client_can_idle.get("/api/recordings")).json())["recordings"]
        assert [r["name"] for r in recs] == ["boot"]
        rid = recs[0]["id"]
        export = await client_can_idle.get(f"/api/recordings/{rid}/export?format=csv")
        assert export.status == 200
        assert len((await export.text()).strip().split("\n")) == 3  # header + one row per event
        assert (await client_can_idle.patch(f"/api/recordings/{rid}", json={"name": "boot 2"})).status == 200
        assert (await client_can_idle.get("/api/recordings/buffer")).status == 200
        assert (await client_can_idle.delete(f"/api/recordings/{rid}")).status == 200
        assert (await (await client_can_idle.get("/api/recordings")).json())["recordings"] == []


class TestQuickSave:

    @pytest.mark.asyncio
    async def test_quick_save(self, client):
        resp = await client.post("/api/recordings/quick", json={"name": "last 5m", "last_seconds": 300})
        assert resp.status == 201
        data = await resp.json()
        assert data["recording"]["end_unix"] is not None
        assert data["recording"]["end_unix"] - data["recording"]["start_unix"] >= 299

    @pytest.mark.asyncio
    async def test_quick_save_requires_positive_seconds(self, client):
        resp = await client.post("/api/recordings/quick", json={"name": "x", "last_seconds": 0})
        assert resp.status == 400
        resp = await client.post("/api/recordings/quick", json={"name": "x", "last_seconds": -1})
        assert resp.status == 400
        resp = await client.post("/api/recordings/quick", json={"name": "x"})
        assert resp.status == 400


class TestExport:

    @pytest.mark.asyncio
    async def test_export_csv(self, client, event_logger_real):
        import time as _t
        now = _t.time()
        event_logger_real._write_events_sync([
            _sample_event(subject_id=100, ts=now),
            _sample_event(subject_id=200, ts=now + 0.5),
        ])
        rid = await event_logger_real.create_recording(name="x", start_unix=now - 1, end_unix=now + 1, events_source="global")
        resp = await client.get(f"/api/recordings/{rid}/export?format=csv")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/csv")
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        body = await resp.text()
        lines = body.strip().split("\n")
        assert lines[0].startswith("recording_id,timestamp_unix")
        # Two events × one attribute each = two data rows
        assert len(lines) == 3

    @pytest.mark.asyncio
    async def test_export_jsonl(self, client, event_logger_real):
        import time as _t
        now = _t.time()
        event_logger_real._write_events_sync([_sample_event(ts=now)])
        rid = await event_logger_real.create_recording(name="x", start_unix=now - 1, end_unix=now + 1, events_source="global")
        resp = await client.get(f"/api/recordings/{rid}/export?format=jsonl")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/x-ndjson")
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        assert ".jsonl" in resp.headers["Content-Disposition"]
        body = await resp.text()
        lines = [json.loads(l) for l in body.strip().split("\n")]
        assert "recording" in lines[0]
        assert lines[0]["recording"]["id"] == rid
        assert lines[1]["subject_id"] == 100

    @pytest.mark.asyncio
    async def test_export_invalid_format(self, client, event_logger_real):
        rid = await event_logger_real.create_recording(name="x")
        for fmt in ("xml", "json"):
            resp = await client.get(f"/api/recordings/{rid}/export?format={fmt}")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_export_missing_recording(self, client):
        resp = await client.get("/api/recordings/999/export?format=csv")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_csv_escapes_commas_and_quotes(self, client, event_logger_real):
        import time as _t
        now = _t.time()
        event_logger_real._write_events_sync([{
            "subject_id": 100,
            "timestamp": "ok",
            "timestamp_unix": now,
            "rate": 1,
            "message_type": 'msg,with,commas "and quotes"',
            "publisher_node_id": 1,
            "unique_id": None,
            "attributes": [{"attribute": "a", "value": "v", "unit": "u"}],
        }])
        rid = await event_logger_real.create_recording(name="x", start_unix=now - 1, end_unix=now + 1, events_source="global")
        resp = await client.get(f"/api/recordings/{rid}/export?format=csv")
        body = await resp.text()
        # The message_type cell must be quoted, with internal quotes doubled
        assert '"msg,with,commas ""and quotes"""' in body

    @pytest.mark.asyncio
    async def test_csv_gives_a_service_call_its_service_id_and_a_row_per_field(self, client, event_logger_real):
        rid = await event_logger_real.create_recording(name="x", filter_spec={"service_ids": [384]})
        detail = {"service_id": 384, "service_type": "uavcan.register.Access_1_0", "client_node_id": 10,
                  "status": "ok", "latency_ms": 7, "request": '{"name": "x"}', "response": '{"value": 42}'}
        with sqlite3.connect(event_logger_real.db_path) as conn:
            event_logger_real._insert_service_event_sync(conn.cursor(), rid, 12, None, detail, time.time())
        resp = await client.get(f"/api/recordings/{rid}/export?format=csv")
        rows = list(csv.DictReader(io.StringIO(await resp.text())))
        assert [(r["subject_id"], r["service_id"], r["publisher_node_id"], r["message_type"], r["attribute"], r["value"])
                for r in rows] == [
            ("", "384", "12", "uavcan.register.Access_1_0", "client_node_id", "10"),
            ("", "384", "12", "uavcan.register.Access_1_0", "status", "ok"),
            ("", "384", "12", "uavcan.register.Access_1_0", "latency_ms", "7"),
            ("", "384", "12", "uavcan.register.Access_1_0", "request", '{"name": "x"}'),
            ("", "384", "12", "uavcan.register.Access_1_0", "response", '{"value": 42}'),
        ]


class TestLimits:

    @pytest.mark.asyncio
    async def test_create_with_limits(self, client):
        resp = await client.post("/api/recordings", json={
            "name": "limited",
            "max_length_seconds": 60,
            "max_events": 100,
            "stop_on_limit": True,
        })
        assert resp.status == 201
        rec = (await resp.json())["recording"]
        assert rec["max_length_seconds"] == 60
        assert rec["max_events"] == 100
        assert rec["stop_on_limit"] is True

    @pytest.mark.asyncio
    async def test_create_rejects_negative_limits(self, client):
        resp = await client.post("/api/recordings", json={"name": "x", "max_length_seconds": -1})
        assert resp.status == 400
        resp = await client.post("/api/recordings", json={"name": "x", "max_events": 0})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_rejects_non_integer_max_events(self, client):
        resp = await client.post("/api/recordings", json={"name": "x", "max_events": 1.5})
        assert resp.status == 400


class TestBuffer:

    @pytest.mark.asyncio
    async def test_buffer_endpoint(self, client, event_logger_real):
        import time as _t
        event_logger_real._write_events_sync([
            _sample_event(subject_id=1, ts=_t.time()),
        ])
        resp = await client.get("/api/recordings/buffer")
        assert resp.status == 200
        data = await resp.json()
        assert data["buffer"]["event_count"] == 1
        assert data["buffer"]["retention_seconds"] > 0
        assert data["buffer"]["oldest_event_unix"] is not None


class TestPurge:

    @pytest.mark.asyncio
    async def test_delete_with_purge(self, client, event_logger_real):
        import time as _t
        now = _t.time()
        event_logger_real._write_events_sync([
            _sample_event(subject_id=100, ts=now),
            _sample_event(subject_id=200, ts=now + 1000),  # outside window
        ])
        rid = await event_logger_real.create_recording(name="x", start_unix=now - 1, end_unix=now + 1, events_source="global")
        resp = await client.delete(f"/api/recordings/{rid}?purge=true")
        assert resp.status == 200
        data = await resp.json()
        assert data["purged"] is True
        assert event_logger_real._get_event_count_sync() == 1
