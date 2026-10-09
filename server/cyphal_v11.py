"""Noticing Cyphal v1.1 traffic, which Cynitor (a Cyphal v1.0 monitor) cannot decode.

Cyphal/CAN v1.1 publishes on 16-bit subject-IDs in a frame format v1.0 does
not have, so a v1.0 stack drops those frames without a word: a device on the
bus would just look silent. This says that it is there instead.

A v1.1 message frame has, in its extended CAN ID, bit 25 = 0 (a message),
bit 24 = 0 and bit 7 = 1, where v1.0 messages keep bit 7 at 0. DroneCAN
service frames set bit 7 too; they are told apart by the first frame of a
transfer, whose tail byte has the toggle bit 1 in Cyphal and 0 in DroneCAN.
Only first frames are counted, so a count is of transfers, not frames.
(Layout as in pycyphal2's and libcanard v5's CAN code; the v1.1
specification is a draft.)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import can

logger = logging.getLogger(__name__)

V11_ID_MASK = (1 << 25) | (1 << 24) | (1 << 7)
V11_ID_BITS = 1 << 7
_TAIL_START_TOGGLE = 0x80 | 0x20  # start of transfer, toggle
_TAIL_END = 0x40
_MIN_NONFINAL_FRAME = 8  # bytes: a Classic CAN frame's worth, at least
SUBJECT_SAMPLE = 16  # subject-IDs listed in the status; the rest are counted

# For a SocketCAN socket: let the kernel pass only frames that may be v1.1.
SOCKETCAN_FILTER = [{"can_id": V11_ID_BITS, "can_mask": V11_ID_MASK, "extended": True}]


def is_v11_transfer_start(msg: can.Message) -> bool:
    """Whether ``msg`` is the first frame of a Cyphal/CAN v1.1 message transfer."""
    if not msg.is_extended_id or msg.is_error_frame or not msg.data:
        return False
    if msg.arbitration_id & V11_ID_MASK != V11_ID_BITS:
        return False
    tail = msg.data[-1]
    if not tail & _TAIL_END and len(msg.data) < _MIN_NONFINAL_FRAME:
        return False  # every frame but a transfer's last is full: not a Cyphal frame
    return tail & _TAIL_START_TOGGLE == _TAIL_START_TOGGLE


class V11Traffic:
    """What v1.1 traffic has been seen: transfers, from which nodes, on which subject-IDs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.transfers = 0
        self.nodes: set[int] = set()
        self.subjects: set[int] = set()
        self.last_seen_unix: Optional[float] = None

    def observe(self, msg: can.Message) -> None:
        if not is_v11_transfer_start(msg):
            return
        source = msg.arbitration_id & 0x7F
        subject = (msg.arbitration_id >> 8) & 0xFFFF
        with self._lock:
            first = self.transfers == 0
            self.transfers += 1
            self.nodes.add(source)
            self.subjects.add(subject)
            self.last_seen_unix = time.time()
        if first:
            logger.warning("Cyphal v1.1 traffic on the bus (node %d, subject-ID %d): Cynitor speaks "
                           "Cyphal v1.0 and does not decode it", source, subject)

    def status(self) -> Optional[dict]:
        """None until v1.1 traffic is seen."""
        with self._lock:
            if not self.transfers:
                return None
            return {
                "transfers": self.transfers,
                "nodes": sorted(self.nodes),
                "subject_count": len(self.subjects),
                "subject_ids": sorted(self.subjects)[:SUBJECT_SAMPLE],
                "last_seen_unix": self.last_seen_unix,
            }
