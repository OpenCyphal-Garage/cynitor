"""Tests for frame_capture: serialize_capture() and FrameCaptureManager.

All hermetic — pycyphal CANCapture objects are duck-typed with fakes so no real
CAN bus or transport is needed.
"""

import asyncio
import json
import types
from decimal import Decimal
import pytest

from frame_capture import serialize_capture, FrameCaptureManager
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


def _msg_parsed(src=42, subject=7509, tid=5):
    ss = types.SimpleNamespace(
        source_node_id=src, destination_node_id=None,
        data_specifier=MessageDataSpecifier(subject),
    )
    uf = types.SimpleNamespace(transfer_id=tid, start_of_transfer=True, end_of_transfer=True)
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

    def test_cyphal_service_request(self):
        ss = types.SimpleNamespace(
            source_node_id=10, destination_node_id=20,
            data_specifier=ServiceDataSpecifier(384, ServiceDataSpecifier.Role.REQUEST),
        )
        uf = types.SimpleNamespace(transfer_id=1, start_of_transfer=True, end_of_transfer=True)
        row = serialize_capture(_Cap(0x1, b"", own=False, parsed=(ss, Priority.HIGH, uf)))
        assert row["kind"] == "req"
        assert row["port"] == 384
        assert row["dst"] == 20


class _FakeScanner:
    def __init__(self):
        self.handler = None
        self._active = False

    def begin_frame_capture(self, handler):
        self.handler = handler
        self._active = True

    @property
    def capture_active(self):
        return self._active


class TestFrameCaptureManager:
    @pytest.mark.asyncio
    async def test_start_idempotent_and_counters(self):
        sc = _FakeScanner()
        mgr = FrameCaptureManager(sc)
        assert mgr.active is False
        assert mgr.start() is True
        first_handler = sc.handler
        assert mgr.start() is True  # idempotent — does not re-register
        assert sc.handler is first_handler

        q = mgr.subscribe()
        sc.handler(_Cap(0x123, b"\xaa", own=False, parsed=None))         # foreign rx
        sc.handler(_Cap(0x1, b"\x00", own=True, parsed=_msg_parsed()))   # cyphal tx
        stats = mgr.stats()
        assert stats == {"active": True, "captured": 2, "rx": 1, "tx": 1,
                         "cyphal": 1, "foreign": 1, "dropped": 0}
        assert q.qsize() == 2
        assert len(mgr.snapshot(10)) == 2

    @pytest.mark.asyncio
    async def test_full_subscriber_drops_oldest(self):
        sc = _FakeScanner()
        mgr = FrameCaptureManager(sc)
        mgr.start()
        q = asyncio.Queue(maxsize=1)
        mgr._subscribers.add(q)
        sc.handler(_Cap(0x2, b"", own=False, parsed=None))
        sc.handler(_Cap(0x3, b"", own=False, parsed=None))  # q full -> drop oldest
        assert mgr.stats()["dropped"] >= 1
        assert q.qsize() == 1

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        sc = _FakeScanner()
        mgr = FrameCaptureManager(sc)
        mgr.start()
        q = mgr.subscribe()
        mgr.unsubscribe(q)
        sc.handler(_Cap(0x4, b"", own=False, parsed=None))
        assert q.qsize() == 0          # no longer receiving
        assert mgr.stats()["captured"] == 1  # ring/counters still updated
