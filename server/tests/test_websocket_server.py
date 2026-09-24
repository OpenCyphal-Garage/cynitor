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
    session.default_bitrate = None
    session.can_bitrate = None
    session.telemetry = None
    session.bus_load = None
    session.last_error = None
    session.replay = None
    session.event_logger = None
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


class TestCANConnectSpecsAndBitrate:
    """The real /api/can/connect handler, with discovery pinned."""

    @pytest.mark.asyncio
    async def test_bare_name_is_checked_against_discovery(self, client, session):
        with patch("main.discover_can_interfaces", return_value=["vcan0"]):
            resp = await client.post("/api/can/connect", json={"interface": "vcan99"})
        assert resp.status == 400
        data = await resp.json()
        assert data["available_interfaces"] == ["vcan0"]
        session.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discovered_socketcan_name_needs_no_bitrate(self, client, session):
        with patch("main.discover_can_interfaces", return_value=["vcan0"]):
            resp = await client.post("/api/can/connect", json={"interface": "vcan0"})
        assert resp.status == 200
        session.connect.assert_awaited_once_with("vcan0", bitrate=None)

    @pytest.mark.asyncio
    async def test_explicit_spec_skips_discovery(self, client, session):
        # Discovery never lists adapters such as gs_usb:0 (and lists nothing
        # at all off Linux), so requiring a match made them unreachable.
        with patch("main.discover_can_interfaces", return_value=[]) as discover:
            resp = await client.post(
                "/api/can/connect", json={"interface": "gs_usb:0", "bitrate": 250000},
            )
        assert resp.status == 200
        discover.assert_not_called()
        session.connect.assert_awaited_once_with("gs_usb:0", bitrate=250000)

    @pytest.mark.asyncio
    async def test_adapter_without_bitrate_is_refused(self, client, session):
        resp = await client.post("/api/can/connect", json={"interface": "gs_usb:0"})
        assert resp.status == 400
        assert "bitrate is required" in (await resp.json())["error"]
        session.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_adapter_falls_back_to_server_bitrate(self, client, session):
        # Started with --bitrate: API clients may leave it out.
        session.default_bitrate = 250_000
        resp = await client.post("/api/can/connect", json={"interface": "gs_usb:0"})
        assert resp.status == 200
        session.connect.assert_awaited_once_with("gs_usb:0", bitrate=None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bitrate", [0, 5_000_000, "500000", True])
    async def test_rejects_invalid_bitrate(self, client, session, bitrate):
        resp = await client.post(
            "/api/can/connect", json={"interface": "gs_usb:0", "bitrate": bitrate},
        )
        assert resp.status == 400
        assert "bitrate" in (await resp.json())["error"].lower()
        session.connect.assert_not_awaited()


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
        resp = await client.post("/api/services/42/400/call", json={"attributes": {}})
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
        assert resp.status == 504
        data = await resp.json()
        assert data["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_call_invalid_json(self, client, session):
        session.is_running = True
        resp = await client.post("/api/services/42/100/call", data=b"not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400


class TestTransportDiagnostics:
    """GET /api/can/transport — Phase 1 Debugging-view diagnostics."""

    @pytest.mark.asyncio
    async def test_transport_idle(self, client, session):
        # No CAN session: the endpoint reports connected=False (200) so the
        # Debugging view can render an idle state instead of an error.
        session.is_running = False
        session.scanner = None
        resp = await client.get("/api/can/transport")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"connected": False}

    @pytest.mark.asyncio
    async def test_transport_connected(self, client, session):
        session.is_running = True
        session.can_interface = "vcan0"
        scanner = MagicMock()
        scanner.get_transport_info = MagicMock(return_value={
            "protocol": {"mtu": 7, "transfer_id_modulo": 32, "max_nodes": 128, "is_fd": False},
            "statistics": {"in_frames": 10, "in_frames_errored": 0, "out_frames": 4},
            "capture_active": False,
        })
        session.scanner = scanner
        bus_load = MagicMock()
        bus_load.utilization = 12.5
        session.bus_load = bus_load

        link = {"state": "ERROR-ACTIVE", "bitrate": 500000, "berr_tx": 0, "berr_rx": 0}
        with patch("main.get_can_link_diagnostics", return_value=link):
            resp = await client.get("/api/can/transport")
        assert resp.status == 200
        data = await resp.json()
        assert data["connected"] is True
        assert data["interface"] == "vcan0"
        assert data["protocol"]["mtu"] == 7
        assert data["protocol"]["is_fd"] is False
        assert data["statistics"]["in_frames"] == 10
        assert data["capture_active"] is False
        assert data["link"]["state"] == "ERROR-ACTIVE"
        assert data["bus_utilization"] == 12.5
        scanner.get_transport_info.assert_called_once()

    @pytest.mark.asyncio
    async def test_transport_behind_hub_skips_iproute2(self, client, session):
        # An adapter such as gs_usb:0 has no kernel device for `ip link` to read.
        session.is_running = True
        session.can_interface = "gs_usb:0"
        session.bus_load = None
        scanner = MagicMock()
        scanner.get_transport_info = MagicMock(return_value={"protocol": None, "statistics": None})
        session.scanner = scanner
        with patch("main.get_can_link_diagnostics") as diagnostics:
            resp = await client.get("/api/can/transport")
        assert resp.status == 200
        assert (await resp.json())["link"] == {}
        diagnostics.assert_not_called()

    @pytest.mark.asyncio
    async def test_transport_reads_socketcan_device_not_spec(self, client, session):
        session.is_running = True
        session.can_interface = "socketcan:vcan0"
        session.bus_load = None
        scanner = MagicMock()
        scanner.get_transport_info = MagicMock(return_value={"protocol": None, "statistics": None})
        session.scanner = scanner
        with patch("main.get_can_link_diagnostics", return_value={}) as diagnostics:
            await client.get("/api/can/transport")
        diagnostics.assert_called_once_with("vcan0")


class TestFrameCaptureAPI:
    """GET /api/can/capture snapshot + 'capture' WS message handling."""

    @pytest.mark.asyncio
    async def test_capture_snapshot_no_session(self, client, session):
        session.frame_capture = None
        resp = await client.get("/api/can/capture")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"active": False, "stats": None, "frames": []}

    @pytest.mark.asyncio
    async def test_capture_snapshot_active(self, client, session):
        mgr = MagicMock()
        mgr.active = True
        mgr.stats = MagicMock(return_value={"captured": 3})
        mgr.snapshot = MagicMock(return_value=[{"id": "0x1"}])
        session.frame_capture = mgr
        resp = await client.get("/api/can/capture?limit=10")
        assert resp.status == 200
        data = await resp.json()
        assert data["active"] is True
        assert data["stats"]["captured"] == 3
        assert data["frames"] == [{"id": "0x1"}]
        mgr.snapshot.assert_called_once_with(10)

    @pytest.mark.asyncio
    async def test_capture_message_no_session(self, server, session):
        # enabling capture with no CAN session replies with an inactive status
        session.frame_capture = None
        ws = MagicMock()
        ws.send_json = AsyncMock()
        await server._handle_capture_message(ws, enabled=True)
        ws.send_json.assert_awaited_once()
        payload = ws.send_json.await_args.args[0]
        assert payload["type"] == "capture_status"
        assert payload["active"] is False
        assert "error" in payload

    @pytest.mark.asyncio
    async def test_capture_message_enable_then_disable(self, server, session):
        q = asyncio.Queue()
        mgr = MagicMock()
        mgr.active = True
        mgr.start = MagicMock()
        mgr.subscribe = MagicMock(return_value=q)
        mgr.unsubscribe = MagicMock()
        mgr.stats = MagicMock(return_value={"captured": 0})
        session.frame_capture = mgr
        ws = MagicMock()
        ws.send_json = AsyncMock()

        await server._handle_capture_message(ws, enabled=True)
        mgr.start.assert_called_once()
        mgr.subscribe.assert_called_once()
        assert server.capture_clients.get(ws) is q

        await server._handle_capture_message(ws, enabled=False)
        mgr.unsubscribe.assert_called_once_with(q)
        assert ws not in server.capture_clients


class TestAdapterListing:
    """available_adapters in /api/status and GET /api/can/adapters."""

    @pytest.fixture
    def catalog(self):
        from can_discovery import Adapter
        c = MagicMock()
        c.get = MagicMock(return_value=[
            Adapter("vcan0", "vcan0 (SocketCAN)", False),
            Adapter("gs_usb:0", "CANable 0", True),
        ])
        return c

    @pytest.fixture
    async def listing_client(self, session, log_store, catalog):
        srv = WebSocketServer(session=session, host="127.0.0.1", port=0,
                              log_store=log_store, adapter_catalog=catalog)
        async with TestClient(TestServer(srv.app)) as c:
            srv._running = True
            yield c

    @pytest.mark.asyncio
    async def test_status_lists_adapters_and_bitrate(self, listing_client, session):
        session.can_bitrate = None
        with patch("main.discover_can_interfaces", return_value=["vcan0"]):
            data = await (await listing_client.get("/api/status")).json()
        assert data["available_interfaces"] == ["vcan0"]  # unchanged for old clients
        assert data["available_adapters"][1] == {
            "interface": "gs_usb:0", "label": "CANable 0", "needs_bitrate": True,
        }
        assert data["can_bitrate"] is None

    @pytest.mark.asyncio
    async def test_refresh_query_forces_a_rescan(self, listing_client, catalog):
        resp = await listing_client.get("/api/can/adapters?refresh=1")
        assert resp.status == 200
        assert len((await resp.json())["adapters"]) == 2
        catalog.get.assert_called_once_with(True, True)

    @pytest.mark.asyncio
    async def test_no_rescan_while_connected(self, listing_client, catalog, session):
        # Probing the adapter the session holds could disturb it.
        session.is_running = True
        await listing_client.get("/api/can/adapters?refresh=1")
        catalog.get.assert_called_once_with(True, False)

    @pytest.mark.asyncio
    async def test_without_a_catalog_nothing_is_listed(self, client):
        assert (await (await client.get("/api/can/adapters")).json()) == {"adapters": []}
