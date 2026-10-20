"""Tests for raw CAN logs: the candump file, the session's start/stop, and /api/rawlogs."""

import asyncio
import datetime
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
        log.write(can.Message(timestamp=123.4, arbitration_id=0x1, data=b"\x00"))
        log.close()
        [msg] = can.LogReader(str(log.path))
        # The file keeps microseconds, so compare loosely, not against an exact instant.
        assert abs(msg.timestamp - time.time()) < 60

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

    def test_a_close_that_fails_too_does_not_escape(self, tmp_path):
        # On a full disk, closing flushes and fails as well; that raised into the
        # thread that handed the frame over, and behind the hub ended it (F-027).
        log = RawLog(tmp_path / "cynitor-20260928-120000.log", channel="can0")
        log._writer.on_message_received = MagicMock(side_effect=OSError("No space left on device"))
        log._writer.stop = MagicMock(side_effect=OSError("No space left on device (flush)"))
        log.write(_frames()[0])
        log.write(_frames()[1])
        assert log.error == "No space left on device" and log._writer is None

    def test_stops_at_the_size_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(RawLog, "MAX_BYTES", 200)
        monkeypatch.setattr(RawLog, "CHECK_EVERY_FRAMES", 1)
        log = RawLog(tmp_path / "cynitor-20260928-120000.log", channel="can0")
        for _ in range(20):
            log.write(_frames()[0])
        log.close()
        assert log.error and "Size limit" in log.error
        assert log.frames < 20

    def test_stops_when_the_disk_runs_low(self, tmp_path, monkeypatch):
        import raw_log
        monkeypatch.setattr(RawLog, "CHECK_EVERY_FRAMES", 2)
        monkeypatch.setattr(raw_log.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * 2**20))
        log = RawLog(tmp_path / "cynitor-20260928-120000.log", channel="can0")
        for _ in range(5):
            log.write(_frames()[0])
        assert log.frames == 2 and log._writer is None
        assert "MiB free" in log.error


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


class _Hub:
    """The CAN hub's listeners, without an adapter."""

    def __init__(self):
        self.listeners = []

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        self.listeners.remove(listener)

    def tell(self, msg):
        for listener in self.listeners:
            listener(msg)


@pytest.fixture
def running_session(tmp_path):
    """A CANSession connected through a (fake) hub."""
    from main import CANSession
    session = CANSession(data_dir=tmp_path)
    session.scanner = MagicMock()  # is_running
    session.hub = _Hub()
    session.can_interface = "pcan:PCAN_USBBUS1"
    return session


