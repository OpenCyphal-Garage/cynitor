"""Tests for the optional bearer-token auth middleware on WebSocketServer."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from aiohttp.test_utils import TestClient, TestServer

from websocket_server import WebSocketServer
from log_store import InMemoryLogStore


def _make_session():
    s = MagicMock()
    s.is_running = False
    s.can_interface = None
    s.can_bitrate = None
    s.telemetry = None
    s.bus_load = None
    s.last_error = None
    s.dropped_events.return_value = None
    s.default_data_bitrate = None
    s.can_data_bitrate = None
    s.can_fd = False
    s.replay = None
    s.event_logger = None
    s.connect = AsyncMock()
    s.disconnect = AsyncMock()
    return s


@pytest.fixture
def session():
    return _make_session()


@pytest.fixture
async def open_client(session):
    # No auth_token → server runs open, behaviour unchanged from pre-auth
    server = WebSocketServer(session=session, host="127.0.0.1", port=0, log_store=InMemoryLogStore())
    async with TestClient(TestServer(server.app)) as c:
        server._running = True
        yield c


@pytest.fixture
async def secured_client(session):
    server = WebSocketServer(
        session=session, host="127.0.0.1", port=0,
        log_store=InMemoryLogStore(),
        auth_token="s3cret",
    )
    async with TestClient(TestServer(server.app)) as c:
        server._running = True
        yield c


class TestNoAuthConfigured:
    """When CYNITOR_AUTH_TOKEN is unset, every endpoint stays open."""

    @pytest.mark.asyncio
    async def test_protected_endpoint_open(self, open_client):
        resp = await open_client.get("/api/status")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_health_open(self, open_client):
        resp = await open_client.get("/api/health")
        assert resp.status == 200


class TestAuthEnforced:
    """When the token is set, REST and WS both require it — except /api/health."""

    @pytest.mark.asyncio
    async def test_status_rejects_missing_token(self, secured_client):
        resp = await secured_client.get("/api/status")
        assert resp.status == 401
        body = await resp.json()
        assert "error" in body

    @pytest.mark.asyncio
    async def test_status_accepts_correct_token(self, secured_client):
        resp = await secured_client.get(
            "/api/status",
            headers={"Authorization": "Bearer s3cret"},
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_status_rejects_wrong_token(self, secured_client):
        resp = await secured_client.get(
            "/api/status",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_health_stays_open(self, secured_client):
        """Load balancers / uptime checks must hit /api/health without auth."""
        resp = await secured_client.get("/api/health")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_options_preflight_unauthenticated(self, secured_client):
        """CORS preflight requests don't carry the Authorization header — must succeed."""
        resp = await secured_client.options("/api/status")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_post_rejects_missing_token(self, secured_client):
        resp = await secured_client.post(
            "/api/can/connect",
            json={"interface": "vcan0"},
        )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_ws_rejects_missing_token(self, secured_client):
        """WebSocket clients can't set headers from browsers — token via query."""
        resp = await secured_client.get("/ws")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_ws_accepts_token_query_param(self, secured_client):
        """?token=<value> is the WS auth path."""
        async with secured_client.ws_connect("/ws?token=s3cret") as ws:
            await ws.send_json({"type": "ping"})
            resp = await ws.receive_json()
            assert resp["type"] == "pong"

    @pytest.mark.asyncio
    async def test_ws_rejects_wrong_token_query(self, secured_client):
        resp = await secured_client.get("/ws?token=wrong")
        assert resp.status == 401


class TestTokenExtraction:

    @pytest.mark.asyncio
    async def test_malformed_bearer_header(self, secured_client):
        """A non-Bearer Authorization scheme should be treated as missing."""
        resp = await secured_client.get(
            "/api/status",
            headers={"Authorization": "Basic s3cret"},
        )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_empty_bearer_token(self, secured_client):
        resp = await secured_client.get(
            "/api/status",
            headers={"Authorization": "Bearer "},
        )
        assert resp.status == 401
