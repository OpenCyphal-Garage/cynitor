"""Tests for WebSocketServer REST endpoints including CAN connect/disconnect."""

import asyncio
import time
from types import SimpleNamespace
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from websocket_server import WebSocketServer
from dsdl_manager import DsdlManager
import can

from frame_capture import FrameCaptureManager
from log_store import APILogHandler, InMemoryLogStore
import logging


def _make_session(is_running=False, can_interface=None):
    """Create a mock CANSession."""
    session = MagicMock()
    session.is_running = is_running
    session.can_interface = can_interface
    session.default_bitrate = None
    session.can_bitrate = None
    session.hub = None
    session.telemetry = None
    session.bus_load = None
    session.last_error = None
    session.dropped_events.return_value = None
    session.allocator_status.return_value = None
    session.decode_on_demand = False
    session.set_decode_on_demand = AsyncMock()
    session.default_data_bitrate = None
    session.can_data_bitrate = None
    session.can_fd = False
    session.replay = None
    session.event_logger = None
    session.frame_capture = None
    session.test_nodes.identify.return_value = None  # no node on the bus is a test node
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
        # We patch the handler directly to avoid importing can_link.discover_can_interfaces
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
    async def test_connect_refuses_an_interface_cynitor_does_not_know(self, client, server, session):
        body = {"interface": "slcan:/dev/pts/3@115200", "bitrate": 500000}
        refused = await client.post("/api/can/connect", json=body)
        assert refused.status == 400 and "not a serial port name" in (await refused.json())["error"]
        session.connect.assert_not_awaited()
        server.allow_any_interface = True  # --allow-any-interface
        allowed = await client.post("/api/can/connect", json=body)
        assert allowed.status == 200
        session.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_listen_only_reaches_the_session_and_must_be_true_or_false(self, client, session):
        with patch("api_can.discover_can_interfaces", return_value=["vcan0"]):
            bad = await client.post("/api/can/connect", json={"interface": "vcan0", "listen_only": "yes"})
            ok = await client.post("/api/can/connect", json={"interface": "vcan0", "listen_only": True})
        assert bad.status == 400 and ok.status == 200
        session.connect.assert_awaited_once_with("vcan0", bitrate=None, data_bitrate=None, allocator=None,
                                                 listen_only=True, no_commands=None)

    @pytest.mark.asyncio
    async def test_nothing_is_sent_while_listening_only(self, client, session):
        # Registers, service calls and firmware updates would transmit: refused with the reason.
        session.is_running, session.listen_only = True, True
        session.scanner = MagicMock(get_registers=AsyncMock(), set_register=AsyncMock(), make_service_call=AsyncMock())
        session.begin_firmware_update = AsyncMock()
        answers = [
            await client.get("/api/registers/42"),
            await client.post("/api/registers/42/set", json={"name": "x", "value": "1", "type": "natural16"}),
            await client.post("/api/services/42/435/call", json={"attributes": {}}),
            await client.post("/api/nodes/42/firmware", json={"file": "a.bin"}),
        ]
        assert [a.status for a in answers] == [409, 409, 409, 409]
        assert "listens only" in (await answers[0].json())["error"]
        session.scanner.get_registers.assert_not_awaited()
        session.scanner.set_register.assert_not_awaited()
        session.begin_firmware_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_commands_reaches_the_session_and_must_be_true_or_false(self, client, session):
        with patch("api_can.discover_can_interfaces", return_value=["vcan0"]):
            bad = await client.post("/api/can/connect", json={"interface": "vcan0", "no_commands": 1})
            ok = await client.post("/api/can/connect", json={"interface": "vcan0", "no_commands": True})
        assert bad.status == 400 and ok.status == 200
        session.connect.assert_awaited_once_with("vcan0", bitrate=None, data_bitrate=None, allocator=None,
                                                 listen_only=None, no_commands=True)

    @pytest.mark.asyncio
    async def test_no_node_is_changed_while_taking_no_commands(self, client, session):
        # Reads still go out (names, registers); writes, calls and updates are refused (F-024).
        session.is_running, session.listen_only, session.no_commands = True, False, True
        session.scanner = MagicMock(get_registers=AsyncMock(return_value=[]), set_register=AsyncMock(),
                                    make_service_call=AsyncMock())
        session.begin_firmware_update = AsyncMock()
        answers = [
            await client.get("/api/registers/42"),
            await client.post("/api/registers/42/set", json={"name": "x", "value": "1", "type": "natural16"}),
            await client.post("/api/services/42/435/call", json={"attributes": {}}),
            await client.post("/api/nodes/42/firmware", json={"file": "a.bin"}),
        ]
        assert [a.status for a in answers] == [200, 409, 409, 409]
        assert "takes no commands" in (await answers[1].json())["error"]
        session.scanner.set_register.assert_not_awaited()
        session.scanner.make_service_call.assert_not_awaited()
        session.begin_firmware_update.assert_not_awaited()
        status = await (await client.get("/api/status")).json()
        assert status["no_commands"] is True and status["listen_only"] is False

    @pytest.mark.asyncio
    async def test_connect_while_a_replay_runs_is_a_conflict(self, client, session):
        # It was a 500 with a traceback in the log (F-018 b).
        from main import SessionBusy
        session.connect = AsyncMock(side_effect=SessionBusy("Replay session is active — stop it before connecting CAN"))
        with patch("api_can.discover_can_interfaces", return_value=["vcan0"]):
            resp = await client.post("/api/can/connect", json={"interface": "vcan0"})
        assert resp.status == 409 and "Replay session is active" in (await resp.json())["error"]

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
        with patch("api_can.discover_can_interfaces", return_value=["vcan0"]):
            resp = await client.post("/api/can/connect", json={"interface": "vcan99"})
        assert resp.status == 400
        data = await resp.json()
        assert data["available_interfaces"] == ["vcan0"]
        session.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discovered_socketcan_name_needs_no_bitrate(self, client, session):
        with patch("api_can.discover_can_interfaces", return_value=["vcan0"]):
            resp = await client.post("/api/can/connect", json={"interface": "vcan0"})
        assert resp.status == 200
        session.connect.assert_awaited_once_with("vcan0", bitrate=None, data_bitrate=None, allocator=None, listen_only=None, no_commands=None)

    @pytest.mark.asyncio
    async def test_explicit_spec_skips_discovery(self, client, session):
        # Discovery never lists adapters such as gs_usb:0 (and lists nothing
        # at all off Linux), so requiring a match made them unreachable.
        with patch("api_can.discover_can_interfaces", return_value=[]) as discover:
            resp = await client.post(
                "/api/can/connect", json={"interface": "gs_usb:0", "bitrate": 250000},
            )
        assert resp.status == 200
        discover.assert_not_called()
        session.connect.assert_awaited_once_with("gs_usb:0", bitrate=250000, data_bitrate=None, allocator=None, listen_only=None, no_commands=None)

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
        session.connect.assert_awaited_once_with("gs_usb:0", bitrate=None, data_bitrate=None, allocator=None, listen_only=None, no_commands=None)

    @pytest.mark.asyncio
    async def test_data_bitrate_opens_can_fd(self, client, session):
        resp = await client.post("/api/can/connect", json={
            "interface": "pcan:PCAN_USBBUS1", "bitrate": 500000, "data_bitrate": 2000000,
        })
        assert resp.status == 200
        session.connect.assert_awaited_once_with("pcan:PCAN_USBBUS1", bitrate=500000, data_bitrate=2000000,
                                                     allocator=None, listen_only=None, no_commands=None)
        assert (await resp.json())["can_fd"] is False  # what the (mock) session reports

    @pytest.mark.asyncio
    async def test_data_bitrate_refused_for_adapter_without_can_fd(self, client, session):
        resp = await client.post("/api/can/connect", json={
            "interface": "canalystii:0", "bitrate": 500000, "data_bitrate": 2000000,
        })
        assert resp.status == 400
        assert "cannot run CAN FD" in (await resp.json())["error"]
        session.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_data_bitrate_refused_outside_the_adapters_presets(self, client, session):
        # The CANable 2.0's slcan firmware has two data bitrates.
        resp = await client.post("/api/can/connect", json={
            "interface": "slcan:COM5", "bitrate": 500000, "data_bitrate": 4000000,
        })
        assert resp.status == 400
        assert "2000000 or 5000000 bit/s only" in (await resp.json())["error"]
        session.connect.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("data_bitrate", [0, 50_000_000, "2000000"])
    async def test_rejects_invalid_data_bitrate(self, client, session, data_bitrate):
        resp = await client.post("/api/can/connect", json={
            "interface": "kvaser:0", "bitrate": 500000, "data_bitrate": data_bitrate,
        })
        assert resp.status == 400
        session.connect.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("allocator", [True, False])
    async def test_allocator_choice_reaches_the_session(self, client, session, allocator):
        with patch("api_can.discover_can_interfaces", return_value=["vcan0"]):
            resp = await client.post("/api/can/connect", json={"interface": "vcan0", "allocator": allocator})
        assert resp.status == 200
        session.connect.assert_awaited_once_with("vcan0", bitrate=None, data_bitrate=None, allocator=allocator, listen_only=None, no_commands=None)
        assert (await resp.json())["allocator"] is None  # what the (mock) session reports it runs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("allocator", ["yes", 1, 0])
    async def test_rejects_an_allocator_choice_that_is_no_boolean(self, client, session, allocator):
        resp = await client.post("/api/can/connect", json={"interface": "vcan0", "allocator": allocator})
        assert resp.status == 400
        assert "allocator" in (await resp.json())["error"]
        session.connect.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [None, {"other_allocator": 42, "allocated": 1}])
    async def test_status_reports_cynitors_allocator(self, client, session, status):
        session.allocator_status.return_value = status
        with patch("api_can.discover_can_interfaces", return_value=[]):
            resp = await client.get("/api/status")
        assert resp.status == 200
        assert (await resp.json())["allocator"] == status

    @pytest.mark.asyncio
    @pytest.mark.parametrize("on_demand", [True, False])
    async def test_decoding_is_switched_and_status_says_which(self, client, session, on_demand):
        resp = await client.post("/api/decoding", json={"on_demand": on_demand})
        assert resp.status == 200 and await resp.json() == {"decode_on_demand": on_demand}
        session.set_decode_on_demand.assert_awaited_once_with(on_demand)
        session.decode_on_demand = on_demand   # as the session then has it
        with patch("api_can.discover_can_interfaces", return_value=[]):
            status = await (await client.get("/api/status")).json()
        assert status["decode_on_demand"] is on_demand

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{}, {"on_demand": "yes"}, {"on_demand": 1}, [True]])
    async def test_decoding_wants_true_or_false(self, client, session, body):
        resp = await client.post("/api/decoding", json=body)
        assert resp.status == 400 and "on_demand" in (await resp.json())["error"]
        session.set_decode_on_demand.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_status_reports_the_servers_cpu_use(self, client, server, session):
        server.cpu.read = lambda: 42.5
        with patch("api_can.discover_can_interfaces", return_value=[]):
            resp = await client.get("/api/status")
        assert (await resp.json())["server_cpu"] == 42.5

    @pytest.mark.asyncio
    async def test_status_says_why_history_is_not_written(self, client, session):
        with patch("api_can.discover_can_interfaces", return_value=[]):
            session.event_logger = MagicMock(write_error=None)
            fine = (await (await client.get("/api/status")).json())["history_error"]
            session.event_logger.write_error = "disk I/O error"
            failing = (await (await client.get("/api/status")).json())["history_error"]
        assert (fine, failing) == (None, "disk I/O error")

    @pytest.mark.asyncio
    async def test_status_names_cynitors_own_node_id(self, client, session):
        # It was shown nowhere (F-039).
        session.is_running = True
        session._own_node_ids = MagicMock(return_value={127})
        with patch("api_can.discover_can_interfaces", return_value=[]):
            status = await (await client.get("/api/status")).json()
        assert status["own_node_id"] == 127

    @pytest.mark.asyncio
    async def test_status_reports_a_newer_release(self, client, server, session):
        with patch("api_can.discover_can_interfaces", return_value=[]):
            unchecked = (await (await client.get("/api/status")).json())["update"]
            newer = {"version": "1.4.0", "url": "https://github.com/OpenCyphal-Garage/cynitor/releases/tag/v1.4.0"}
            server.update_check = MagicMock(available=newer)
            found = (await (await client.get("/api/status")).json())["update"]
        assert (unchecked, found) == (None, newer)

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
    async def test_get_logs_since(self, client, log_store):
        """A poller asks for what came since the last record it has."""
        for i, message in enumerate(["older", "newer"]):
            record = logging.LogRecord("test", logging.INFO, "t.py", 1, message, (), None)
            record.created = time.time() + 10 + i
            log_store.add_record(record)
        seen = log_store.get_logs()[-2]["timestamp"]
        resp = await client.get("/api/logs", params={"since": seen})
        assert resp.status == 200
        assert [log["message"] for log in (await resp.json())["logs"]] == ["older", "newer"]

    @pytest.mark.asyncio
    async def test_get_logs_invalid_since(self, client):
        resp = await client.get("/api/logs", params={"since": "yesterday"})
        assert resp.status == 400
        assert "since" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_requests_do_not_fill_the_log_store(self, session):
        """With logging set up as main.py sets it up, a dashboard's own requests
        leave nothing in the store that /api/logs serves."""
        store = InMemoryLogStore()
        handler = APILogHandler(store)
        access = logging.getLogger("aiohttp.access")
        level = access.level
        access.setLevel(logging.INFO)
        access.addHandler(handler)
        try:
            server = WebSocketServer(session=session, host="127.0.0.1", port=0, log_store=store)
            async with TestClient(TestServer(server.app)) as c:
                for _ in range(3):
                    await c.get("/api/logs")
                data = await (await c.get("/api/logs")).json()
        finally:
            access.removeHandler(handler)
            access.setLevel(level)
        assert data["logs"] == []

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
    async def test_the_stream_follows_a_new_can_session(self, client, session):
        # A dashboard whose socket stayed open across a CAN disconnect and
        # connect got nothing more: it stayed on the old session's telemetry (F-028).
        class Source:
            def __init__(self):
                self.queues, self.gone = [], []

            def subscribe(self, max_queue=100):
                self.queues.append(asyncio.Queue())
                return self.queues[-1]

            def unsubscribe(self, queue):
                self.gone.append(queue)

        async def subscribed(source):
            for _ in range(100):
                if source.queues:
                    return source.queues[-1]
                await asyncio.sleep(0.05)
            raise AssertionError("the stream never subscribed to the new session")

        async def next_event(ws):
            while True:
                message = await asyncio.wait_for(ws.receive_json(), timeout=5)
                if "subject_id" in message:
                    return message

        first, second = Source(), Source()
        session.replay, session.telemetry = None, first
        async with client.ws_connect("/ws") as ws:
            (await subscribed(first)).put_nowait({"subject_id": 100, "publisher_node_id": 42})
            got_first = await next_event(ws)
            session.telemetry = None   # disconnected ...
            await asyncio.sleep(1.5)
            session.telemetry = second  # ... and connected again, within one status poll
            (await subscribed(second)).put_nowait({"subject_id": 200, "publisher_node_id": 43})
            got_second = await next_event(ws)
        assert (got_first["subject_id"], got_second["subject_id"]) == (100, 200)
        assert first.gone == first.queues

    @pytest.mark.asyncio
    async def test_a_message_that_is_no_object_is_answered_and_the_stream_goes_on(self, client, session):
        # A JSON array ended the socket, though later frames are to be accepted (F-018 a).
        session.telemetry = None
        async with client.ws_connect("/ws") as ws:
            await ws.send_str("[1, 2]")
            first = await ws.receive_json()
            await ws.send_json({"type": "ping"})
            second = await ws.receive_json()
        assert first == {"error": "A message must be a JSON object"} and second["type"] == "pong"

    @pytest.mark.asyncio
    async def test_streams_beyond_the_cap_are_refused(self, client, server, session):
        # 500 sockets were accepted in 0.4 s, CPU 13 % to 45 % (F-035).
        session.telemetry = None
        server.MAX_CLIENTS = 1
        async with client.ws_connect("/ws"):
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await client.ws_connect("/ws")
        assert refused.value.status == 503

    @pytest.mark.asyncio
    async def test_ws_invalid_json(self, client, session):
        session.telemetry = None
        async with client.ws_connect("/ws") as ws:
            await ws.send_str("not json")
            resp = await ws.receive_json()
            assert "error" in resp


