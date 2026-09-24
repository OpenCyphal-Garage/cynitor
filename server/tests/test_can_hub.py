"""Tests for CANHub, which shares one adapter handle among Cynitor's components.

The "adapter" is a python-can virtual channel standing in for the physical
bus, so these run on any OS without hardware. A second bus on that channel
plays another node on the wire.
"""

import itertools
import random
import threading
import time
from unittest.mock import patch

import can
import pytest

import can_hub
from can_hub import CANHub, _heartbeat_source, close_adapter, open_adapter, pick_free_node_id

_wire_numbers = itertools.count(1)
_TIMEOUT = 2.0


def _frame(can_id=0x107D552A, data=b"\x01\x02\xe0"):
    return can.Message(arbitration_id=can_id, is_extended_id=True, data=data)


def _recv_matching(bus, can_id, timeout=_TIMEOUT):
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        msg = bus.recv(timeout=remaining)
        if msg is not None and msg.arbitration_id == can_id:
            return msg
    return None


@pytest.fixture
def wire():
    """Channel name of the simulated physical bus."""
    return f"test-wire-{next(_wire_numbers)}"


@pytest.fixture
def hub(wire):
    h = CANHub(
        "virtual:" + wire, 500_000,
        open_bus=lambda spec, bitrate: can.Bus(interface="virtual", channel=wire),
    )
    h.start()
    yield h
    h.stop()


@pytest.fixture
def other_node(wire):
    bus = can.Bus(interface="virtual", channel=wire)
    yield bus
    bus.shutdown()


def _component(hub):
    parts = hub.local_spec.split(":", 1)
    return can.Bus(interface=parts[0], channel=parts[1])


class TestForwarding:
    def test_frames_on_the_wire_reach_every_component(self, hub, other_node):
        a, b = _component(hub), _component(hub)
        try:
            other_node.send(_frame(0x100))
            assert _recv_matching(a, 0x100) is not None
            assert _recv_matching(b, 0x100) is not None
        finally:
            a.shutdown()
            b.shutdown()

    def test_component_frames_reach_the_wire_and_the_other_components(self, hub, other_node):
        # Like two programs on one SocketCAN interface: each sees the other.
        a, b = _component(hub), _component(hub)
        try:
            a.send(_frame(0x200))
            assert _recv_matching(other_node, 0x200) is not None
            assert _recv_matching(b, 0x200) is not None
        finally:
            a.shutdown()
            b.shutdown()

    def test_a_component_does_not_get_its_own_frames_back(self, hub, other_node):
        a = _component(hub)
        try:
            a.send(_frame(0x300))
            assert _recv_matching(other_node, 0x300) is not None
            assert _recv_matching(a, 0x300, timeout=0.3) is None
        finally:
            a.shutdown()

    def test_counts_frames_both_ways(self, hub, other_node):
        a = _component(hub)
        try:
            other_node.send(_frame(0x400))
            assert _recv_matching(a, 0x400) is not None
            a.send(_frame(0x401))
            assert _recv_matching(other_node, 0x401) is not None
            assert hub.frames_from_bus >= 1
            assert hub.frames_to_bus >= 1
        finally:
            a.shutdown()

    def test_local_spec_is_a_virtual_channel(self, hub):
        assert hub.local_spec == f"virtual:{hub.channel}"


class StubAdapter(can.BusABC):
    """An adapter whose received frames and send behaviour a test controls."""

    def __init__(self, incoming=(), send_error=None, recv_error=None):
        super().__init__(channel="stub")
        self.incoming = list(incoming)
        self.sent = []
        self.send_error = send_error
        self.recv_error = recv_error
        self.closed = False

    def _recv_internal(self, timeout):
        if self.recv_error is not None:
            raise self.recv_error
        if self.incoming:
            return self.incoming.pop(0), False
        time.sleep(timeout or 0)
        return None, False

    def send(self, msg, timeout=None):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(msg)

    def shutdown(self):
        self.closed = True
        super().shutdown()


