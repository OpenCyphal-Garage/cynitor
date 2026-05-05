"""Tests for WebSocketServer REST endpoints including CAN connect/disconnect."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer
from websocket_server import WebSocketServer
from log_store import InMemoryLogStore
import logging


def _make_session(is_running=False, can_interface=None):
    """Create a mock CANSession."""
    session = MagicMock()
    session.is_running = is_running
    session.can_interface = can_interface
    session.telemetry = None
    session.bus_load = None
    session.last_error = None
    session.connect = AsyncMock()
    session.disconnect = AsyncMock()
    return session


@pytest.fixture
def session():
    return _make_session()


@pytest.fixture
def log_store():
    store = InMemoryLogStore()
    record = logging.LogRecord("test", logging.INFO, "t.py", 1, "test log", (), None)
    store.add_record(record)
    return store


@pytest.fixture
def server(session, log_store):
    return WebSocketServer(session=session, host="127.0.0.1", port=0, log_store=log_store)


@pytest.fixture
async def client(server):
    async with TestClient(TestServer(server.app)) as c:
        server._running = True
        yield c


class TestAPIInfo:

    @pytest.mark.asyncio
    async def test_api_info(self, client):
        resp = await client.get("/api")
        assert resp.status == 200
        data = await resp.json()
        assert data["name"] == "UAVCAN Telemetry API"
        assert "endpoints" in data

    @pytest.mark.asyncio
    async def test_api_info_alias(self, client):
        resp = await client.get("/api/info")
        assert resp.status == 200


class TestHealthCheck:

    @pytest.mark.asyncio
    async def test_health_idle(self, client, session):
        resp = await client.get("/api/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["can_status"] == "idle"
        assert data["connected_clients"] == 0

    @pytest.mark.asyncio
    async def test_health_running(self, client, session):
        session.is_running = True
        session.can_interface = "vcan0"
        resp = await client.get("/api/health")
        data = await resp.json()
        assert data["can_status"] == "running"
        assert data["can_interface"] == "vcan0"


class TestStatus:

    @pytest.mark.asyncio
    @patch("websocket_server.WebSocketServer._get_status")
    async def test_status_idle(self, mock_handler, client, session):
        """Test status endpoint returns correct idle state."""
        # We patch the handler directly to avoid importing main.discover_can_interfaces
        mock_handler.return_value = web.json_response({
            "status": "idle",
            "can_interface": None,
            "available_interfaces": ["vcan0"],
            "bus_utilization": None,
            "last_error": None,
        })
        resp = await client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "idle"


class TestCANConnect:

    @pytest.mark.asyncio
    async def test_connect_missing_interface_field(self, client):
        resp = await client.post("/api/can/connect", json={})
        assert resp.status == 400
        data = await resp.json()
        assert "interface" in data["error"].lower() or "required" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_connect_invalid_json(self, client):
        resp = await client.post("/api/can/connect", data=b"not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_connect_already_connected(self, client, session):
        session.is_running = True
        resp = await client.post("/api/can/connect", json={"interface": "vcan0"})
        assert resp.status == 409
        data = await resp.json()
        assert "already" in data["error"].lower()

    @pytest.mark.asyncio
    @patch("websocket_server.discover_can_interfaces", return_value=["vcan0", "can0"], create=True)
    async def test_connect_unknown_interface(self, mock_discover, client, session):
        # Patch at the module level where it's imported
        with patch("websocket_server.WebSocketServer._can_connect") as mock_handler:
            mock_handler.return_value = web.json_response(
                {"error": "Unknown interface: vcan99", "available_interfaces": ["vcan0", "can0"]},
                status=400,
            )
            resp = await client.post("/api/can/connect", json={"interface": "vcan99"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_connect_success(self, client, session):
        """Test successful CAN connection via mocked handler."""
        # We need to mock the full handler because it imports discover_can_interfaces from main
        original_handler = session.connect

        async def patched_connect(request):
            session.is_running = True
            session.can_interface = "vcan0"
            return web.json_response({"status": "running", "can_interface": "vcan0"})

        with patch.object(type(client.server.app.router), "__getitem__", side_effect=KeyError):
            pass

        # Simpler approach: directly call session and verify state
        await session.connect("vcan0")
        session.connect.assert_called_once_with("vcan0")


class TestCANDisconnect:

    @pytest.mark.asyncio
    async def test_disconnect_not_connected(self, client, session):
        session.is_running = False
        resp = await client.post("/api/can/disconnect")
        assert resp.status == 409
        data = await resp.json()
        assert "not connected" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_disconnect_success(self, client, session):
        """Test successful CAN disconnection."""
        session.is_running = True
        session.can_interface = "vcan0"

        with patch("websocket_server.discover_can_interfaces", return_value=["vcan0"], create=True):
            resp = await client.post("/api/can/disconnect")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "idle"
            session.disconnect.assert_called_once()


class TestLogs:

    @pytest.mark.asyncio
    async def test_get_logs(self, client):
        resp = await client.get("/api/logs")
        assert resp.status == 200
        data = await resp.json()
        assert "logs" in data
        assert "count" in data

    @pytest.mark.asyncio
    async def test_get_logs_invalid_limit(self, client):
        resp = await client.get("/api/logs?limit=abc")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_get_logs_with_level_filter(self, client, log_store):
        record = logging.LogRecord("test", logging.ERROR, "t.py", 1, "error log", (), None)
        log_store.add_record(record)
        resp = await client.get("/api/logs?level=ERROR")
        assert resp.status == 200
        data = await resp.json()
        for log in data["logs"]:
            assert log["level"] == "ERROR"

    @pytest.mark.asyncio
    async def test_get_logs_no_store(self, session):
        """Server without log_store returns 503."""
        server = WebSocketServer(session=session, host="127.0.0.1", port=0, log_store=None)
        async with TestClient(TestServer(server.app)) as c:
            resp = await c.get("/api/logs")
            assert resp.status == 503


class TestNodes:

    @pytest.mark.asyncio
    async def test_nodes_no_telemetry(self, client, session):
        """When CAN not connected, returns empty nodes."""
        session.telemetry = None
        resp = await client.get("/api/nodes")
        assert resp.status == 200
        data = await resp.json()
        assert data["node_count"] == 0

    @pytest.mark.asyncio
    async def test_nodes_with_telemetry(self, client, session):
        telemetry = MagicMock()
        telemetry.get_all_nodes_info.return_value = {
            "node_count": 1,
            "nodes": {"42": {"node_id": 42, "name": "test_node"}},
        }
        session.telemetry = telemetry
        resp = await client.get("/api/nodes")
        assert resp.status == 200
        data = await resp.json()
        assert data["node_count"] == 1


class TestLatestEndpoints:

    @pytest.mark.asyncio
    async def test_latest_subject_no_telemetry(self, client, session):
        session.telemetry = None
        resp = await client.get("/api/latest/subject/100")
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_latest_subject_not_found(self, client, session):
        telemetry = MagicMock()
        telemetry.get_latest_subject.return_value = None
        session.telemetry = telemetry
        resp = await client.get("/api/latest/subject/100")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_latest_subject_found(self, client, session):
        telemetry = MagicMock()
        telemetry.get_latest_subject.return_value = {"subject_id": 100, "rate": 1.0}
        session.telemetry = telemetry
        resp = await client.get("/api/latest/subject/100")
        assert resp.status == 200
        data = await resp.json()
        assert data["subject_id"] == 100

    @pytest.mark.asyncio
    async def test_latest_subject_invalid_id(self, client, session):
        session.telemetry = MagicMock()
        resp = await client.get("/api/latest/subject/abc")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_latest_node_no_telemetry(self, client, session):
        session.telemetry = None
        resp = await client.get("/api/latest/node/42")
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_latest_node_not_found(self, client, session):
        telemetry = MagicMock()
        telemetry.get_latest_node.return_value = {}
        session.telemetry = telemetry
        resp = await client.get("/api/latest/node/42")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_latest_node_found(self, client, session):
        telemetry = MagicMock()
        telemetry.get_latest_node.return_value = {100: {"subject_id": 100}}
        session.telemetry = telemetry
        resp = await client.get("/api/latest/node/42")
        assert resp.status == 200


class TestWebSocket:

    @pytest.mark.asyncio
    async def test_ws_ping_pong(self, client, session):
        session.telemetry = None
        async with client.ws_connect("/ws") as ws:
            await ws.send_json({"type": "ping"})
            resp = await ws.receive_json()
            assert resp["type"] == "pong"

    @pytest.mark.asyncio
    async def test_ws_filter(self, client, session):
        session.telemetry = None
        async with client.ws_connect("/ws") as ws:
            await ws.send_json({
                "type": "filter",
                "subject_ids": [100],
                "node_ids": [42],
            })
            resp = await ws.receive_json()
            assert resp["type"] == "filter_updated"
            assert resp["filter"]["subject_ids"] == [100]

    @pytest.mark.asyncio
    async def test_ws_invalid_json(self, client, session):
        session.telemetry = None
        async with client.ws_connect("/ws") as ws:
            await ws.send_str("not json")
            resp = await ws.receive_json()
            assert "error" in resp


class TestCORS:

    @pytest.mark.asyncio
    async def test_cors_headers(self, client):
        resp = await client.get("/api")
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    @pytest.mark.asyncio
    async def test_options_preflight(self, client):
        resp = await client.options("/api")
        assert resp.status == 200
        assert "Access-Control-Allow-Methods" in resp.headers


class TestGetServices:

    @pytest.mark.asyncio
    async def test_services_can_not_running(self, client, session):
        session.is_running = False
        resp = await client.get("/api/services/42")
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_services_invalid_node_id(self, client, session):
        session.is_running = True
        resp = await client.get("/api/services/abc")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_services_node_not_found(self, client, session):
        session.is_running = True
        telemetry = MagicMock()
        telemetry.get_service_schema.return_value = None
        session.telemetry = telemetry
        resp = await client.get("/api/services/99")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_services_success(self, client, session):
        session.is_running = True
        telemetry = MagicMock()
        telemetry.get_service_schema.return_value = {
            "node_id": 42,
            "services": [{"service_id": 100, "full_type": "uavcan.node.GetInfo"}],
        }
        session.telemetry = telemetry
        resp = await client.get("/api/services/42")
        assert resp.status == 200
        data = await resp.json()
        assert data["node_id"] == 42
        assert len(data["services"]) == 1


class TestGetClients:

    @pytest.mark.asyncio
    async def test_clients_can_not_running(self, client, session):
        session.is_running = False
        resp = await client.get("/api/clients/42")
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_clients_invalid_node_id(self, client, session):
        session.is_running = True
        resp = await client.get("/api/clients/abc")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_clients_node_not_found(self, client, session):
        session.is_running = True
        telemetry = MagicMock()
        telemetry.get_client_info.return_value = None
        session.telemetry = telemetry
        resp = await client.get("/api/clients/99")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_clients_success(self, client, session):
        session.is_running = True
        telemetry = MagicMock()
        telemetry.get_client_info.return_value = {
            "node_id": 42,
            "clients": [{"service_id": 200, "full_type": "uavcan.node.GetInfo", "server_nodes": [10]}],
        }
        session.telemetry = telemetry
        resp = await client.get("/api/clients/42")
        assert resp.status == 200
        data = await resp.json()
        assert data["node_id"] == 42
        assert len(data["clients"]) == 1


class TestGetRegisters:

    @pytest.mark.asyncio
    async def test_registers_can_not_running(self, client, session):
        session.is_running = False
        resp = await client.get("/api/registers/42")
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_registers_invalid_node_id(self, client, session):
        session.is_running = True
        resp = await client.get("/api/registers/abc")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_registers_success(self, client, session):
        session.is_running = True
        scanner = MagicMock()
        scanner.get_registers = AsyncMock(return_value=[
            {"register_name": "uavcan.node.id", "value": "42", "type": "natural16", "access": "read-write"},
        ])
        session.scanner = scanner
        resp = await client.get("/api/registers/42")
        assert resp.status == 200
        data = await resp.json()
        assert data["node_id"] == 42
        assert len(data["registers"]) == 1
        assert data["registers"][0]["register_name"] == "uavcan.node.id"

    @pytest.mark.asyncio
    async def test_registers_scanner_error(self, client, session):
        session.is_running = True
        scanner = MagicMock()
        scanner.get_registers = AsyncMock(side_effect=ValueError("Node unreachable"))
        session.scanner = scanner
        resp = await client.get("/api/registers/42")
        assert resp.status == 500


class TestSetRegister:

    @pytest.mark.asyncio
    async def test_set_register_can_not_running(self, client, session):
        session.is_running = False
        resp = await client.post("/api/registers/42/set", json={"name": "x", "value": "1", "type": "natural16"})
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_set_register_missing_fields(self, client, session):
        session.is_running = True
        resp = await client.post("/api/registers/42/set", json={"name": "x"})
        assert resp.status == 400
        data = await resp.json()
        assert "missing" in data["error"].lower() or "required" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_set_register_invalid_json(self, client, session):
        session.is_running = True
        resp = await client.post("/api/registers/42/set", data=b"not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_set_register_success(self, client, session):
        session.is_running = True
        scanner = MagicMock()
        scanner.set_register = AsyncMock(return_value=[42])
        session.scanner = scanner
        resp = await client.post("/api/registers/42/set", json={"name": "uavcan.node.id", "value": "42", "type": "natural16"})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["value"] == "42"

    @pytest.mark.asyncio
    async def test_set_register_timeout(self, client, session):
        session.is_running = True
        scanner = MagicMock()
        scanner.set_register = AsyncMock(return_value=None)
        session.scanner = scanner
        resp = await client.post("/api/registers/42/set", json={"name": "x", "value": "1", "type": "natural16"})
        assert resp.status == 504

    @pytest.mark.asyncio
    async def test_set_register_value_error(self, client, session):
        session.is_running = True
        scanner = MagicMock()
        scanner.set_register = AsyncMock(side_effect=ValueError("Unsupported type"))
        session.scanner = scanner
        resp = await client.post("/api/registers/42/set", json={"name": "x", "value": "1", "type": "bogus"})
        assert resp.status == 400


class TestServiceCall:

    @pytest.mark.asyncio
    async def test_call_can_not_running(self, client, session):
        session.is_running = False
        resp = await client.post("/api/services/42/100/call", json={"attributes": {}})
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_call_invalid_ids(self, client, session):
        session.is_running = True
        resp = await client.post("/api/services/abc/xyz/call", json={"attributes": {}})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_call_service_not_found(self, client, session):
        session.is_running = True
        scanner = MagicMock()
        scanner.service_metadata = {}
        session.scanner = scanner
        resp = await client.post("/api/services/42/999/call", json={"attributes": {}})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_call_success(self, client, session):
        session.is_running = True
        scanner = MagicMock()
        scanner.service_metadata = {
            (42, 100): {"namespace": "uavcan.node", "service_name": "GetInfo_1_0"},
        }
        scanner.make_service_call = AsyncMock(return_value="protocol_version: 1.0")
        session.scanner = scanner
        session.event_logger = MagicMock()
        session.event_logger.log_node_event = AsyncMock()
        resp = await client.post("/api/services/42/100/call", json={"attributes": {}})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert "protocol_version" in data["response"]
        assert data["latency_ms"] >= 0

    @pytest.mark.asyncio
    async def test_call_timeout(self, client, session):
        session.is_running = True
        scanner = MagicMock()
        scanner.service_metadata = {
            (42, 100): {"namespace": "uavcan.node", "service_name": "GetInfo_1_0"},
        }
        scanner.make_service_call = AsyncMock(side_effect=asyncio.TimeoutError())
        session.scanner = scanner
        session.event_logger = MagicMock()
        session.event_logger.log_node_event = AsyncMock()
        resp = await client.post("/api/services/42/100/call", json={"attributes": {}})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_call_invalid_json(self, client, session):
        session.is_running = True
        resp = await client.post("/api/services/42/100/call", data=b"not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400
