"""Raw CAN frame/transfer capture for the Debugging view (Phase 2).

Taps pycyphal's transport-level capture API (``CANTransport.begin_capture``) to
expose every frame on the bus — below the DSDL/application layer — including
foreign (non-Cyphal) traffic and the multi-frame/transfer-ID detail that the
decoded telemetry path hides.

Important: pycyphal implements capture by reconfiguring the acceptance filter to
accept everything and forcing loopback on every outgoing frame. It adds bus/CPU
overhead and CANNOT be stopped without closing the transport (i.e. a CAN
disconnect). Capture is therefore opt-in and deliberately sticky — the UI starts
it explicitly and warns the user.

The capture callback runs on the asyncio event loop thread (SocketCAN media
delivers frames via ``loop.call_soon_threadsafe``), so the hand-off to
subscribers uses plain ``asyncio.Queue.put_nowait`` and must stay non-blocking.
"""

import asyncio
import logging
from collections import deque

from pycyphal.transport import MessageDataSpecifier, ServiceDataSpecifier

logger = logging.getLogger(__name__)


def serialize_capture(cap) -> dict:
    """Convert a pycyphal CANCapture into a small JSON-friendly dict.

    Always returns the raw frame fields. When the frame parses as a valid Cyphal
    frame, transport-layer detail (source/dest node, port, priority, transfer-ID,
    start/end flags) is added; otherwise ``cyphal`` stays False — those are the
    foreign frames worth flagging during below-DSDL debugging.
    """
    frame = cap.frame
    data = bytes(frame.data)
    row = {
        # pycyphal Timestamp.monotonic/.system return decimal.Decimal, which is
        # not JSON-serializable — coerce to float so send_json / json_response work.
        "t": float(cap.timestamp.monotonic),   # monotonic seconds — ordering / relative timing
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
        })
        if isinstance(ds, MessageDataSpecifier):
            row["kind"] = "msg"
            row["port"] = ds.subject_id
        elif isinstance(ds, ServiceDataSpecifier):
            row["kind"] = "req" if ds.role == ServiceDataSpecifier.Role.REQUEST else "resp"
            row["port"] = ds.service_id
    return row


class FrameCaptureManager:
    """Owns the transport capture tap, a recent-frame ring buffer, and the set
    of per-client subscriber queues that the WebSocket layer drains."""

    RING_SIZE = 2000        # recent frames kept for the REST snapshot
    SUB_QUEUE_SIZE = 4000   # per-client backlog before oldest frames are dropped

    def __init__(self, scanner) -> None:
        self._scanner = scanner
        self._ring: deque = deque(maxlen=self.RING_SIZE)
        self._subscribers: set[asyncio.Queue] = set()
        self.captured = 0
        self.rx = 0
        self.tx = 0
        self.cyphal = 0
        self.foreign = 0
        self.dropped = 0

    @property
    def active(self) -> bool:
        return self._scanner.capture_active

    def start(self) -> bool:
        """Begin transport-level capture. Idempotent. Returns the active state.

        Sticky by nature of pycyphal — see the module docstring."""
        if not self.active:
            self._scanner.begin_frame_capture(self._on_capture)
            logger.warning(
                "CAN frame capture started — accept-all filtering and forced "
                "loopback are now active until CAN disconnect"
            )
        return self.active

    def _on_capture(self, cap) -> None:
        # Hot path on the event loop thread: keep it cheap and non-blocking.
        try:
            row = serialize_capture(cap)
        except Exception:
            return
        self.captured += 1
        if row["dir"] == "tx":
            self.tx += 1
        else:
            self.rx += 1
        if row["cyphal"]:
            self.cyphal += 1
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

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.SUB_QUEUE_SIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def stats(self) -> dict:
        return {
            "active": self.active,
            "captured": self.captured,
            "rx": self.rx,
            "tx": self.tx,
            "cyphal": self.cyphal,
            "foreign": self.foreign,
            "dropped": self.dropped,
        }

    def snapshot(self, limit: int = 500) -> list:
        items = list(self._ring)
        return items[-limit:] if 0 < limit < len(items) else items