class TestSessionRawLog:
    def test_refused_when_not_connected(self, tmp_path):
        from main import CANSession
        with pytest.raises(RuntimeError, match="not connected"):
            CANSession(data_dir=tmp_path).raw_logs.start()

    def test_hub_feeds_the_log_until_stopped(self, running_session):
        log = running_session.raw_logs.start()
        assert running_session.hub.listeners == [log.write]
        running_session.hub.tell(_frames()[0])
        assert running_session.raw_logs.stop() is log
        assert running_session.hub.listeners == [] and running_session.raw_logs.active is None
        assert log.frames == 1 and log.path.parent == running_session.raw_logs.folder

    def test_one_log_at_a_time(self, running_session):
        running_session.raw_logs.start()
        with pytest.raises(RuntimeError, match="already running"):
            running_session.raw_logs.start()

    def test_stop_without_a_log(self, running_session):
        assert running_session.raw_logs.stop() is None

    def test_refused_when_the_disk_is_low(self, running_session, monkeypatch):
        import raw_log
        monkeypatch.setattr(raw_log.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
        with pytest.raises(RuntimeError, match="MiB free"):
            running_session.raw_logs.start()
        assert running_session.raw_logs.active is None


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
        session.hub.tell(_frames()[0])

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

    async def test_a_slow_disk_does_not_hold_up_the_server(self, api, monkeypatch):
        # On a busy USB stick a stat took seconds, and every request waited them out (Windows audit, FW-06).
        import api_rawlogs
        client, _ = api

        def slow_listing(folder):
            time.sleep(1.0)
            return []

        monkeypatch.setattr(api_rawlogs, "list_logs", slow_listing)
        t0 = time.monotonic()  # before: a blocked loop would hold this test up too
        listing = asyncio.ensure_future(client.get("/api/rawlogs"))
        await asyncio.sleep(0.1)
        health = await client.get("/api/health")
        assert health.status in (200, 503) and time.monotonic() - t0 < 0.7
        assert (await listing).status == 200

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

        def bus(interface, channel, fd, can_filters=None):
            opened.update(interface=interface, channel=channel, fd=fd, can_filters=can_filters)
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
        assert opened == {"interface": "socketcan", "channel": "vcan0", "fd": True, "can_filters": None}
        assert [m.arbitration_id for m in seen] == [0x7]
        assert not tap._thread.is_alive()


# ── Playback ──

from raw_log import LogPlayer, log_has_fd_frames, read_sidecar, sidecar_path, write_sidecar


def _log_with(tmp_path, frames):
    log = RawLog(tmp_path / "cynitor-20260928-120000.log", channel="can0")
    for msg in frames:
        log.write(msg)
    log.close()
    return log.path


def _heartbeat(node_id, t, is_rx=True):
    return can.Message(timestamp=1790000000 + t, arbitration_id=0x107D5500 | node_id, data=b"\x01", is_rx=is_rx)


def _drain(bus, limit=2.0):
    got, deadline = [], time.monotonic() + limit
    while time.monotonic() < deadline:
        try:
            msg = bus.recv(timeout=0.05)
        except can.CanOperationError as exc:
            return got, str(exc)
        if msg is not None:
            got.append(msg)
    return got, None


class TestLogPlayer:
    def test_plays_the_bus_without_the_cynitor_that_logged_it(self, tmp_path):
        path = _log_with(tmp_path, [_heartbeat(50, 0), _heartbeat(46, 0.1, is_rx=False), _heartbeat(50, 0.2, is_rx=False)])
        bus = LogPlayer(path, speed=0, skip_node_id=46)
        bus.play()
        got, end = _drain(bus)
        assert [m.arbitration_id & 0x7F for m in got] == [50, 50]
        assert all(m.is_rx for m in got)  # logged on this host or not, it comes from the "bus" now
        assert end == "end of the raw log"

    def test_silent_until_play(self, tmp_path):
        bus = LogPlayer(_log_with(tmp_path, [_heartbeat(50, 0)]), speed=0)
        assert bus.recv(timeout=0.2) is None
        bus.play()
        assert bus.recv(timeout=0.2).arbitration_id & 0x7F == 50

    def test_speed_scales_the_logged_pace(self, tmp_path):
        bus = LogPlayer(_log_with(tmp_path, [_heartbeat(50, 0), _heartbeat(50, 1.0)]), speed=10)
        bus.play()
        start = time.monotonic()
        got, _ = _drain(bus)
        assert len(got) == 2 and 0.07 < time.monotonic() - start < 0.5  # 1 s of log at 10x

    def test_sending_is_ignored(self, tmp_path):
        LogPlayer(_log_with(tmp_path, [_heartbeat(50, 0)])).send(_heartbeat(1, 0))


class TestSidecar:
    def test_round_trip(self, tmp_path):
        path = _log_with(tmp_path, [_heartbeat(50, 0)])
        write_sidecar(path, {"names": {"50": "x"}})
        assert sidecar_path(path).name == "cynitor-20260928-120000.types.json"
        assert read_sidecar(path) == {"names": {"50": "x"}}

    def test_missing_sidecar_reads_empty(self, tmp_path):
        assert read_sidecar(_log_with(tmp_path, [_heartbeat(50, 0)])) == {}

    @pytest.mark.parametrize("written, kept", [
        # A "publishers" that is no object left a session that never played (F-048).
        ({"publishers": ["oops"], "names": {"50": "x"}}, {"publishers": {}, "names": {"50": "x"}}),
        ({"publishers": {"50": {"1620": 7, "1621": "T_1_0"}, "x": {}}}, {"publishers": {"50": {"1621": "T_1_0"}}}),
        ({"bitrate": "fast", "fd": "yes", "own_node_id": -3, "data_bitrate": 2000000}, {"data_bitrate": 2000000}),
        (["a", "list"], {}),
    ])
    def test_what_does_not_fit_is_left_out(self, tmp_path, written, kept):
        path = _log_with(tmp_path, [_heartbeat(50, 0)])
        sidecar_path(path).write_text(json.dumps(written))
        assert read_sidecar(path) == kept

    def test_fd_frames_detected(self, tmp_path):
        classic = _log_with(tmp_path, [_heartbeat(50, 0)])
        assert not log_has_fd_frames(classic)
        classic.unlink()
        fd = _log_with(tmp_path, [can.Message(timestamp=1790000000, arbitration_id=0x123, is_fd=True, data=bytes(12))])
        assert log_has_fd_frames(fd)


class TestSessionPlayback:
    def test_stopping_a_log_keeps_what_the_session_knew(self, running_session):
        import numpy
        scanner = running_session.scanner
        scanner.active_publishers = {1620: {50}, 7509: {50}}
        scanner.subject_types = {1620: "uavcan.si.sample.temperature.Scalar_1_0"}
        scanner.node_service_types = {50: {384: "uavcan.register.Access_1_0"}}
        node = SimpleNamespace(info_response=SimpleNamespace(name=numpy.frombuffer(b"demo.sensor", numpy.uint8)))
        scanner.all_nodes = {50: node, 51: SimpleNamespace()}
        log = running_session.raw_logs.start()
        running_session.raw_logs.stop()
        info = read_sidecar(log.path)
        assert info["publishers"] == {"50": {"1620": "uavcan.si.sample.temperature.Scalar_1_0"}}
        assert info["servers"] == {"50": {"384": "uavcan.register.Access_1_0"}}
        assert info["names"] == {"50": "demo.sensor"}

    async def test_unknown_log_is_refused(self, tmp_path):
        from main import CANSession
        with pytest.raises(FileNotFoundError):
            await CANSession(data_dir=tmp_path).play_raw_log("cynitor-20260928-000000.log")

    async def test_plays_through_the_hub_with_the_sidecar(self, tmp_path, monkeypatch):
        from main import CANSession
        session = CANSession(data_dir=tmp_path)
        folder = session.raw_logs.folder
        folder.mkdir()
        path = _log_with(folder, [_heartbeat(50, 0)])
        write_sidecar(path, {"own_node_id": 46, "bitrate": 250000, "fd": False,
                             "publishers": {"50": {"1620": "ns.T_1_0"}}})
        calls = {}

        async def connect(iface, **kwargs):
            calls.update(iface=iface, **kwargs)
            session.scanner = MagicMock(add_subscriptions=AsyncMock())
        session.connect = connect
        played = []
        monkeypatch.setattr(LogPlayer, "play", lambda self: played.append(self))
        await session.play_raw_log(path.name, speed=5)
        assert calls["iface"] == f"rawlog:{path.name}" and calls["bitrate"] == 250000
        assert calls["data_bitrate"] is None and calls["offline"]["own_node_id"] == 46
        player = calls["open_bus"]("spec", 1, None)
        assert isinstance(player, LogPlayer) and played == [player]
        session.scanner.add_subscriptions.assert_awaited_once_with(50, {1620: "ns.T_1_0"})


class TestPlayApi:
    async def test_play_errors(self, api):
        client, session = api
        assert (await client.post("/api/rawlogs/cynitor-20260928-000000.log/play", json={})).status == 404
        log = session.raw_logs.start()
        session.raw_logs.stop()
        url = f"/api/rawlogs/{log.path.name}/play"
        assert (await client.post(url, json={"speed": -1})).status == 400
        assert (await client.post(url, json={"speed": "fast"})).status == 400
        assert (await client.post(url, json={})).status == 409  # the fixture session is connected

    async def test_play_starts_the_session(self, api):
        client, session = api
        log = session.raw_logs.start()
        session.raw_logs.stop()
        session.scanner = None  # disconnected
        session.play_raw_log = AsyncMock()
        resp = await client.post(f"/api/rawlogs/{log.path.name}/play", json={"speed": 0})
        assert resp.status == 200
        session.play_raw_log.assert_awaited_once_with(log.path.name, 0.0)

    async def test_delete_removes_the_sidecar(self, api):
        client, session = api
        log = session.raw_logs.start()
        session.raw_logs.stop()
        assert sidecar_path(log.path).exists()
        assert (await client.delete(f"/api/rawlogs/{log.path.name}")).status == 200
        assert not sidecar_path(log.path).exists()