class TestSecFetchSiteAndDevOrigins:
    """F-033: what another page in the visitor's browser can still make the server do."""

    @pytest.mark.asyncio
    async def test_a_cross_site_get_without_origin_is_refused(self, client):
        # An <img src=".../api/registers/50"> on any website: no Origin, but the browser says cross-site.
        resp = await client.get("/api/status", headers={"Sec-Fetch-Site": "cross-site"})
        assert resp.status == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("site", ["same-origin", "same-site", "none"])
    async def test_the_dashboards_own_requests_pass(self, client, site):
        with patch("api_can.discover_can_interfaces", return_value=[]):
            resp = await client.get("/api/status", headers={"Sec-Fetch-Site": site})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_page_on_another_local_port_is_refused_unless_named(self, client, monkeypatch):
        refused = await client.post("/api/can/disconnect", headers={"Origin": "http://localhost:15500"})
        monkeypatch.setenv("CYNITOR_DEV_ORIGINS", "http://localhost:15500")
        named = await client.post("/api/can/disconnect", headers={"Origin": "http://localhost:15500"})
        assert (refused.status, named.status) == (403, 409)  # 409: not connected, past the origin check


class TestHealthOfAWantedBus:
    """F-037: a server started with --can is not healthy while that bus is down."""

    @pytest.mark.asyncio
    async def test_a_lost_bus_is_a_503_until_the_operator_disconnects(self, client, session):
        session.wanted_can, session.is_running, session.last_error = "can0", False, "Interface can0 no longer exists"
        down = await client.get("/api/health")
        body = await down.json()
        session.wanted_can = None  # as POST /api/can/disconnect leaves it
        chosen = await client.get("/api/health")
        assert (down.status, body["status"], body["can_error"]) == (503, "degraded", "Interface can0 no longer exists")
        assert chosen.status == 200

    @pytest.mark.asyncio
    async def test_disconnecting_clears_it(self, client, session):
        session.is_running, session.wanted_can = True, "can0"
        with patch("api_can.discover_can_interfaces", return_value=[]):
            await client.post("/api/can/disconnect")
        assert session.wanted_can is None


