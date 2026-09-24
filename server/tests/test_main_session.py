"""Tests for CANSession.rescan_registrations — the post-compile rescan hook."""

import pytest
from unittest.mock import MagicMock


@pytest.fixture
def session():
    """A CANSession with scanner mocked to a populated state."""
    from main import CANSession
    s = CANSession()
    scanner = MagicMock()
    scanner.service_metadata = {
        (42, 100): {"namespace": "uavcan.node", "service_name": "GetInfo_1_0"},
        (42, 81): {"namespace": "dontpanic", "service_name": "SumService_1_0", "unavailable": True},
        (43, 81): {"namespace": "dontpanic", "service_name": "SumService_1_0", "unavailable": True},
    }
    s.scanner = scanner
    s.registered_nodes.update({42, 43, 77})
    return s


class TestRescanRegistrations:

    def test_clears_registered_nodes_set(self, session):
        assert session.registered_nodes == {42, 43, 77}
        session.rescan_registrations()
        assert session.registered_nodes == set()

    def test_drops_unavailable_service_metadata(self, session):
        session.rescan_registrations()
        meta = session.scanner.service_metadata
        assert (42, 81) not in meta, "unavailable entry should be evicted"
        assert (43, 81) not in meta, "unavailable entry should be evicted"
        assert (42, 100) in meta, "healthy entry should be preserved"

    def test_no_op_when_not_running(self):
        from main import CANSession
        s = CANSession()
        assert s.scanner is None
        s.registered_nodes.update({1, 2, 3})
        s.rescan_registrations()
        # Should not raise and should not touch the set (no scanner = no work to do)
        assert s.registered_nodes == {1, 2, 3}

    def test_no_op_when_scanner_missing(self):
        from main import CANSession
        s = CANSession()
        s.scanner = None
        s.registered_nodes.update({1})
        s.rescan_registrations()
        assert s.registered_nodes == {1}


class FakeHub:
    """Stands in for CANHub so that no test opens a real adapter."""

    def __init__(self, spec, bitrate):
        self.spec = spec
        self.bitrate = bitrate
        self.local_spec = "virtual:fake-hub"
        self.error = None
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def hubs(monkeypatch):
    import main
    created = []

    def make_hub(spec, bitrate):
        created.append(FakeHub(spec, bitrate))
        return created[-1]

    monkeypatch.setattr(main, "CANHub", make_hub)
    monkeypatch.setattr(main, "pick_free_node_id", lambda *args, **kwargs: 42)
    monkeypatch.setattr(main, "ensure_libusb_on_path", lambda: None)
    monkeypatch.setenv("UAVCAN__NODE__ID", "7")  # skip picking unless a test clears it
    return created


@pytest.fixture
def captured(monkeypatch, hubs):
    import main
    calls = []

    def fake_prepare_runtime(*args):
        calls.append(args)
        # Stop before any component tries to open a real bus.
        raise RuntimeError("stop after prepare_runtime")

    monkeypatch.setattr(main, "prepare_runtime", fake_prepare_runtime)
    return calls


