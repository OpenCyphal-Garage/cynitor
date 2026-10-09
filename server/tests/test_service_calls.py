"""Tests for service_calls.ServiceCallRecorder: the calls on the bus, from their frames.

The frames are real Cyphal/CAN frames, made by pycyphal's own serializer and
handed over as python-can Messages by a fake tap; the event logger and the
type lookup are fakes. Drains are run by hand.
"""

import can
import pytest
from pycyphal.transport import Priority
from pycyphal.transport.can._identifier import MessageCANID, ServiceCANID
from pycyphal.transport.can._session._transfer_sender import serialize_transfer

from service_calls import ServiceCallRecorder

T = 1_700_000_000.0
CLASSIC = 7  # the payload bytes of a Classic CAN frame, its tail byte aside


def _frames(can_id: int, transfer_id: int, payload: bytes, t: float) -> list:
    """The python-can Messages a transfer goes out as, all at time ``t``."""
    out = []
    for frame in serialize_transfer(can_id, transfer_id, [memoryview(payload)], CLASSIC):
        data_frame = frame.compile()
        out.append(can.Message(arbitration_id=data_frame.identifier, data=bytes(data_frame.data),
                               is_extended_id=True, timestamp=t))
    return out


def request(client, server, service_id, transfer_id, payload, t):
    can_id = ServiceCANID(Priority.NOMINAL, client, server, service_id, True).compile([memoryview(payload)])
    return _frames(can_id, transfer_id, payload, t)


def response(server, client, service_id, transfer_id, payload, t):
    can_id = ServiceCANID(Priority.NOMINAL, server, client, service_id, False).compile([memoryview(payload)])
    return _frames(can_id, transfer_id, payload, t)


def message(publisher, subject_id, transfer_id, payload, t):
    can_id = MessageCANID(Priority.NOMINAL, publisher, subject_id).compile([memoryview(payload)])
    return _frames(can_id, transfer_id, payload, t)


class FakeLog:
    def __init__(self, wants=lambda call: True):
        self.recording = True
        self.wants = wants
        self.calls = []

    def wants_service_call(self, call):
        return self.wants(call)

    async def log_service_calls(self, calls):
        self.calls.extend(calls)


class FakeTap:
    def __init__(self):
        self.on_frame = None
        self.closed = False

    def open(self, on_frame):
        self.on_frame = on_frame
        return self.close

    def close(self):
        self.closed = True

    def send(self, *transfers):
        for frames in transfers:
            for msg in frames:
                self.on_frame(msg)


described = []


def describe(server, service_id, is_request, payload):
    described.append(service_id)
    return "uavcan.register.Access_1_0", ("asked " if is_request else "answered ") + payload.hex()


@pytest.fixture
async def bus():
    """A started recorder (its drains run by hand), its fake tap and fake log."""
    described.clear()
    log, tap = FakeLog(), FakeTap()
    recorder = ServiceCallRecorder(tap.open, describe, log, node_uid=lambda node_id: f"uid-{node_id}")
    recorder.DRAIN_INTERVAL = 3600
    yield recorder, tap, log
    recorder.stop()


class TestServiceCallRecorder:

    @pytest.mark.asyncio
    async def test_a_call_between_two_nodes_is_recorded_whole(self, bus):
        """A request and its four-frame response, 12 ms apart: one call, both
        transfers rebuilt from their frames, and the time between them."""
        recorder, tap, log = bus
        recorder.start()
        asked, answered = bytes(range(5)), bytes(range(100, 120))  # 20 bytes and the CRC: four frames
        tap.send(request(10, 20, 384, 3, asked, T), response(20, 10, 384, 3, answered, T + 0.012))
        await recorder.drain()
        assert log.calls == [{
            "service_id": 384, "node_id": 20, "client_node_id": 10,
            "service_type": "uavcan.register.Access_1_0", "unique_id": "uid-20",
            "status": "ok", "latency_ms": 12.0,
            "request": "asked " + asked.hex(), "response": "answered " + answered.hex(),
            "timestamp_unix": T,
        }]

    @pytest.mark.asyncio
    async def test_a_request_with_no_response_is_recorded_as_timed_out(self, bus):
        recorder, tap, log = bus
        recorder.start()
        tap.send(request(10, 20, 430, 7, b"", T))
        await recorder.drain(now=T + 1)
        assert log.calls == []  # still waiting for its response
        await recorder.drain(now=T + ServiceCallRecorder.REPLY_TIMEOUT + 0.1)
        assert [(c["status"], c["latency_ms"], c["request"], c["response"]) for c in log.calls] == [
            ("timeout", None, "asked ", None)]

    @pytest.mark.asyncio
    async def test_a_response_whose_request_was_missed_is_recorded_on_its_own(self, bus):
        recorder, tap, log = bus
        recorder.start()
        tap.send(response(20, 10, 384, 4, b"\x05", T))
        await recorder.drain()
        assert [(c["status"], c["latency_ms"], c["request"], c["response"], c["timestamp_unix"])
                for c in log.calls] == [("ok", None, None, "answered 05", T)]

    @pytest.mark.asyncio
    async def test_a_response_handed_over_before_its_request_is_paired(self, bus):
        """Behind the hub, the frames from the bus and Cynitor's own come on two
        threads: a drain puts them back in time order."""
        recorder, tap, log = bus
        recorder.start()
        tap.send(response(20, 10, 384, 5, b"\x06", T + 0.003), request(10, 20, 384, 5, b"\x07", T))
        await recorder.drain()
        assert [(c["status"], c["latency_ms"]) for c in log.calls] == [("ok", 3.0)]

    @pytest.mark.asyncio
    async def test_only_the_calls_a_recording_takes_are_described(self, bus):
        recorder, tap, log = bus
        log.wants = lambda call: call["service_id"] == 384
        recorder.start()
        tap.send(message(10, 7509, 0, bytes(7), T),
                 request(10, 20, 430, 1, b"", T), response(20, 10, 430, 1, b"\x01", T + 0.001),
                 request(10, 20, 384, 2, b"\x02", T), response(20, 10, 384, 2, b"\x03", T + 0.001))
        await recorder.drain()
        assert [c["service_id"] for c in log.calls] == [384]
        assert set(described) == {384}

    @pytest.mark.asyncio
    async def test_frames_wait_only_while_a_recording_runs(self, bus):
        recorder, tap, log = bus
        recorder.start()
        log.recording = False
        tap.send(request(10, 20, 384, 6, b"\x08", T), response(20, 10, 384, 6, b"\x09", T + 0.001))
        log.recording = True
        await recorder.drain()
        assert log.calls == []

    @pytest.mark.asyncio
    async def test_stop_closes_the_tap(self, bus):
        recorder, tap, log = bus
        recorder.start()
        recorder.stop()
        assert tap.closed
