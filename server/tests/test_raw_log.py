"""Tests for raw CAN logs: the candump file, the session's start/stop, and /api/rawlogs."""

import datetime
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import can
import pytest
from aiohttp.test_utils import TestClient, TestServer

from raw_log import RawLog, list_logs, log_path, new_log_name


def _frames():
    return [
        can.Message(timestamp=1727500000.25, arbitration_id=0x107D552A, data=b"\x01\x02\xe0"),
        can.Message(timestamp=1727500000.5, arbitration_id=0x123, is_extended_id=False,
                    is_fd=True, bitrate_switch=True, data=bytes(range(12)), is_rx=False),
        can.Message(timestamp=1727500000.75, is_error_frame=True, arbitration_id=0x4, data=bytes(8)),
    ]


class TestRawLogFile:
    def test_frames_read_back_through_python_can(self, tmp_path):
        log = RawLog(tmp_path / "cynitor-20260928-120000.log", channel="can0")
        for msg in _frames():
            log.write(msg)
        log.close()
        back = list(can.LogReader(str(log.path)))
        assert [(m.arbitration_id, m.is_fd, m.bitrate_switch, m.is_error_frame, m.is_rx, m.timestamp)
                for m in back] == [
            (0x107D552A, False, False, False, True, 1727500000.25),
            (0x123, True, True, False, False, 1727500000.5),
            (0, False, False, True, True, 1727500000.75),
        ]
        assert bytes(back[1].data) == bytes(range(12))
        assert log.frames == 3

    def test_adapter_clock_is_replaced_by_wall_clock(self, tmp_path):
        # Some adapters stamp frames with seconds since power-up.
        log = RawLog(tmp_path / "cynitor-20260928-120000.log", channel="can0")
        before = time.time()
        log.write(can.Message(timestamp=123.4, arbitration_id=0x1, data=b"\x00"))
        log.close()
        [msg] = can.LogReader(str(log.path))
        assert msg.timestamp >= before

    def test_write_after_close_is_ignored(self, tmp_path):
        log = RawLog(tmp_path / "cynitor-20260928-120000.log", channel="can0")
        log.close()
        log.write(_frames()[0])
        assert log.frames == 0

    def test_write_failure_stops_the_log_and_says_why(self, tmp_path):
        log = RawLog(tmp_path / "cynitor-20260928-120000.log", channel="can0")
        log._writer.on_message_received = MagicMock(side_effect=OSError("No space left on device"))
        log.write(_frames()[0])
        assert log.error == "No space left on device" and log._writer is None
        assert log.status()["error"] == "No space left on device"


class TestNames:
    def test_new_name(self):
        assert new_log_name(datetime.datetime(2026, 9, 28, 14, 15, 0)) == "cynitor-20260928-141500.log"

    @pytest.mark.parametrize("name", ["../secret.log", "cynitor-20260928-141500.log/..", "x.log",
                                      "cynitor-20260928-141500.txt", "telemetry_events.db"])
    def test_only_log_names_reach_a_file(self, tmp_path, name):
        assert log_path(tmp_path, name) is None

    def test_list_newest_first_and_only_logs(self, tmp_path):
        for name in ("cynitor-20260927-090000.log", "cynitor-20260928-090000.log", "notes.txt"):
            (tmp_path / name).write_text("x")
        assert [log["name"] for log in list_logs(tmp_path)] == [
            "cynitor-20260928-090000.log", "cynitor-20260927-090000.log"]

    def test_missing_folder_lists_nothing(self, tmp_path):
        assert list_logs(tmp_path / "raw") == []


@pytest.fixture
def running_session(tmp_path):
    """A CANSession connected through a (fake) hub."""
    from main import CANSession
    session = CANSession(data_dir=tmp_path)
    session.scanner = MagicMock()  # is_running
    session.hub = SimpleNamespace(on_frame=None)
    session.can_interface = "pcan:PCAN_USBBUS1"
    return session


class TestSessionRawLog:
    def test_refused_when_not_connected(self, tmp_path):
        from main import CANSession
        with pytest.raises(RuntimeError, match="not connected"):
            CANSession(data_dir=tmp_path).start_raw_log()

    def test_hub_feeds_the_log_until_stopped(self, running_session):
        log = running_session.start_raw_log()
        assert running_session.hub.on_frame == log.write
        running_session.hub.on_frame(_frames()[0])
        assert running_session.stop_raw_log() is log
        assert running_session.hub.on_frame is None and running_session.raw_log is None
        assert log.frames == 1 and log.path.parent == running_session.raw_log_folder

    def test_one_log_at_a_time(self, running_session):
        running_session.start_raw_log()
        with pytest.raises(RuntimeError, match="already running"):
            running_session.start_raw_log()

    def test_stop_without_a_log(self, running_session):
        assert running_session.stop_raw_log() is None


@pytest.fixture
async def api(running_session):
    from websocket_server import WebSocketServer
    server = WebSocketServer(session=running_session, host="127.0.0.1", port=0)
    async with TestClient(TestServer(server.app)) as client:
        yield client, running_session


class TestRawLogApi:
    async def test_start_list_stop_download_delete(self, api):
        client, session = api
        started = await client.post("/api/rawlogs")
        assert started.status == 201
        name = (await started.json())["name"]
        session.hub.on_frame(_frames()[0])

        listing = await (await client.get("/api/rawlogs")).json()
        assert listing["active"]["name"] == name and listing["active"]["frames"] == 1
        assert (await client.delete(f"/api/rawlogs/{name}")).status == 409  # still running

        stopped = await client.post("/api/rawlogs/stop")
        assert stopped.status == 200 and (await stopped.json())["frames"] == 1
        assert (await client.post("/api/rawlogs/stop")).status == 409

        download = await client.get(f"/api/rawlogs/{name}")
        assert download.status == 200
        assert f'filename="{name}"' in download.headers["Content-Disposition"]
        assert "107D552A#0102E0" in await download.text()

        assert (await client.delete(f"/api/rawlogs/{name}")).status == 200
        assert (await (await client.get("/api/rawlogs")).json())["logs"] == []

    async def test_start_refused_when_not_connected(self, api):
        client, session = api
        session.scanner = None
        assert (await client.post("/api/rawlogs")).status == 409

    @pytest.mark.parametrize("name", ["cynitor-20260928-000000.log", "..%2Ftelemetry_events.db", "notes.txt"])
    async def test_unknown_or_foreign_names_are_404(self, api, name):
        client, _ = api
        assert (await client.get(f"/api/rawlogs/{name}")).status == 404
        assert (await client.delete(f"/api/rawlogs/{name}")).status == 404


class TestSocketcanTap:
    def test_frames_reach_the_callback_until_stopped(self, monkeypatch):
        import raw_log
        opened = {}
        real_bus = can.Bus  # raw_log.can is this very module

        def bus(interface, channel, fd):
            opened.update(interface=interface, channel=channel, fd=fd)
            return real_bus(interface="virtual", channel="tap-test")

        wire = real_bus(interface="virtual", channel="tap-test")
        monkeypatch.setattr(raw_log.can, "Bus", bus)
        seen = []
        tap = raw_log.SocketcanTap("vcan0", True, seen.append)
        try:
            wire.send(can.Message(arbitration_id=0x7, data=b"\x01"))
            deadline = time.monotonic() + 2
            while not seen and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            tap.stop()
            wire.shutdown()
        assert opened == {"interface": "socketcan", "channel": "vcan0", "fd": True}
        assert [m.arbitration_id for m in seen] == [0x7]
        assert not tap._thread.is_alive()
