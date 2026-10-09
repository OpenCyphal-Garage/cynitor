"""Raw CAN frame capture for the Debugging view.

Frames come from a listen-only tap on the bus, the kind the raw log uses:
behind the CAN hub a listener on its forwarding loops, on SocketCAN a socket
of its own (raw_log.SocketcanTap). Neither changes what Cynitor sends or
receives, and the tap is open only while a dashboard captures. Every frame on
the bus comes through, Cynitor's own and foreign (non-Cyphal) ones among
them, and error frames too, which the Cyphal stack never sees.

The tap hands python-can Messages over on its own thread. They wait in a
deque that a task on the event loop drains, so the subscribers' asyncio
queues are only touched on the loop.
"""

import asyncio
import logging
import time
from collections import deque

from pycyphal.transport import MessageDataSpecifier, ServiceDataSpecifier, Timestamp
from pycyphal.transport.can import CANCapture
from pycyphal.transport.can.media import DataFrame, FrameFormat

logger = logging.getLogger(__name__)


def serialize_capture(cap) -> dict:
    """Convert a pycyphal CANCapture into a small JSON-friendly dict.

    Always returns the raw frame fields. When the frame parses as a valid Cyphal
    frame, transport-layer detail (source/dest node, port, priority, transfer-ID,
    and the tail byte's start/end/toggle bits) is added; otherwise ``cyphal``
    stays False — those are the foreign frames worth flagging during below-DSDL
    debugging.
    """
    frame = cap.frame
    data = bytes(frame.data)
    row = {
        # pycyphal Timestamp.monotonic/.system return decimal.Decimal, which is
        # not JSON-serializable — coerce to float so send_json / json_response work.
        "t": float(cap.timestamp.monotonic),   # seconds — ordering / relative timing
        "ts": float(cap.timestamp.system),      # system (unix) seconds — wall-clock display
        "dir": "tx" if cap.own else "rx",
        "id": f"0x{frame.identifier:08X}",
        "ext": getattr(frame.format, "name", "") == "EXTENDED",
        "dlc": len(data),
        "data": data.hex(" ").upper(),
        "cyphal": False,
    }
    try:
        parsed = cap.parse()
    except Exception:
        parsed = None
    if parsed:
        ss, priority, uf = parsed
        ds = ss.data_specifier
        row.update({
            "cyphal": True,
            "priority": priority.name,
            "src": ss.source_node_id,
            "dst": ss.destination_node_id,
            "transfer_id": uf.transfer_id,
            "start": uf.start_of_transfer,
            "end": uf.end_of_transfer,
            "toggle": uf.toggle_bit,
        })
        if isinstance(ds, MessageDataSpecifier):
            row["kind"] = "msg"
            row["port"] = ds.subject_id
        elif isinstance(ds, ServiceDataSpecifier):
            row["kind"] = "req" if ds.role == ServiceDataSpecifier.Role.REQUEST else "resp"
            row["port"] = ds.service_id
    return row


# What an error frame says (linux/can/error.h), where the driver reports errors
# as SocketCAN does: the classes in its ID, the controller's status in data[1]
# and the kind of protocol violation in data[2].
_ERROR_CLASSES = ((0x001, "tx timeout"), (0x002, "lost arbitration"), (0x004, "controller"),
                  (0x008, "protocol violation"), (0x010, "transceiver"), (0x020, "no ACK"),
                  (0x040, "bus-off"), (0x080, "bus error"), (0x100, "restarted"))
_CONTROLLER_STATUS = ((0x01, "rx overflow"), (0x02, "tx overflow"), (0x04, "rx warning"), (0x08, "tx warning"),
                      (0x10, "rx passive"), (0x20, "tx passive"), (0x40, "back to error-active"))
_PROTOCOL_VIOLATIONS = ((0x01, "bit"), (0x02, "form"), (0x04, "stuff"), (0x08, "dominant bit"),
                        (0x10, "recessive bit"), (0x20, "overload"), (0x40, "active error flag"),
                        (0x80, "while sending"))


def describe_error(can_id: int, data: bytes) -> list:
    """An error frame's classes in words, the controller's and protocol's with their detail."""
    said = []
    for bit, name in _ERROR_CLASSES:
        if not can_id & bit:
            continue
        details = []
        if bit == 0x004 and len(data) > 1:
            details = [detail for mask, detail in _CONTROLLER_STATUS if data[1] & mask]
        elif bit == 0x008 and len(data) > 2:
            details = [detail for mask, detail in _PROTOCOL_VIOLATIONS if data[2] & mask]
        said.append(f"{name}: {', '.join(details)}" if details else name)
    return said


