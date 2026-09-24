"""Open a CAN adapter once and share it with every component inside Cynitor.

SocketCAN lets any number of sockets open can0. Most other adapters do not:
a candleLight (gs_usb) device or an slcan COM port admits one open handle at a
time, and Cynitor opens the bus several times over -- the allocator probe, the
allocator node and the scanner. The second open fails with "Access denied".

The hub opens the adapter once through python-can and bridges it to an
in-process python-can virtual channel. Every component then opens
``virtual:<channel>`` instead, which any number of buses may share:

    adapter <-> CANHub (two pump threads) <-> virtual channel <-> allocator, scanner, ...

Frames one component sends reach the others directly through the virtual
channel, and reach the adapter through the hub, which is how separate
processes on one SocketCAN interface would see each other.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import threading
import time
from typing import Callable, Optional

import can

logger = logging.getLogger(__name__)

_RECV_TIMEOUT = 0.1
_channel_numbers = itertools.count(1)

# Cyphal/CAN heartbeat, used to find a free node-ID the way `yakut accommodate`
# does, but on the hub's channel: a child process cannot see an in-process one.
HEARTBEAT_SUBJECT_ID = 7509
HEARTBEAT_MAX_PUBLICATION_PERIOD = 1.0
_MODE_INITIALIZATION = 1
_MAX_NODE_ID = 127


def open_adapter(spec: str, bitrate: int) -> can.BusABC:
    """Open ``spec`` (pycyphal's ``<interface>:<channel>`` form) as a python-can bus.

    Mirrors how pycyphal reads the same spec, so that ``--can`` means the same
    thing on both paths: ``slcan:COM5@115200`` carries the serial baud rate,
    gs_usb wants its device index as an integer, and numbered channels
    (``kvaser:0``) are integers while named ones (``pcan:PCAN_USBBUS1``) are not.
    """
    interface, sep, channel = spec.partition(":")
    if not sep or not interface:
        raise ValueError(f"Expected <interface>:<channel>, got {spec!r}")
    kwargs: dict = {}
    if interface == "slcan" and "@" in channel:
        channel, _, baud = channel.rpartition("@")
        kwargs["tty_baudrate"] = int(baud)
    elif interface == "gs_usb":
        # python-can compares the channel with the number of devices found.
        kwargs["index"] = int(channel)
    elif channel.isdigit():
        channel = int(channel)
    return can.ThreadSafeBus(interface=interface, channel=channel, bitrate=bitrate, **kwargs)


def frame_bits(msg: can.Message) -> int:
    """On-wire length of a classic CAN frame, without stuffing bits.

    SOF through interframe space: 47 bits of overhead for a base frame and 67
    for an extended one, plus the data field. Leaving stuffing out is what
    canbusload does by default, so both paths report load the same way.
    """
    data_bits = 0 if msg.is_remote_frame else 8 * len(msg.data)
    return (67 if msg.is_extended_id else 47) + data_bits


def presence_check(bus: can.BusABC) -> Optional[Callable[[], bool]]:
    """A cheap "is the adapter still plugged in?" test, for drivers that hide it.

    python-can's gs_usb reads swallow USB errors, so an unplugged CANable
    looks exactly like a quiet bus. Enumerating USB devices for the one we
    opened needs no I/O to the device itself. Other drivers fail their reads
    when the adapter goes away, which the hub already treats as fatal, so
    they get no check.
    """
    device = getattr(getattr(bus, "gs_usb", None), "gs_usb", None)
    if device is None:
        return None
    import usb.core

    # A replugged adapter comes back at a new address, so this stays False
    # for the device this bus opened.
    identity = dict(idVendor=device.idVendor, idProduct=device.idProduct,
                    bus=device.bus, address=device.address)
    backend = device.backend
    return lambda: usb.core.find(backend=backend, **identity) is not None


def close_adapter(bus: can.BusABC) -> None:
    """Shut ``bus`` down and release its USB device right away.

    python-can's gs_usb shutdown stops the adapter but leaves pyusb holding its
    USB interface until the bus object is garbage-collected. WinUSB admits one
    handle per device, so until then reopening the adapter -- reconnecting from
    the dashboard -- fails with "Access denied".
    """
    bus.shutdown()
    device = getattr(getattr(bus, "gs_usb", None), "gs_usb", None)
    if device is not None:
        import usb.util
        usb.util.dispose_resources(device)


class CANHub:
    """One open adapter, shared through a private virtual channel.

    ``error`` is set, and the pumps stop, when the adapter or the channel fails
    for good; the session's health loop reads it. A failed send to the adapter
    is not fatal -- a bus with no other node to acknowledge frames fills the
    adapter's transmit queue -- so it is counted in ``send_failures`` instead.
    """

    def __init__(self, spec: str, bitrate: int,
                 open_bus: Callable[[str, int], can.BusABC] = open_adapter) -> None:
        self.spec = spec
        self.bitrate = bitrate
        self.channel = f"cynitor-hub-{next(_channel_numbers)}"
        self.error: Optional[str] = None
        self.frames_from_bus = 0
        self.frames_to_bus = 0
        self.send_failures = 0
        # Bits on the wire, for bus load. One counter per pump thread, so
        # that neither increment can overwrite the other's.
        self.bits_from_bus = 0
        self.bits_to_bus = 0
        self._open_bus = open_bus
        self._still_present: Optional[Callable[[], bool]] = None
        self._adapter: Optional[can.BusABC] = None
        self._local: Optional[can.BusABC] = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def local_spec(self) -> str:
        """What the components should open instead of the adapter."""
        return f"virtual:{self.channel}"

    def start(self) -> None:
        """Open the adapter and start forwarding. Blocking; raises if the adapter cannot be opened."""
        self._adapter = self._open_bus(self.spec, self.bitrate)
        try:
            # preserve_timestamps passes the adapter's receive time through
            # instead of stamping frames with the moment they were forwarded.
            self._local = can.ThreadSafeBus(
                interface="virtual", channel=self.channel, preserve_timestamps=True,
            )
        except Exception:
            close_adapter(self._adapter)
            self._adapter = None
            raise
        self._still_present = presence_check(self._adapter)
        self._threads = [
            threading.Thread(target=self._pump_from_bus, name=f"{self.channel}-rx", daemon=True),
            threading.Thread(target=self._pump_to_bus, name=f"{self.channel}-tx", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        logger.info("CAN hub: %s at %d bit/s shared as %s", self.spec, self.bitrate, self.local_spec)

    def stop(self) -> None:
        """Stop forwarding and close both buses. Idempotent."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []
        for bus, close in ((self._local, lambda b: b.shutdown()), (self._adapter, close_adapter)):
            if bus is not None:
                try:
                    close(bus)
                except Exception as exc:
                    logger.debug("CAN hub: error closing %s: %s", bus, exc)
        self._local = self._adapter = None

    @property
    def bits_on_bus(self) -> int:
        """Bits of every frame forwarded either way so far."""
        return self.bits_from_bus + self.bits_to_bus

    def health(self) -> Optional[str]:
        """Why the adapter has become unusable, or None. Cheap enough to call every few seconds."""
        if self.error is None and self._still_present is not None and not self._stop.is_set():
            try:
                present = self._still_present()
            except Exception as exc:
                logger.debug("CAN hub: presence check failed: %s", exc)
                present = True  # an unanswerable question is not an unplugged adapter
            if not present:
                self._fail(f"{self.spec}: adapter disconnected (unplugged?)")
        return self.error

    def link_diagnostics(self) -> dict:
        """What the debug view can show about the adapter; the SocketCAN fields have no source here."""
        return {
            "bitrate": self.bitrate,
            "adapter_frames_in": self.frames_from_bus,
            "adapter_frames_out": self.frames_to_bus,
            "adapter_send_failures": self.send_failures,
        }

    def _fail(self, message: str) -> None:
        if self._stop.is_set():
            return  # shutting down; errors from closing buses are expected
        self.error = message
        logger.error("CAN hub stopped: %s", message)
        self._stop.set()

    def _pump_from_bus(self) -> None:
        assert self._adapter is not None and self._local is not None
        while not self._stop.is_set():
            try:
                msg = self._adapter.recv(timeout=_RECV_TIMEOUT)
            except Exception as exc:
                self._fail(f"{self.spec}: receive failed: {exc}")
                return
            # is_rx is False for adapters that echo what they sent (gs_usb
            # does). Forwarding the echo would hand each node its own frames.
            if msg is None or msg.is_error_frame or not msg.is_rx:
                continue
            try:
                self._local.send(msg)
            except Exception as exc:
                self._fail(f"internal channel {self.channel}: {exc}")
                return
            self.frames_from_bus += 1
            self.bits_from_bus += frame_bits(msg)

    def _pump_to_bus(self) -> None:
        assert self._adapter is not None and self._local is not None
        while not self._stop.is_set():
            try:
                msg = self._local.recv(timeout=_RECV_TIMEOUT)
            except Exception as exc:
                self._fail(f"internal channel {self.channel}: {exc}")
                return
            if msg is None:
                continue
            try:
                self._adapter.send(msg)
            except Exception as exc:
                self.send_failures += 1
                # Log the first failure and then every 100th, so a bus with no
                # one to acknowledge does not flood the log.
                if self.send_failures == 1 or self.send_failures % 100 == 0:
                    logger.warning("CAN hub: send to %s failed (%d so far): %s",
                                   self.spec, self.send_failures, exc)
                continue
            self.frames_to_bus += 1
            self.bits_to_bus += frame_bits(msg)


class HubBusLoad:
    """Bus utilization behind the hub, from the frames it forwards.

    Stands in for main.BusLoadMonitor (canbusload), which needs a SocketCAN
    device: same ``utilization`` / ``is_alive`` / ``start`` / ``stop``. Every
    ``interval`` it takes the bits forwarded either way as a share of what the
    bitrate allows. The hub sees all traffic to and from the adapter, so this
    is the whole bus as the adapter hears it.
    """

    def __init__(self, hub: CANHub, interval: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._hub = hub
        self._interval = interval
        self._clock = clock
        self._task: Optional[asyncio.Task] = None
        self._last: Optional[tuple[float, int]] = None
        self.utilization: float = 0.0

    @property
    def is_alive(self) -> bool:
        # A failing adapter is reported by the hub's own health check.
        return True

    def sample(self) -> float:
        """Update and return utilization since the previous sample, in percent."""
        now, bits = self._clock(), self._hub.bits_on_bus
        if self._last is not None:
            elapsed = now - self._last[0]
            if elapsed > 0:
                load = 100.0 * (bits - self._last[1]) / (elapsed * self._hub.bitrate)
                self.utilization = round(min(load, 100.0), 1)
        self._last = (now, bits)
        return self.utilization

    async def start(self) -> None:
        self.sample()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self.utilization = 0.0

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self.sample()


def _heartbeat_source(msg: can.Message) -> Optional[tuple[int, Optional[int]]]:
    """``(source node-ID, mode)`` if ``msg`` is a Cyphal/CAN Heartbeat, else None."""
    if not msg.is_extended_id or msg.is_error_frame or msg.is_remote_frame or not msg.data:
        return None
    can_id = msg.arbitration_id
    if can_id & (1 << 25) or can_id & (1 << 24):  # service transfer / anonymous node
        return None
    if (can_id >> 8) & 0x1FFF != HEARTBEAT_SUBJECT_ID:
        return None
    tail = msg.data[-1]
    # Start and end of transfer with the toggle bit set: a single-frame
    # Cyphal v1 transfer. DroneCAN (UAVCAN v0) starts with the toggle clear.
    if tail & 0xE0 != 0xE0:
        return None
    # Payload: uptime uint32, health, mode, vendor status; then the tail byte.
    mode = msg.data[5] & 0x07 if len(msg.data) >= 7 else None
    return can_id & 0x7F, mode


def pick_free_node_id(local_spec: str, exclude: frozenset[int] = frozenset(),
                      rng: Optional[random.Random] = None) -> Optional[int]:
    """Listen to heartbeats on ``local_spec`` and pick an unused node-ID at random.

    Same procedure as `yakut accommodate`: listen for two heartbeat periods,
    extending the wait whenever a new node shows up (three periods if it is
    still initializing, as the network may be starting), then choose among
    the node-IDs nobody used. Blocking. Returns None if every node-ID is taken.
    """
    interface, _, channel = local_spec.partition(":")
    candidates = set(range(_MAX_NODE_ID + 1)) - set(exclude)
    bus = can.Bus(interface=interface, channel=channel)
    try:
        deadline = time.monotonic() + HEARTBEAT_MAX_PUBLICATION_PERIOD * 2.0
        while (remaining := deadline - time.monotonic()) > 0:
            msg = bus.recv(timeout=remaining)
            heartbeat = _heartbeat_source(msg) if msg is not None else None
            if heartbeat is None:
                continue
            node_id, mode = heartbeat
            if node_id in candidates:
                candidates.discard(node_id)
                multiplier = 3.0 if mode == _MODE_INITIALIZATION else 1.0
                deadline = max(deadline, time.monotonic() + HEARTBEAT_MAX_PUBLICATION_PERIOD * multiplier)
    finally:
        bus.shutdown()
    if not candidates:
        return None
    pick = (rng or random).choice(sorted(candidates))
    logger.info("Node-ID %d chosen; %d of %d unused", pick, len(candidates), _MAX_NODE_ID + 1)
    return pick