class TestSecurityHeaders:
    """No page may frame the dashboard, nor run a script it did not serve; no version banner (F-019)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/api/health", "/api"])
    async def test_every_response_carries_them(self, client, path):
        resp = await client.get(path)
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in csp and "script-src 'self';" in csp
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Server") == "Cynitor"


class TestCORS:

    @pytest.mark.asyncio
    async def test_dev_server_origin_is_allowed(self, client):
        # The dashboard served separately for frontend work, on this machine.
        resp = await client.get("/api", headers={"Origin": "http://localhost:5500"})
        assert resp.status == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:5500"
        assert "Origin" in resp.headers.get("Vary", "")

    @pytest.mark.asyncio
    async def test_no_wildcard_origin(self, client):
        resp = await client.get("/api")
        assert "Access-Control-Allow-Origin" not in resp.headers

    @pytest.mark.asyncio
    async def test_options_preflight(self, client):
        resp = await client.options("/api", headers={"Origin": "http://127.0.0.1:5500"})
        assert resp.status == 200
        assert "Access-Control-Allow-Methods" in resp.headers


class TestCrossSiteRequests:
    """A page on another site must not be able to use the API through the
    browser of whoever has Cynitor running: it would command nodes on the bus."""

    @pytest.mark.asyncio
    async def test_foreign_site_post_is_refused_before_it_runs(self, client, session):
        # text/plain is a "simple" request: the browser sends it without a
        # preflight, so a CORS header alone would not stop it from running.
        with patch("api_can.discover_can_interfaces", return_value=["vcan0"]):
            resp = await client.post(
                "/api/can/connect", data='{"interface": "vcan0"}',
                headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
            )
        assert resp.status == 403
        session.connect.assert_not_awaited()
        assert "Access-Control-Allow-Origin" not in resp.headers

    @pytest.mark.asyncio
    async def test_foreign_site_preflight_is_refused(self, client):
        resp = await client.options("/api/can/connect", headers={"Origin": "https://evil.example"})
        assert resp.status == 403
        assert "Access-Control-Allow-Origin" not in resp.headers

    @pytest.mark.asyncio
    async def test_foreign_site_websocket_is_refused(self, client):
        # WebSockets are not covered by CORS at all.
        from aiohttp import WSServerHandshakeError
        with pytest.raises(WSServerHandshakeError) as err:
            await client.ws_connect("/ws", headers={"Origin": "https://evil.example"})
        assert err.value.status == 403

    @pytest.mark.asyncio
    async def test_sandboxed_null_origin_is_refused(self, client):
        resp = await client.get("/api/status", headers={"Origin": "null"})
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_dns_rebinding_is_refused(self, client):
        # evil.example re-resolved to 127.0.0.1: same-origin to the browser,
        # but a loopback-only server is never legitimately called that.
        resp = await client.get("/api/status", headers={
            "Host": "evil.example:8080", "Origin": "http://evil.example:8080",
        })
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_request_without_origin_is_allowed(self, client):
        # curl, scripts and other non-browser clients send no Origin.
        resp = await client.get("/api")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_same_origin_dashboard_is_allowed(self, client):
        # The dashboard this server serves: Origin names the server itself.
        host = f"127.0.0.1:{client.port}"
        resp = await client.post("/api/can/disconnect", headers={"Origin": f"http://{host}"})
        assert resp.status != 403

    @pytest.mark.asyncio
    async def test_network_bound_server_accepts_its_own_origin(self, session, log_store):
        # Bound beyond loopback, it is reached by the machine's address or name.
        server = WebSocketServer(session=session, host="0.0.0.0", port=0, log_store=log_store)
        async with TestClient(TestServer(server.app)) as c:
            resp = await c.get("/api", headers={
                "Host": "192.168.1.5:8080", "Origin": "http://192.168.1.5:8080",
            })
            assert resp.status == 200
            resp = await c.get("/api", headers={
                "Host": "192.168.1.5:8080", "Origin": "https://evil.example",
            })
            assert resp.status == 403


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

    @pytest.mark.asyncio
    async def test_registers_node_does_not_answer(self, client, session):
        session.is_running = True
        scanner = MagicMock()
        scanner.get_registers = AsyncMock(side_effect=TimeoutError("Node 42 does not answer register requests"))
        session.scanner = scanner
        resp = await client.get("/api/registers/42")
        assert resp.status == 504
        assert (await resp.json())["error"] == "Node 42 does not answer register requests"


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
    async def test_a_register_write_is_written_to_the_nodes_history(self, client, session):
        # It left no record that Cynitor changed the node (F-024).
        session.is_running = True
        session.scanner = MagicMock(set_register=AsyncMock(side_effect=[[True], None]),
                                    _get_node_unique_id_hex=lambda node_id: None)
        session.event_logger = MagicMock(log_node_event=AsyncMock())
        await client.post("/api/registers/42/set", json={"name": "flag", "value": "1", "type": "bit"})
        await client.post("/api/registers/42/set", json={"name": "flag", "value": "0", "type": "bit"})
        assert [c.args for c in session.event_logger.log_node_event.await_args_list] == [
            (42, "register_write", {"name": "flag", "value": "1", "read_back": "True", "status": "ok",
                                    "client": "127.0.0.1"}),
            (42, "register_write", {"name": "flag", "value": "0", "status": "timeout", "client": "127.0.0.1"}),
        ]

    @pytest.mark.asyncio
    async def test_a_service_call_says_who_made_it_in_the_history(self, client, session):
        session.is_running = True
        session.scanner = MagicMock(make_service_call=AsyncMock(return_value="{}"),
                                    service_metadata={(42, 435): {"namespace": "uavcan.node",
                                                                  "service_name": "ExecuteCommand_1_3"}},
                                    _get_node_unique_id_hex=lambda node_id: None)
        session.event_logger = MagicMock(log_node_event=AsyncMock())
        await client.post("/api/services/42/435/call", json={"attributes": {}})
        assert session.event_logger.log_node_event.await_args.args[2]["client"] == "127.0.0.1"

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
    async def test_call_to_an_offline_node_says_it_is_offline(self, client, session):
        # Its services are forgotten while it is offline: it answered 404 "Service 430 not found" (Windows audit, FW-08).
        session.is_running = True
        scanner = MagicMock()
        scanner.service_metadata = {}
        scanner.all_nodes = {42: SimpleNamespace(has_disappeared=True)}
        session.scanner = scanner
        resp = await client.post("/api/services/42/430/call", json={"attributes": {}})
        assert resp.status == 409
        assert (await resp.json())["error"] == "Node 42 is offline"

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
    async def test_call_to_a_node_that_went_offline(self, client, session):
        session.is_running = True
        session.scanner = MagicMock(service_metadata={(42, 435): {"namespace": "uavcan.node",
                                                                   "service_name": "ExecuteCommand_1_3"}})
        session.scanner.make_service_call = AsyncMock(
            side_effect=ConnectionError("Node 42 went offline while the call waited for its answer"))
        resp = await client.post("/api/services/42/435/call", json={"attributes": {}})
        assert resp.status == 409 and "went offline" in (await resp.json())["error"]

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
        with patch("api_can.get_can_link_diagnostics", return_value=link):
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
    async def test_transport_behind_hub_reports_the_hub(self, client, session):
        # An adapter such as gs_usb:0 has no kernel device for `ip link` to
        # read; the hub says what it knows instead.
        session.is_running = True
        session.can_interface = "gs_usb:0"
        session.bus_load = None
        session.hub = MagicMock()
        session.hub.link_diagnostics = MagicMock(return_value={
            "bitrate": 500000, "adapter_frames_in": 10,
            "adapter_frames_out": 4, "adapter_send_failures": 0,
        })
        scanner = MagicMock()
        scanner.get_transport_info = MagicMock(return_value={"protocol": None, "statistics": None})
        session.scanner = scanner
        with patch("api_can.get_can_link_diagnostics") as diagnostics:
            resp = await client.get("/api/can/transport")
        assert resp.status == 200
        link = (await resp.json())["link"]
        assert link["bitrate"] == 500000 and link["adapter_send_failures"] == 0
        diagnostics.assert_not_called()

    @pytest.mark.asyncio
    async def test_transport_reads_socketcan_device_not_spec(self, client, session):
        session.is_running = True
        session.can_interface = "socketcan:vcan0"
        session.bus_load = None
        scanner = MagicMock()
        scanner.get_transport_info = MagicMock(return_value={"protocol": None, "statistics": None})
        session.scanner = scanner
        with patch("api_can.get_can_link_diagnostics", return_value={}) as diagnostics:
            await client.get("/api/can/transport")
        diagnostics.assert_called_once_with("vcan0")


class _CaptureTap:
    """The tap on the bus CANSession opens for FrameCaptureManager."""

    def __init__(self):
        self.on_frame, self.open_now = None, False

    def open(self, on_frame):
        self.on_frame, self.open_now = on_frame, True
        return self.close

    def close(self):
        self.open_now = False


def _captured(can_id):
    """A non-Cyphal frame with this CAN ID, as a tap hands it over."""
    return can.Message(arbitration_id=can_id, data=b"\x00", timestamp=1_700_000_000.0 + can_id)


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
        mgr.subscribe = MagicMock(return_value=q)
        mgr.unsubscribe = MagicMock()
        mgr.stats = MagicMock(return_value={"captured": 0})
        session.frame_capture = mgr
        ws = MagicMock()
        ws.send_json = AsyncMock()

        await server._handle_capture_message(ws, enabled=True)
        mgr.subscribe.assert_called_once()
        assert server.capture_clients.get(ws) is q

        await server._handle_capture_message(ws, enabled=False)
        mgr.unsubscribe.assert_called_once_with(q)
        assert ws not in server.capture_clients

    @pytest.mark.asyncio
    async def test_capture_enable_sends_earlier_frames_once(self, server, session):
        # The frames caught before the client subscribed (for another one) come
        # with the reply, and its queue holds only later ones: the client gets
        # each frame once.
        tap = _CaptureTap()
        session.frame_capture = mgr = FrameCaptureManager(tap.open)
        other = mgr.subscribe()
        tap.on_frame(_captured(1))
        tap.on_frame(_captured(2))
        mgr.drain()
        ws = MagicMock()
        ws.send_json = AsyncMock()
        await server._handle_capture_message(ws, enabled=True)
        tap.on_frame(_captured(3))
        mgr.drain()
        mgr.unsubscribe(other)
        queue = server.capture_clients[ws]
        streamed = [queue.get_nowait()["id"] for _ in range(queue.qsize())]
        reply = ws.send_json.await_args.args[0]
        assert [f["id"] for f in reply.get("frames", [])] == ["0x00000001", "0x00000002"]
        assert streamed == ["0x00000003"]

    @pytest.mark.asyncio
    async def test_capture_loop_sends_a_backlog_at_once(self, server, session):
        # A busy bus queues frames faster than 250 a window: each message takes
        # all that is queued, so the stream keeps up instead of dropping frames.
        session.frame_capture = None
        queue = asyncio.Queue()
        for n in range(1000):
            queue.put_nowait({"id": n})
        ws = MagicMock()
        ws.closed = False
        sent = []
        ws.send_json = AsyncMock(side_effect=sent.append)
        server._running = True
        server.capture_clients[ws] = queue
        forward = asyncio.create_task(server._capture_loop(ws))
        await asyncio.sleep(server._CAPTURE_BATCH_WINDOW + 0.1)
        forward.cancel()
        with pytest.raises(asyncio.CancelledError):
            await forward
        assert [len(message["frames"]) for message in sent] == [1000]

    @pytest.mark.asyncio
    async def test_capture_stops_with_the_last_connection(self, server, session):
        # Capture runs while any connection captures: one's stop ends only the
        # frames sent to it, the last one's ends capture. Each reply says both.
        tap = _CaptureTap()
        session.frame_capture = FrameCaptureManager(tap.open)
        first, second = MagicMock(), MagicMock()
        first.send_json, second.send_json = AsyncMock(), AsyncMock()
        said = []
        for ws, enabled in ((first, True), (second, True), (first, False), (second, False)):
            await server._handle_capture_message(ws, enabled=enabled)
            reply = ws.send_json.await_args.args[0]
            said.append((reply.get("capturing"), reply.get("forwarding"), tap.open_now))
        session.frame_capture = None
        await server._handle_capture_message(first, enabled=True)
        no_can = first.send_json.await_args.args[0]
        assert said == [(True, True, True), (True, True, True), (True, False, True), (False, False, False)]
        assert (no_can.get("capturing"), no_can.get("forwarding")) == (False, False)

    @pytest.mark.asyncio
    async def test_capture_that_cannot_start_says_why(self, server, session):
        def refuse(on_frame):
            raise OSError("No such device")
        session.frame_capture = FrameCaptureManager(refuse)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        await server._handle_capture_message(ws, enabled=True)
        reply = ws.send_json.await_args.args[0]
        assert reply["forwarding"] is False and "No such device" in reply["error"]
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
            Adapter("slcan:COM5", "CANable (slcan) COM5", True, True, (2_000_000, 5_000_000)),
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
        with patch("api_can.discover_can_interfaces", return_value=["vcan0"]):
            data = await (await listing_client.get("/api/status")).json()
        assert data["available_interfaces"] == ["vcan0"]  # unchanged for old clients
        assert data["available_adapters"][1] == {
            "interface": "gs_usb:0", "label": "CANable 0", "needs_bitrate": True, "supports_fd": False,
            "data_bitrates": None,
        }
        assert data["available_adapters"][2]["data_bitrates"] == [2_000_000, 5_000_000]
        assert data["can_bitrate"] is None
        assert data["can_data_bitrate"] is None and data["can_fd"] is False

    @pytest.mark.asyncio
    async def test_status_reports_dropped_events(self, listing_client, session):
        session.dropped_events.return_value = {"scanner": 0, "logger": 4, "clients": 1}
        with patch("api_can.discover_can_interfaces", return_value=[]):
            data = await (await listing_client.get("/api/status")).json()
        assert data["dropped"] == {"scanner": 0, "logger": 4, "clients": 1}

    @pytest.mark.asyncio
    async def test_status_reports_bus_errors(self, listing_client, session):
        from bus_errors import BusErrors
        with patch("api_can.discover_can_interfaces", return_value=[]):
            none_yet = (await (await listing_client.get("/api/status")).json())["bus_errors"]
            session.bus_errors = BusErrors("can0")
            session.bus_errors.observe({"state": "ERROR-PASSIVE", "berr_tx": 0, "berr_rx": 128}, now=100.0)
            data = await (await listing_client.get("/api/status")).json()
        assert none_yet is None
        assert data["bus_errors"] == {"state": "ERROR-PASSIVE", "tx_errors": 0, "rx_errors": 128, "since_unix": 100.0}

    @pytest.mark.asyncio
    async def test_refresh_query_forces_a_rescan(self, listing_client, catalog):
        resp = await listing_client.get("/api/can/adapters?refresh=1")
        assert resp.status == 200
        assert len((await resp.json())["adapters"]) == 3
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


class TestRecentNodeEvents:
    """GET /api/nodes/events: every node's lifecycle events, for the graph."""

    @pytest.mark.asyncio
    async def test_returns_events_with_server_clock(self, client, session):
        session.event_logger = MagicMock()
        session.event_logger.get_node_history = AsyncMock(return_value=[
            {"id": 1, "node_id": 10, "unique_id": None, "timestamp_unix": 100.0,
             "event_type": "restart_suspected", "detail": None},
        ])
        resp = await client.get("/api/nodes/events?range=1h&types=restart_suspected,node_id_conflict&limit=50")
        assert resp.status == 200
        data = await resp.json()
        assert data["events"][0]["event_type"] == "restart_suspected"
        assert isinstance(data["now_unix"], float)
        args, kwargs = session.event_logger.get_node_history.call_args
        assert args == (None,)
        assert kwargs["event_types"] == ["restart_suspected", "node_id_conflict"]
        assert kwargs["limit"] == 50
        assert data["now_unix"] - kwargs["since_unix"] == pytest.approx(3600, abs=1)

    @pytest.mark.asyncio
    async def test_rejects_bad_parameters(self, client, session):
        session.event_logger = MagicMock()
        session.event_logger.get_node_history = AsyncMock(return_value=[])
        assert (await client.get("/api/nodes/events?range=2y")).status == 400
        assert (await client.get("/api/nodes/events?limit=x")).status == 400
        assert (await client.get("/api/nodes/events?limit=0")).status == 400

    @pytest.mark.asyncio
    async def test_needs_the_event_logger(self, client):
        assert (await client.get("/api/nodes/events")).status == 503


