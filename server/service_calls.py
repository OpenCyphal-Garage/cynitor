"""Every service call on the bus, for the recordings.

A Cyphal service call goes from one node to another, and Cynitor's own node
hears only the calls made to it. The recorder listens to the bus instead, on a
listen-only tap like frame capture's: behind the CAN hub a listener on its
forwarding, on SocketCAN a socket of its own that the kernel passes only
service frames. pycyphal's tracer rebuilds each transfer from its frames, each
request is paired with its response, and the call goes to the recordings whose
filter takes it. Cynitor's own calls cross the bus too, so they are recorded
the same way.

The tap hands python-can Messages over on its own thread. They wait in a deque
that a task on the event loop drains, as in frame_capture.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Optional

from pycyphal.transport import ServiceDataSpecifier, Timestamp, TransferTrace
from pycyphal.transport.can import CANCapture, CANTracer
from pycyphal.transport.can.media import DataFrame, FrameFormat

logger = logging.getLogger(__name__)

SERVICE_FRAME = 1 << 25  # the service-not-message bit of a Cyphal/CAN frame's ID
# What a SocketCAN tap lets through: extended frames with that bit set.
SOCKETCAN_FILTER = [{"can_id": SERVICE_FRAME, "can_mask": SERVICE_FRAME, "extended": True}]


def _frame_time(msg, arrival: float) -> float:
    # An adapter with a clock of its own counts from its power-up: the frame
    # then takes the time it reached the tap (as in frame_capture).
    return msg.timestamp if msg.timestamp > 1e9 else arrival


class ServiceCallRecorder:
    """Pairs the service requests and responses on the bus into calls for the recordings.

    ``open_tap(on_frame)`` starts calling ``on_frame`` with the frames on the
    bus, from another thread, and returns what stops it. ``describe(server,
    service_id, is_request, payload)`` gives a transfer's type name (None if
    unknown) and its fields as text. ``log`` is the EventLogger: frames wait
    only while it records, a call is described only if a recording takes it
    (``wants_service_call``), and the calls go to ``log_service_calls``.
    ``node_uid(node_id)`` gives a node's unique-ID, if known.
    """

    REPLY_TIMEOUT = 5.0     # seconds a request waits for its response, as Cynitor's own calls wait
    WAITING_MAX = 20000     # frames waiting for the event loop; more are dropped
    DRAIN_INTERVAL = 0.05   # seconds between hand-overs to the event loop

    def __init__(self, open_tap, describe, log, node_uid=lambda node_id: None) -> None:
        self._open_tap = open_tap
        self._describe = describe
        self._log = log
        self._node_uid = node_uid
        self._close_tap = None
        self._drainer = None
        self._waiting: deque = deque()
        self._tracer = CANTracer()
        # (client, server, service-ID, transfer-ID) -> (time, payload) of a request awaiting its response
        self._pending: dict = {}
        self.dropped = 0

    def start(self) -> None:
        """Open the tap; raises whatever opening it raised."""
        waiting = self._waiting

        def arrive(msg) -> None:
            # On the tap's thread: a few checks and a deque append, which is thread-safe.
            if (not msg.is_extended_id or not msg.arbitration_id & SERVICE_FRAME
                    or msg.is_error_frame or msg.is_remote_frame or not self._log.recording):
                return
            if len(waiting) < self.WAITING_MAX:
                waiting.append((msg, time.time()))
            else:
                self.dropped += 1

        self._close_tap = self._open_tap(arrive)
        self._drainer = asyncio.get_running_loop().create_task(self._drain_loop())

    def stop(self) -> None:
        """Close the tap. Calls still waiting for their response are dropped."""
        close, self._close_tap = self._close_tap, None
        if close is None:
            return
        self._drainer.cancel()
        self._drainer = None
        close()
        self._waiting.clear()
        self._pending.clear()

    async def _drain_loop(self) -> None:
        while True:
            await asyncio.sleep(self.DRAIN_INTERVAL)
            try:
                await self.drain()
            except Exception as exc:  # the next drain goes on
                logger.warning("Service call recorder: %s", exc)

    async def drain(self, now: Optional[float] = None) -> None:
        """Rebuild transfers from the frames that came, and record the calls they end,
        and those whose response is overdue."""
        frames = []
        while self._waiting:
            frames.append(self._waiting.popleft())
        # Behind the hub, the frames from the bus and Cynitor's own come on two threads.
        frames.sort(key=lambda item: _frame_time(*item))
        calls: list[dict] = []
        for msg, arrival in frames:
            try:
                self._take(msg, _frame_time(msg, arrival), calls)
            except Exception as exc:
                logger.debug("Service call recorder skipped a frame it could not read: %s", exc)
        now = time.time() if now is None else now
        for key, (asked, request) in list(self._pending.items()):
            if now - asked > self.REPLY_TIMEOUT:
                del self._pending[key]
                self._finish(key, asked, request, None, None, calls)
        if calls:
            await self._log.log_service_calls(calls)

    def _take(self, msg, t: float, calls: list) -> None:
        ns = int(t * 1e9)
        frame = DataFrame(FrameFormat.EXTENDED, msg.arbitration_id, bytearray(msg.data))
        trace = self._tracer.update(CANCapture(Timestamp(system_ns=ns, monotonic_ns=ns), frame, own=False))
        if not isinstance(trace, TransferTrace):
            return
        meta = trace.transfer.metadata
        spec = meta.session_specifier
        service = spec.data_specifier
        if not isinstance(service, ServiceDataSpecifier):
            return
        payload = b"".join(trace.transfer.fragmented_payload)
        if service.role == ServiceDataSpecifier.Role.REQUEST:
            key = (spec.source_node_id, spec.destination_node_id, service.service_id, meta.transfer_id)
            if key in self._pending:  # the transfer-ID came round again: no response to the earlier one
                self._finish(key, *self._pending.pop(key), None, None, calls)
            self._pending[key] = (t, payload)
        else:
            key = (spec.destination_node_id, spec.source_node_id, service.service_id, meta.transfer_id)
            asked, request = self._pending.pop(key, (None, None))
            self._finish(key, asked, request, t, payload, calls)

    def _finish(self, key, asked, request, answered, response, calls: list) -> None:
        """A call ended, answered or not: described, if a recording takes it."""
        client, server, service_id, _ = key
        call = {"service_id": service_id, "node_id": server, "client_node_id": client}
        if not self._log.wants_service_call(call):
            return
        type_name, request_text = self._text(server, service_id, True, request)
        response_type, response_text = self._text(server, service_id, False, response)
        call.update(
            service_type=type_name or response_type,
            unique_id=self._node_uid(server),
            status="ok" if response is not None else "timeout",
            latency_ms=None if asked is None or answered is None else round((answered - asked) * 1000, 1),
            request=request_text,
            response=response_text,
            timestamp_unix=asked if asked is not None else answered,
        )
        calls.append(call)

    def _text(self, server: int, service_id: int, is_request: bool, payload: Optional[bytes]):
        if payload is None:
            return None, None
        return self._describe(server, service_id, is_request, payload)
