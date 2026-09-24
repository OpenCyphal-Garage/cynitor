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


class TestConnectBitrate:
    """CANSession.connect hands one validated bitrate to prepare_runtime."""

    @pytest.fixture
    def captured(self, monkeypatch):
        import main
        calls = []

        def fake_prepare_runtime(*args):
            calls.append(args)
            # Stop before any component tries to open a real bus.
            raise RuntimeError("stop after prepare_runtime")

        monkeypatch.setattr(main, "prepare_runtime", fake_prepare_runtime)
        return calls

    async def test_uses_session_default_when_none_given(self, captured):
        from main import CANSession
        s = CANSession(default_bitrate=250_000)
        with pytest.raises(RuntimeError, match="stop"):
            await s.connect("gs_usb:0")
        assert captured == [("gs_usb:0", False, 250_000)]

    async def test_explicit_bitrate_wins(self, captured):
        from main import CANSession
        s = CANSession(default_bitrate=250_000)
        with pytest.raises(RuntimeError, match="stop"):
            await s.connect("gs_usb:0", bitrate=125_000)
        assert captured == [("gs_usb:0", False, 125_000)]

    async def test_invalid_bitrate_fails_before_prepare_runtime(self, captured):
        from main import CANSession
        with pytest.raises(ValueError):
            await CANSession().connect("gs_usb:0", bitrate=0)
        assert captured == []

    async def test_adapter_without_any_bitrate_fails_before_prepare_runtime(self, captured):
        from main import CANSession
        with pytest.raises(ValueError, match="bitrate is required"):
            await CANSession().connect("gs_usb:0")
        assert captured == []

    async def test_socketcan_needs_no_bitrate(self, captured):
        from main import CANSession
        with pytest.raises(RuntimeError, match="stop"):
            await CANSession().connect("vcan0")
        assert captured == [("vcan0", False, None)]

    async def test_socketcan_ignores_a_given_bitrate(self, captured):
        # The kernel's setting applies; passing one on would only mislead.
        from main import CANSession
        with pytest.raises(RuntimeError, match="stop"):
            await CANSession(default_bitrate=250_000).connect("vcan0")
        assert captured == [("vcan0", False, None)]

    def test_invalid_default_is_rejected_up_front(self):
        from main import CANSession
        with pytest.raises(ValueError):
            CANSession(default_bitrate=0)
