"""Tests for frame_capture: serialize_capture(), serialize_message() and FrameCaptureManager.

All hermetic: pycyphal CANCapture objects are duck-typed with fakes, and the
tap on the bus is a fake handing over python-can Messages, so no real CAN bus
or transport is needed.
"""

import asyncio
import json
import types
from decimal import Decimal
import pytest

import can

from frame_capture import serialize_capture, serialize_message, FrameCaptureManager
from pycyphal.transport import MessageDataSpecifier, ServiceDataSpecifier, Priority


class _Fmt:
    name = "EXTENDED"


class _Frame:
    def __init__(self, identifier, data):
        self.format = _Fmt()
        self.identifier = identifier
        self.data = bytearray(data)


class _Ts:
    # pycyphal Timestamp.monotonic/.system are decimal.Decimal — mirror that here
    # so the serializer's float() coercion (JSON-safety) is actually exercised.
    monotonic = Decimal("123.5")
    system = Decimal("1700000000.0")


class _Cap:
    def __init__(self, identifier, data, own, parsed):
        self.frame = _Frame(identifier, data)
        self.timestamp = _Ts()
        self.own = own
        self._parsed = parsed

    def parse(self):
        return self._parsed


def _msg_parsed(src=42, subject=7509, tid=5, start=True, end=True, toggle=True):
    ss = types.SimpleNamespace(
        source_node_id=src, destination_node_id=None,
        data_specifier=MessageDataSpecifier(subject),
    )
    uf = types.SimpleNamespace(transfer_id=tid, start_of_transfer=start, end_of_transfer=end, toggle_bit=toggle)
    return (ss, Priority.NOMINAL, uf)


class TestSerializeCapture:
    def test_foreign_frame(self):
        row = serialize_capture(_Cap(0x123, b"\xaa\xbb", own=False, parsed=None))
        assert row["cyphal"] is False
        assert row["dir"] == "rx"
        assert row["id"] == "0x00000123"
        assert row["dlc"] == 2
        assert row["data"] == "AA BB"

    def test_timestamps_are_json_serializable_floats(self):
        # Regression: Decimal timestamps from pycyphal must be coerced to float,
        # else send_json / json_response raise and the whole stream silently dies.
        row = serialize_capture(_Cap(0x1, b"\x00", own=True, parsed=_msg_parsed()))
        assert isinstance(row["t"], float)
        assert isinstance(row["ts"], float)
        json.dumps(row)  # must not raise

    def test_cyphal_message_tx(self):
        row = serialize_capture(_Cap(0x1AABBCCD, b"\x01\x02", own=True, parsed=_msg_parsed()))
        assert row["cyphal"] is True
        assert row["dir"] == "tx"
        assert row["kind"] == "msg"
        assert row["port"] == 7509
        assert row["src"] == 42
        assert row["transfer_id"] == 5
        assert row["priority"] == "NOMINAL"

    def test_tail_byte_bits(self):
        # The second frame of a multi-frame transfer: neither start nor end, toggle clear.
        row = serialize_capture(_Cap(0x1, b"\x01\x05", own=False,
                                     parsed=_msg_parsed(start=False, end=False, toggle=False)))
        assert (row["start"], row["end"], row["toggle"]) == (False, False, False)
        row = serialize_capture(_Cap(0x1, b"\x01\xe5", own=False, parsed=_msg_parsed()))
        assert (row["start"], row["end"], row["toggle"]) == (True, True, True)

    def test_cyphal_service_request(self):
        ss = types.SimpleNamespace(
            source_node_id=10, destination_node_id=20,
            data_specifier=ServiceDataSpecifier(384, ServiceDataSpecifier.Role.REQUEST),
        )
        uf = types.SimpleNamespace(transfer_id=1, start_of_transfer=True, end_of_transfer=True, toggle_bit=True)
        row = serialize_capture(_Cap(0x1, b"", own=False, parsed=(ss, Priority.HIGH, uf)))
        assert row["kind"] == "req"
        assert row["port"] == 384
        assert row["dst"] == 20


class _FakeTap:
    """A tap on the bus as CANSession opens it for FrameCaptureManager."""

    def __init__(self):
        self.on_frame = None
        self.opened = 0
        self.closed = 0

    def open(self, on_frame):
        self.on_frame = on_frame
        self.opened += 1
        return self.close

    def close(self):
        self.closed += 1

    def hear(self, *messages):
        for msg in messages:
            self.on_frame(msg)


def _msg(can_id, data=b"\x00", is_rx=True, **kwargs):
    """A python-can Message as a tap hands it over, received at a fixed time."""
    return can.Message(arbitration_id=can_id, data=data, is_rx=is_rx, timestamp=1_700_000_000.25, **kwargs)


# A Cyphal heartbeat from node 42 (subject 7509, priority nominal), single frame.
HEARTBEAT_42 = 0x107D552A


