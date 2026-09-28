"""Firmware updates: the file server's reads, the session's update command, the REST API."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

import main
from firmware import FirmwareServer, firmware_path, list_firmware

IMAGE = bytes(range(256)) * 3 + b"tail"  # three full chunks and a short one


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "firmware"
    path.mkdir()
    (path / "motor-1.2.app.bin").write_bytes(IMAGE)
    return path


class TestNames:
    def test_plain_names_only(self, tmp_path):
        assert firmware_path(tmp_path, "motor-1.2.app.bin") == tmp_path / "motor-1.2.app.bin"
        for bad in ("", "../secret", "a/b.bin", ".hidden", "x" * 200, "a b.bin"):
            assert firmware_path(tmp_path, bad) is None

    def test_list(self, folder):
        (folder / ".motor.bin.part").write_bytes(b"x")  # an upload in progress
        assert [f["name"] for f in list_firmware(folder)] == ["motor-1.2.app.bin"]
        assert list_firmware(folder / "missing") == []


class TestRead:
    def test_chunks_until_a_short_one(self, folder):
        server = FirmwareServer(folder)
        server.begin(42, "motor-1.2.app.bin", len(IMAGE))
        received = b""
        while True:
            chunk = server.read(42, "motor-1.2.app.bin", len(received))
            received += chunk
            if len(chunk) < FirmwareServer.CHUNK:
                break
            assert server.updates[42]["state"] == "reading"
        assert received == IMAGE
        assert server.updates[42]["state"] == "transferred"
        assert server.updates[42]["read"] == len(IMAGE)

    def test_leading_slash_is_ignored(self, folder):
        assert FirmwareServer(folder).read(42, "/motor-1.2.app.bin", 0) == IMAGE[:256]

    def test_unknown_or_outside_files(self, folder):
        server = FirmwareServer(folder)
        (folder.parent / "secret.bin").write_bytes(b"no")
        for name in ("nothing.bin", "../secret.bin", "/../secret.bin"):
            assert server.read(42, name, 0) is None

    def test_other_nodes_do_not_move_the_progress(self, folder):
        server = FirmwareServer(folder)
        server.begin(42, "motor-1.2.app.bin", len(IMAGE))
        server.read(43, "motor-1.2.app.bin", 512)
        assert server.updates[42]["state"] == "requested" and server.updates[42]["read"] == 0


@pytest.fixture
def session(tmp_path, folder):
    s = main.CANSession(data_dir=tmp_path)
    s.scanner = MagicMock()  # is_running
    s.firmware = FirmwareServer(s.firmware_folder)
    return s


class TestBeginUpdate:
    async def test_accepted(self, session):
        with patch.object(main, "send_update_command", AsyncMock(return_value=0)) as send:
            update = await session.begin_firmware_update(42, "motor-1.2.app.bin")
        send.assert_awaited_once_with(session.scanner.node, 42, "motor-1.2.app.bin")
        assert update["state"] == "requested" and update["bytes"] == len(IMAGE)
        assert session.firmware.updates[42] is update

    @pytest.mark.parametrize("status, error, match", [
        (None, TimeoutError, "did not answer"),
        (5, RuntimeError, "bad state"),
        (99, RuntimeError, "status 99"),
    ])
    async def test_refused_or_silent(self, session, status, error, match):
        with patch.object(main, "send_update_command", AsyncMock(return_value=status)):
            with pytest.raises(error, match=match):
                await session.begin_firmware_update(42, "motor-1.2.app.bin")
        assert 42 not in session.firmware.updates

    async def test_unknown_file(self, session):
        with pytest.raises(FileNotFoundError):
            await session.begin_firmware_update(42, "nothing.bin")

    async def test_nothing_can_be_sent(self, session):
        session.firmware = None  # a raw log plays, or no node-ID
        with pytest.raises(RuntimeError, match="cannot send"):
            await session.begin_firmware_update(42, "motor-1.2.app.bin")


@pytest.fixture
async def api(session):
    from websocket_server import WebSocketServer
    server = WebSocketServer(session=session, host="127.0.0.1", port=0)
    async with TestClient(TestServer(server.app)) as client:
        yield client, session


class TestApi:
    async def test_upload_list_delete(self, api):
        client, session = api
        resp = await client.post("/api/firmware?name=new-2.0.bin", data=b"\x01\x02\x03")
        assert resp.status == 201 and (await resp.json()) == {"name": "new-2.0.bin", "bytes": 3}
        body = await (await client.get("/api/firmware")).json()
        assert [f["name"] for f in body["files"]] == ["motor-1.2.app.bin", "new-2.0.bin"]
        assert body["updates"] == {}
        assert (await client.delete("/api/firmware/new-2.0.bin")).status == 200
        assert (await client.delete("/api/firmware/new-2.0.bin")).status == 404

    async def test_upload_refusals(self, api):
        client, session = api
        assert (await client.post("/api/firmware?name=../x.bin", data=b"x")).status == 400
        assert (await client.post("/api/firmware?name=empty.bin", data=b"")).status == 400
        with patch("websocket_server.MAX_FIRMWARE_BYTES", 4):
            assert (await client.post("/api/firmware?name=big.bin", data=b"12345")).status == 413
        assert not list(session.firmware_folder.glob("*big*")) and not list(session.firmware_folder.glob(".*"))

    async def test_update(self, api):
        client, session = api
        session.begin_firmware_update = AsyncMock(return_value={"state": "requested"})
        resp = await client.post("/api/nodes/42/firmware", json={"file": "motor-1.2.app.bin"})
        assert resp.status == 200
        session.begin_firmware_update.assert_awaited_once_with(42, "motor-1.2.app.bin")

    @pytest.mark.parametrize("error, status", [
        (FileNotFoundError("x"), 404), (TimeoutError("x"), 504), (RuntimeError("x"), 409),
    ])
    async def test_update_errors(self, api, error, status):
        client, session = api
        session.begin_firmware_update = AsyncMock(side_effect=error)
        assert (await client.post("/api/nodes/42/firmware", json={"file": "a.bin"})).status == status

    async def test_update_bad_requests(self, api):
        client, _ = api
        assert (await client.post("/api/nodes/42/firmware", json={})).status == 400
        assert (await client.post("/api/nodes/200/firmware", json={"file": "a.bin"})).status == 400

    async def test_progress_is_listed(self, api):
        client, session = api
        session.firmware.begin(42, "motor-1.2.app.bin", len(IMAGE))
        session.scanner.node_mode = lambda node_id: "SOFTWARE_UPDATE"
        body = await (await client.get("/api/firmware")).json()
        assert body["updates"]["42"]["state"] == "requested"
        assert body["updates"]["42"]["mode"] == "SOFTWARE_UPDATE"


class TestNodeMode:
    def test_from_the_latest_heartbeat(self):
        from scanner_node import ScannerNode
        scanner = ScannerNode.__new__(ScannerNode)
        scanner._prev_mode = {42: 3, 43: 9}
        assert scanner.node_mode(42) == "SOFTWARE_UPDATE"
        assert scanner.node_mode(43) == "9"
        assert scanner.node_mode(44) is None