def serialize_message(msg, own_node_ids=frozenset(), decode_errors=True, arrival=None) -> dict:
    """A python-can Message from a tap, in serialize_capture's shape.

    ``dir`` is ``tx`` for a frame sent from this computer (python-can's
    ``is_rx`` False) that, if it is a Cyphal frame, comes from one of
    ``own_node_ids``: a SocketCAN tap hears the other programs on this
    computer too, such as every node on a vcan. An error frame carries
    ``error``, what it reports, in words where ``decode_errors`` (the driver
    reports errors as SocketCAN does), else ``["error frame"]``. A remote
    frame carries ``rtr``.
    """
    # An adapter with a clock of its own counts from its power-up: the frame
    # then takes the time it reached the tap.
    t = msg.timestamp if msg.timestamp > 1e9 else (arrival or time.time())
    data = bytes(msg.data)
    raw = {"t": t, "ts": t, "dir": "rx" if msg.is_rx else "tx", "id": f"0x{msg.arbitration_id:08X}",
           "ext": bool(msg.is_extended_id), "dlc": len(data), "data": data.hex(" ").upper(), "cyphal": False}
    if msg.is_error_frame:
        said = describe_error(msg.arbitration_id, data) if decode_errors else []
        return {**raw, "dir": "rx", "error": said or ["error frame"]}
    if msg.is_remote_frame:
        return {**raw, "dlc": msg.dlc, "data": "", "rtr": True}
    try:
        frame = DataFrame(FrameFormat.EXTENDED if msg.is_extended_id else FrameFormat.BASE,
                          msg.arbitration_id, bytearray(data))
    except ValueError:  # a length CAN has no DLC for: no Cyphal frame
        return raw
    row = serialize_capture(CANCapture(Timestamp(system_ns=0, monotonic_ns=0), frame, own=not msg.is_rx))
    row["t"] = row["ts"] = t  # the message's own time, which nanoseconds and back would blur
    if row["dir"] == "tx" and row["cyphal"] and row["src"] not in own_node_ids:
        row["dir"] = "rx"
    return row


class FrameCaptureManager:
    """Owns the capture tap while anyone captures, a ring of recent frames, and
    the per-client queues that the WebSocket layer drains.

    ``open_tap(on_frame)`` starts calling ``on_frame`` with every frame on the
    bus, from another thread, and returns a function that stops it without
    blocking. The first subscriber opens the tap, and the last one to leave
    closes it, so capture costs nothing while no one looks.
    """

    RING_SIZE = 2000         # recent frames kept for the REST snapshot
    SUB_QUEUE_SIZE = 4000    # per-client backlog before oldest frames are dropped
    WAITING_MAX = 20000      # frames waiting for the event loop; more are dropped
    DRAIN_INTERVAL = 0.05    # seconds between hand-overs to the event loop

    def __init__(self, open_tap, own_node_ids=frozenset, decode_errors: bool = True) -> None:
        self._open_tap = open_tap
        self._own_node_ids = own_node_ids  # a function giving Cynitor's node-IDs
        self._decode_errors = decode_errors
        self._close_tap = None
        self._drainer = None
        self._own = frozenset()
        self._waiting: deque = deque()
        self._ring: deque = deque(maxlen=self.RING_SIZE)
        self._subscribers: set[asyncio.Queue] = set()
        self._count_from_zero()

    def _count_from_zero(self) -> None:
        self.captured = self.rx = self.tx = self.cyphal = self.foreign = self.errors = self.dropped = 0

    @property
    def active(self) -> bool:
        return self._close_tap is not None

    def subscribe(self) -> asyncio.Queue:
        """A queue the frames from now on go to. The first subscriber starts
        capture, and raises whatever opening the tap raised."""
        if not self.active:
            self._start()
        q: asyncio.Queue = asyncio.Queue(maxsize=self.SUB_QUEUE_SIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """No more frames to ``q``. The last subscriber to leave stops capture."""
        self._subscribers.discard(q)
        if not self._subscribers:
            self.stop()

    def _start(self) -> None:
        self._count_from_zero()
        self._ring.clear()
        self._own = frozenset(self._own_node_ids())
        # A deque of this capture's own: a SocketCAN tap may hand over a frame
        # or two while its thread stops, which must not reach the next capture.
        waiting = self._waiting = deque()

        def arrive(msg) -> None:
            # On the tap's thread: a deque append, which is thread-safe, and no more.
            if len(waiting) < self.WAITING_MAX:
                waiting.append((msg, time.time()))
            else:
                self.dropped += 1

        self._close_tap = self._open_tap(arrive)
        self._drainer = asyncio.get_running_loop().create_task(self._drain_loop())
        logger.info("CAN frame capture started")

    def stop(self) -> None:
        """Close the tap, if it is open. The ring keeps the frames until the next start."""
        close, self._close_tap = self._close_tap, None
        if close is None:
            return
        self._drainer.cancel()
        self._drainer = None
        close()
        self._waiting = deque()
        logger.info("CAN frame capture stopped after %d frames", self.captured)

    async def _drain_loop(self) -> None:
        while True:
            await asyncio.sleep(self.DRAIN_INTERVAL)
            self.drain()

    def drain(self) -> None:
        """Hand the frames that came to the ring and the subscribers."""
        while self._waiting:
            msg, arrival = self._waiting.popleft()
            try:
                row = serialize_message(msg, self._own, self._decode_errors, arrival)
            except Exception as exc:
                logger.debug("Frame capture skipped a frame it could not read: %s", exc)
                continue
            self._add(row)

    def _add(self, row: dict) -> None:
        self.captured += 1
        if row["dir"] == "tx":
            self.tx += 1
        else:
            self.rx += 1
        if row["cyphal"]:
            self.cyphal += 1
        elif "error" in row:
            self.errors += 1
        else:
            self.foreign += 1
        self._ring.append(row)
        for q in self._subscribers:
            if q.full():
                try:
                    q.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(row)
            except asyncio.QueueFull:
                self.dropped += 1

    def stats(self) -> dict:
        return {
            "active": self.active,
            "captured": self.captured,
            "rx": self.rx,
            "tx": self.tx,
            "cyphal": self.cyphal,
            "foreign": self.foreign,
            "errors": self.errors,
            "dropped": self.dropped,
        }

    def snapshot(self, limit: int = 500) -> list:
        items = list(self._ring)
        return items[-limit:] if 0 < limit < len(items) else items