class TestSerializeMessage:
    def test_cyphal_frame_as_serialize_capture_shapes_it(self):
        row = serialize_message(_msg(HEARTBEAT_42, b"\x01\x02\x03\x04\x05\x06\x07\xe5", is_extended_id=True))
        assert (row["cyphal"], row["kind"], row["port"], row["src"], row["dir"]) == (True, "msg", 7509, 42, "rx")
        assert (row["start"], row["end"], row["toggle"], row["transfer_id"]) == (True, True, True, 5)
        assert row["t"] == row["ts"] == 1_700_000_000.25
        json.dumps(row)

    def test_sent_from_this_computer_by_cynitor_or_another_program(self):
        # A SocketCAN tap hears every program on the computer: Cynitor's own
        # node-IDs make a frame tx, a vcan neighbour's stays rx.
        tail = b"\x00\xe0"
        own = serialize_message(_msg(HEARTBEAT_42, tail, is_rx=False, is_extended_id=True), own_node_ids={42})
        neighbour = serialize_message(_msg(HEARTBEAT_42, tail, is_rx=False, is_extended_id=True), own_node_ids={7})
        wire = serialize_message(_msg(HEARTBEAT_42, tail, is_rx=True, is_extended_id=True), own_node_ids={42})
        assert (own["dir"], neighbour["dir"], wire["dir"]) == ("tx", "rx", "rx")

    def test_error_frame_says_what_went_wrong(self):
        # No ACK and a bus error; a stuff error in the protocol byte.
        row = serialize_message(_msg(0x0A8, bytes([0, 0, 0x04, 0, 0, 0, 0, 0]), is_error_frame=True))
        assert row["error"] == ["protocol violation: stuff", "no ACK", "bus error"]
        assert (row["cyphal"], row["dir"]) == (False, "rx")
        unknown = serialize_message(_msg(0x0A8, bytes(8), is_error_frame=True), decode_errors=False)
        assert unknown["error"] == ["error frame"]

    def test_remote_frame(self):
        row = serialize_message(can.Message(arbitration_id=0x123, is_extended_id=False, is_remote_frame=True,
                                            dlc=4, timestamp=1_700_000_000.0))
        assert (row["rtr"], row["dlc"], row["data"], row["ext"], row["cyphal"]) == (True, 4, "", False, False)

    def test_adapter_clock_gives_way_to_arrival(self):
        row = serialize_message(can.Message(arbitration_id=0x1, timestamp=12.5), arrival=1_700_000_001.0)
        assert row["t"] == 1_700_000_001.0


class TestFrameCaptureManager:
    @pytest.mark.asyncio
    async def test_capture_runs_while_anyone_subscribes(self):
        tap = _FakeTap()
        mgr = FrameCaptureManager(tap.open)
        assert mgr.active is False and tap.opened == 0
        first, second = mgr.subscribe(), mgr.subscribe()
        assert mgr.active is True and tap.opened == 1  # one tap however many watch
        mgr.unsubscribe(first)
        assert mgr.active is True and tap.closed == 0
        mgr.unsubscribe(second)
        assert mgr.active is False and tap.closed == 1  # the last one stops it

    @pytest.mark.asyncio
    async def test_frames_reach_subscribers_and_counters(self):
        tap = _FakeTap()
        mgr = FrameCaptureManager(tap.open, own_node_ids=lambda: {42})
        q = mgr.subscribe()
        tap.hear(_msg(0x123, b"\xaa", is_extended_id=False),                          # foreign rx
                 _msg(HEARTBEAT_42, b"\x00\xe0", is_rx=False, is_extended_id=True),  # cyphal, Cynitor's own
                 _msg(0x020, bytes(8), is_error_frame=True))                           # no ACK
        assert q.qsize() == 0  # handed over on the event loop
        mgr.drain()
        assert mgr.stats() == {"active": True, "captured": 3, "rx": 2, "tx": 1, "cyphal": 1, "foreign": 1,
                               "errors": 1, "dropped": 0}
        assert [q.get_nowait()["id"] for _ in range(3)] == ["0x00000123", "0x107D552A", "0x00000020"]
        assert len(mgr.snapshot(10)) == 3
        mgr.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_drained_on_the_event_loop_by_itself(self):
        tap = _FakeTap()
        mgr = FrameCaptureManager(tap.open)
        q = mgr.subscribe()
        tap.hear(_msg(0x7))
        await asyncio.sleep(mgr.DRAIN_INTERVAL * 3)
        assert q.qsize() == 1
        mgr.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_full_subscriber_drops_oldest(self):
        tap = _FakeTap()
        mgr = FrameCaptureManager(tap.open)
        q = asyncio.Queue(maxsize=1)
        keep = mgr.subscribe()
        mgr._subscribers.add(q)
        tap.hear(_msg(0x2), _msg(0x3))  # q full -> drop oldest
        mgr.drain()
        assert mgr.stats()["dropped"] >= 1
        assert q.qsize() == 1
        mgr.unsubscribe(keep)

    @pytest.mark.asyncio
    async def test_frames_the_event_loop_cannot_take_are_counted_lost(self):
        tap = _FakeTap()
        mgr = FrameCaptureManager(tap.open)
        mgr.WAITING_MAX = 2
        q = mgr.subscribe()
        tap.hear(_msg(0x1), _msg(0x2), _msg(0x3))
        mgr.drain()
        assert (q.qsize(), mgr.stats()["dropped"]) == (2, 1)
        mgr.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_a_closed_tap_s_last_frames_stay_out_of_the_next_capture(self):
        # A SocketCAN tap's thread may hand over a frame while it stops.
        tap = _FakeTap()
        mgr = FrameCaptureManager(tap.open)
        mgr.unsubscribe(mgr.subscribe())
        late = tap.on_frame
        q = mgr.subscribe()
        late(_msg(0x5))
        tap.hear(_msg(0x6))
        mgr.drain()
        assert [q.get_nowait()["id"] for _ in range(q.qsize())] == ["0x00000006"]
        mgr.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_a_new_capture_starts_afresh(self):
        tap = _FakeTap()
        mgr = FrameCaptureManager(tap.open)
        q = mgr.subscribe()
        tap.hear(_msg(0x4))
        mgr.drain()
        mgr.unsubscribe(q)
        q = mgr.subscribe()
        assert mgr.stats()["captured"] == 0 and mgr.snapshot() == []
        mgr.unsubscribe(q)