def _start_with(adapter, listen=False):
    """Start a hub on ``adapter``; with ``listen``, also a component that is on
    the channel before the first frame is forwarded."""
    h = CANHub("stub:0", 500_000, open_bus=lambda spec, bitrate: adapter)
    component = _component(h) if listen else None
    h.start()
    return (h, component) if listen else h


class TestFiltering:
    def test_echoed_own_frames_are_dropped(self):
        # gs_usb reports each frame it sent back to the host with is_rx False.
        echo = _frame(0x500)
        echo.is_rx = False
        real = _frame(0x501)
        h, a = _start_with(StubAdapter(incoming=[echo, real]), listen=True)
        try:
            assert _recv_matching(a, 0x501) is not None
            assert _recv_matching(a, 0x500, timeout=0.3) is None
        finally:
            a.shutdown()
            h.stop()

    def test_error_frames_are_dropped(self):
        error = can.Message(arbitration_id=0x600, is_error_frame=True)
        real = _frame(0x601)
        h, a = _start_with(StubAdapter(incoming=[error, real]), listen=True)
        try:
            assert _recv_matching(a, 0x601) is not None
            assert _recv_matching(a, 0x600, timeout=0.3) is None
        finally:
            a.shutdown()
            h.stop()


class TestFailures:
    def test_receive_failure_is_fatal_and_reported(self):
        h = _start_with(StubAdapter(recv_error=can.CanOperationError("device gone")))
        try:
            deadline = time.monotonic() + _TIMEOUT
            while h.error is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert h.error is not None and "device gone" in h.error
        finally:
            h.stop()

    def test_send_failure_is_counted_not_fatal(self):
        # A bus with nobody to acknowledge fills the adapter's transmit queue;
        # that must not tear the session down.
        adapter = StubAdapter(send_error=can.CanOperationError("tx queue full"))
        h = _start_with(adapter)
        a = _component(h)
        try:
            a.send(_frame(0x700))
            deadline = time.monotonic() + _TIMEOUT
            while h.send_failures == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert h.send_failures == 1
            assert h.error is None
        finally:
            a.shutdown()
            h.stop()

    def test_stop_closes_the_adapter_and_is_idempotent(self):
        adapter = StubAdapter()
        h = _start_with(adapter)
        h.stop()
        h.stop()
        assert adapter.closed

    def test_stop_takes_the_hub_off_its_channel(self, hub):
        # A bus left on the channel would keep collecting every frame.
        import can.interfaces.virtual as virtual
        hub.stop()
        assert hub.channel not in virtual.channels

    def test_adapter_open_failure_propagates(self):
        def refuse(spec, bitrate):
            raise can.CanInitializationError("Access denied")

        h = CANHub("gs_usb:0", 500_000, open_bus=refuse)
        with pytest.raises(can.CanInitializationError):
            h.start()


class TestCloseAdapter:
    def test_releases_the_gs_usb_device(self):
        # python-can leaves the USB interface claimed until garbage collection;
        # on Windows the next open then fails.
        pytest.importorskip("usb.util")  # pyusb is a Windows-only requirement

        class FakeGsUsbBus:
            def __init__(self):
                self.gs_usb = type("GsUsb", (), {"gs_usb": object()})()
                self.shut_down = False

            def shutdown(self):
                self.shut_down = True

        bus = FakeGsUsbBus()
        with patch("usb.util.dispose_resources") as dispose:
            close_adapter(bus)
        assert bus.shut_down
        dispose.assert_called_once_with(bus.gs_usb.gs_usb)

    def test_other_adapters_are_just_shut_down(self):
        # No pyusb needed: without a gs_usb device there is nothing to release.
        adapter = StubAdapter()
        close_adapter(adapter)
        assert adapter.closed


