"""Smoke tests for the WebSocket event-stream subscribe/broadcast/filter path.

The existing test_websocket_server.py covers WS protocol-level frames (ping,
filter, invalid JSON) with telemetry set to None. These tests exercise the
full pipeline with a real TelemetryManager-shaped fake: a client connects,
events get pushed into its subscriber queue, and the handler forwards them
through the WebSocket.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from aiohttp.test_utils import TestClient, TestServer

from websocket_server import WebSocketServer
from log_store import InMemoryLogStore


class FakeTelemetry:
    """Minimal stand-in for TelemetryManager exposing subscribe/unsubscribe."""

    def __init__(self):
        self._queues = []

    def subscribe(self, max_queue=100):
        q = asyncio.Queue(maxsize=max_queue)
        self._queues.append(q)
        return q

    def unsubscribe(self, q):
        if q in self._queues:
            self._queues.remove(q)

    async def emit(self, event):
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


def _make_session_with_telemetry():
    session = MagicMock()
    session.is_running = True
    session.can_interface = "vcan0"
    session.telemetry = FakeTelemetry()
    session.bus_load = None
    session.last_error = None
    session.dropped_events.return_value = None
    session.replay = None
    session.event_logger = None
    session.connect = AsyncMock()
    session.disconnect = AsyncMock()
    return session


@pytest.fixture
def session():
    return _make_session_with_telemetry()


@pytest.fixture
def server(session):
    return WebSocketServer(session=session, host="127.0.0.1", port=0, log_store=InMemoryLogStore())


@pytest.fixture
async def client(server):
    async with TestClient(TestServer(server.app)) as c:
        server._running = True
        yield c


def _telemetry_event(subject_id, node_id=42, message_type="Heartbeat_1_0"):
    return {
        "subject_id": subject_id,
        "timestamp": "2026-06-03T00:00:00",
        "timestamp_unix": 1748908800.0,
        "rate": 1,
        "message_type": message_type,
        "publisher_node_id": node_id,
        "payload_bytes": 7,
        "attributes": [],
    }


async def _recv_event(ws, timeout=2.0):
    """Receive one event message, skipping filter_updated/metrics noise."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError("no telemetry event received")
        msg = await asyncio.wait_for(ws.receive_json(), timeout=remaining)
        if msg.get("type") in ("filter_updated", "pong", "metrics"):
            continue
        return msg


class TestSubscribeBroadcast:

    @pytest.mark.asyncio
    async def test_event_reaches_unfiltered_client(self, client, session):
        async with client.ws_connect("/ws") as ws:
            # Allow handler to wire up its subscriber
            await asyncio.sleep(0.1)
            await session.telemetry.emit(_telemetry_event(subject_id=7509))
            msg = await _recv_event(ws)
            assert msg["subject_id"] == 7509
            assert msg["publisher_node_id"] == 42

    @pytest.mark.asyncio
    async def test_filter_drops_non_matching_subject(self, client, session):
        async with client.ws_connect("/ws") as ws:
            await ws.send_json({"type": "filter", "subject_ids": [100]})
            ack = await ws.receive_json()
            assert ack["type"] == "filter_updated"
            await asyncio.sleep(0.05)
            # Non-matching event should NOT reach the client
            await session.telemetry.emit(_telemetry_event(subject_id=7509))
            # Matching event SHOULD reach the client
            await session.telemetry.emit(_telemetry_event(subject_id=100))
            msg = await _recv_event(ws)
            assert msg["subject_id"] == 100, "only filter-matching events should pass"

    @pytest.mark.asyncio
    async def test_filter_by_node_id(self, client, session):
        async with client.ws_connect("/ws") as ws:
            await ws.send_json({"type": "filter", "node_ids": [99]})
            ack = await ws.receive_json()
            assert ack["type"] == "filter_updated"
            await asyncio.sleep(0.05)
            await session.telemetry.emit(_telemetry_event(subject_id=7509, node_id=42))
            await session.telemetry.emit(_telemetry_event(subject_id=7509, node_id=99))
            msg = await _recv_event(ws)
            assert msg["publisher_node_id"] == 99


class TestClientLifecycle:

    @pytest.mark.asyncio
    async def test_subscriber_released_on_disconnect(self, client, session):
        """When the client disconnects, the handler must unsubscribe its
        queue from the telemetry manager — otherwise leaked queues would
        pile up across reconnects."""
        before = len(session.telemetry._queues)
        async with client.ws_connect("/ws") as ws:
            await asyncio.sleep(0.1)
            during = len(session.telemetry._queues)
            assert during == before + 1, "subscriber registered while client is connected"
        # Allow the server-side teardown to complete
        await asyncio.sleep(0.1)
        after = len(session.telemetry._queues)
        assert after == before, "subscriber released after client disconnect"

    @pytest.mark.asyncio
    async def test_multiple_clients_get_independent_streams(self, client, session):
        async with client.ws_connect("/ws") as ws1, client.ws_connect("/ws") as ws2:
            await asyncio.sleep(0.1)
            await session.telemetry.emit(_telemetry_event(subject_id=7509))
            msg1 = await _recv_event(ws1)
            msg2 = await _recv_event(ws2)
            assert msg1["subject_id"] == 7509
            assert msg2["subject_id"] == 7509