class TestConnectBitrate:
    """CANSession.connect hands one validated bitrate to prepare_runtime."""

    async def test_uses_session_default_when_none_given(self, captured, hubs):
        from main import CANSession
        s = CANSession(default_bitrate=250_000)
        with pytest.raises(RuntimeError, match="stop"):
            await s.connect("gs_usb:0")
        assert (hubs[0].spec, hubs[0].bitrate) == ("gs_usb:0", 250_000)
        assert captured == [("virtual:fake-hub", False, 250_000, False)]

    async def test_explicit_bitrate_wins(self, captured, hubs):
        from main import CANSession
        s = CANSession(default_bitrate=250_000)
        with pytest.raises(RuntimeError, match="stop"):
            await s.connect("gs_usb:0", bitrate=125_000)
        assert hubs[0].bitrate == 125_000
        assert captured == [("virtual:fake-hub", False, 125_000, False)]

    async def test_invalid_bitrate_fails_before_prepare_runtime(self, captured, hubs):
        from main import CANSession
        with pytest.raises(ValueError):
            await CANSession().connect("gs_usb:0", bitrate=0)
        assert captured == [] and hubs == []

    async def test_adapter_without_any_bitrate_fails_before_prepare_runtime(self, captured, hubs):
        from main import CANSession
        with pytest.raises(ValueError, match="bitrate is required"):
            await CANSession().connect("gs_usb:0")
        assert captured == [] and hubs == []

    async def test_socketcan_needs_no_bitrate(self, captured, hubs):
        from main import CANSession
        with pytest.raises(RuntimeError, match="stop"):
            await CANSession().connect("vcan0")
        assert captured == [("vcan0", False, None)]
        assert hubs == []  # SocketCAN keeps its direct path

    async def test_socketcan_ignores_a_given_bitrate(self, captured, hubs):
        # The kernel's setting applies; passing one on would only mislead.
        from main import CANSession
        with pytest.raises(RuntimeError, match="stop"):
            await CANSession(default_bitrate=250_000).connect("vcan0")
        assert captured == [("vcan0", False, None)]

    def test_invalid_default_is_rejected_up_front(self):
        from main import CANSession
        with pytest.raises(ValueError):
            CANSession(default_bitrate=0)


class TestConnectThroughHub:
    """Non-SocketCAN adapters are opened once, by the hub, and shared."""

    async def test_hub_is_started_and_stopped_when_setup_fails(self, captured, hubs):
        from main import CANSession
        s = CANSession()
        with pytest.raises(RuntimeError, match="stop"):
            await s.connect("gs_usb:0", bitrate=500_000)
        assert hubs[0].started and hubs[0].stopped
        assert s.hub is None

    async def test_node_id_is_picked_on_the_hub_channel(self, captured, hubs, monkeypatch):
        # `yakut accommodate` runs in a child process, which cannot see the
        # hub's in-process channel, so the session picks the node-ID itself.
        import os
        import main
        picks = []

        def fake_pick(local_spec, exclude):
            picks.append((local_spec, exclude))
            return 42

        monkeypatch.setattr(main, "pick_free_node_id", fake_pick)
        monkeypatch.delenv("UAVCAN__NODE__ID")
        with pytest.raises(RuntimeError, match="stop"):
            await main.CANSession().connect("gs_usb:0", bitrate=500_000)
        assert picks == [("virtual:fake-hub", frozenset({1}))]  # never the allocator's
        assert os.environ["UAVCAN__NODE__ID"] == "42"

    async def test_preset_node_id_is_kept(self, captured, hubs, monkeypatch):
        import os
        import main
        monkeypatch.setattr(main, "pick_free_node_id", lambda *a: pytest.fail("should not pick"))
        with pytest.raises(RuntimeError, match="stop"):
            await main.CANSession().connect("gs_usb:0", bitrate=500_000)
        assert os.environ["UAVCAN__NODE__ID"] == "7"


class TestSessionHealth:
    """What the register loop's watchdog asks every few seconds."""

    async def test_hub_reports_its_own_error(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "_check_can_health", lambda iface: pytest.fail("not SocketCAN"))
        s = main.CANSession()
        s.can_interface = "gs_usb:0"
        s.hub = FakeHub("gs_usb:0", 500_000)
        assert await main._session_health_error(s) is None
        s.hub.error = "gs_usb:0: receive failed: device gone"
        assert await main._session_health_error(s) == "gs_usb:0: receive failed: device gone"

    async def test_socketcan_is_checked_by_device_name(self, monkeypatch):
        # sysfs has /sys/class/net/vcan0, not .../socketcan:vcan0; checking the
        # spec reported a healthy interface as gone.
        import main
        checked = []
        monkeypatch.setattr(main, "_check_can_health", lambda iface: checked.append(iface))
        s = main.CANSession()
        s.can_interface = "socketcan:vcan0"
        assert await main._session_health_error(s) is None
        assert checked == ["vcan0"]