class TestOpenAdapter:
    @pytest.fixture
    def opened(self):
        with patch.object(can_hub.can, "ThreadSafeBus") as bus:
            yield bus

    def test_gs_usb_index_is_an_integer(self, opened):
        open_adapter("gs_usb:0", 500_000)
        opened.assert_called_once_with(interface="gs_usb", channel="0", bitrate=500_000, index=0)

    def test_slcan_carries_the_serial_baud_rate(self, opened):
        open_adapter("slcan:COM5@115200", 250_000)
        opened.assert_called_once_with(
            interface="slcan", channel="COM5", bitrate=250_000, tty_baudrate=115200,
        )

    def test_numbered_channel_is_an_integer(self, opened):
        open_adapter("kvaser:0", 500_000)
        opened.assert_called_once_with(interface="kvaser", channel=0, bitrate=500_000)

    def test_named_channel_stays_a_string(self, opened):
        open_adapter("pcan:PCAN_USBBUS1", 500_000)
        opened.assert_called_once_with(interface="pcan", channel="PCAN_USBBUS1", bitrate=500_000)

    @pytest.mark.parametrize("spec", ["vcan0", ":0"])
    def test_rejects_specs_without_an_interface(self, spec):
        with pytest.raises(ValueError):
            open_adapter(spec, 500_000)


def _heartbeat(node_id, mode=0, anonymous=False, service=False, subject=7509, tail=0xE0):
    can_id = (4 << 26) | (3 << 21) | (subject << 8) | node_id
    if anonymous:
        can_id |= 1 << 24
    if service:
        can_id |= 1 << 25
    # uptime (4 bytes), health, mode, vendor status, tail
    return can.Message(arbitration_id=can_id, is_extended_id=True,
                       data=bytes([0, 0, 0, 0, 0, mode, 0, tail]))


class TestHeartbeatSource:
    def test_reads_node_id_and_mode(self):
        assert _heartbeat_source(_heartbeat(42, mode=1)) == (42, 1)

    @pytest.mark.parametrize("msg", [
        _heartbeat(42, anonymous=True),
        _heartbeat(42, service=True),
        _heartbeat(42, subject=7510),
        _heartbeat(42, tail=0xC0),   # toggle clear: DroneCAN, not Cyphal v1
        _heartbeat(42, tail=0xA0),   # first frame of a multi-frame transfer
    ])
    def test_ignores_everything_else(self, msg):
        assert _heartbeat_source(msg) is None


class TestPickFreeNodeId:
    @pytest.fixture(autouse=True)
    def short_period(self, monkeypatch):
        monkeypatch.setattr(can_hub, "HEARTBEAT_MAX_PUBLICATION_PERIOD", 0.2)

    @pytest.fixture
    def channel(self):
        return f"test-pnp-{next(_wire_numbers)}"

    def _publish(self, channel, node_ids, stop):
        bus = can.Bus(interface="virtual", channel=channel)
        try:
            while not stop.is_set():
                for node_id in node_ids:
                    bus.send(_heartbeat(node_id))
                time.sleep(0.02)
        finally:
            bus.shutdown()

    def _pick_while_publishing(self, channel, node_ids, **kwargs):
        stop = threading.Event()
        publisher = threading.Thread(target=self._publish, args=(channel, node_ids, stop))
        publisher.start()
        try:
            return pick_free_node_id(f"virtual:{channel}", **kwargs)
        finally:
            stop.set()
            publisher.join()

    def test_avoids_node_ids_in_use_and_excluded(self, channel):
        taken = set(range(128)) - {5, 9, 77}
        pick = self._pick_while_publishing(channel, sorted(taken - {9}), exclude=frozenset({9}))
        assert pick in {5, 77}

    def test_none_when_every_node_id_is_taken(self, channel):
        assert self._pick_while_publishing(channel, list(range(128))) is None

    def test_any_node_id_on_a_quiet_bus(self, channel):
        pick = pick_free_node_id(f"virtual:{channel}", rng=random.Random(0))
        assert 0 <= pick <= 127