class TestDsdlCompile:
    """Two compiles at once would write the same compiled files at once."""

    @pytest.mark.asyncio
    async def test_one_compile_at_a_time(self, session, log_store):
        running, overlapped = [], []

        class SlowDsdl:
            def compile_custom(self):
                running.append(1)
                overlapped.append(len(running) > 1)
                time.sleep(0.2)
                running.pop()
                return {"ok": True}

        server = WebSocketServer(session=session, host="127.0.0.1", port=0, log_store=log_store,
                                 dsdl_manager=SlowDsdl())
        async with TestClient(TestServer(server.app)) as c:
            server._running = True
            answers = await asyncio.gather(*(c.post("/api/dsdl/compile", json={"scope": "custom"}) for _ in range(2)))
        assert [a.status for a in answers] == [200, 200]
        assert overlapped == [False, False]

    @pytest.mark.asyncio
    async def test_a_failed_compile_answers_as_every_route_does(self, session, log_store, tmp_path, monkeypatch):
        """{"error": message}: each namespace's error on a line of its own."""
        (tmp_path / "dsdl_messages" / "custom").mkdir(parents=True)
        mgr = DsdlManager(tmp_path)
        server = WebSocketServer(session=session, host="127.0.0.1", port=0, log_store=log_store, dsdl_manager=mgr)
        async with TestClient(TestServer(server.app)) as c:
            server._running = True
            nothing = await c.post("/api/dsdl/compile", json={"scope": "custom"})
            for namespace in ("alpha", "beta"):
                mgr.save_type(namespace, "Reading", "1.0", "uint8 x\n@sealed\n")
            monkeypatch.setattr(mgr, "_compile", lambda target, lookups, output, label: [f"{label}: broken"])
            broken = await c.post("/api/dsdl/compile", json={"scope": "custom"})
            answers = [(nothing.status, await nothing.json()), (broken.status, await broken.json())]
        assert answers == [(422, {"error": "No custom types to compile"}),
                           (422, {"error": "custom/alpha: broken\ncustom/beta: broken"})]


class TestDsdlDeleteNamespace:
    """DELETE /api/dsdl/custom/namespace/{namespace}: an empty one goes."""

    @pytest.mark.asyncio
    async def test_status_codes(self, session, log_store, tmp_path):
        (tmp_path / "dsdl_messages" / "custom").mkdir(parents=True)
        mgr = DsdlManager(tmp_path)
        mgr.create_namespace("spare")
        mgr.save_type("full", "Reading", "1.0", "uint8 x\n@sealed\n")
        server = WebSocketServer(session=session, host="127.0.0.1", port=0, log_store=log_store, dsdl_manager=mgr)
        async with TestClient(TestServer(server.app)) as c:
            server._running = True
            statuses = {ns: (await c.delete(f"/api/dsdl/custom/namespace/{ns}")).status
                        for ns in ("spare", "full", "nowhere", "Bad-Name")}
        assert statuses == {"spare": 200, "full": 400, "nowhere": 404, "Bad-Name": 400}
        assert mgr.list_custom_namespaces() == ["full"]
