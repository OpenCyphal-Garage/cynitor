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
