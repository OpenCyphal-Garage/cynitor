"""Open a CAN adapter once and share it with every component inside Cynitor.

SocketCAN lets any number of sockets open can0. Most other adapters do not:
a candleLight (gs_usb) device or an slcan COM port admits one open handle at a
time, and Cynitor opens the bus several times over -- the node-ID picker, the
scanner and each test node. The second open fails with "Access denied".

The hub opens the adapter once through python-can and bridges it to an
in-process python-can virtual channel. Every component then opens
``virtual:<channel>`` instead, which any number of buses may share:

    adapter <-> CANHub (two pump threads) <-> virtual channel <-> scanner, test nodes, ...

Frames one component sends reach the others directly through the virtual
channel, and reach the adapter through the hub, which is how separate
processes on one SocketCAN interface would see each other.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import random
import threading
import time
from typing import Callable, Optional

import can

from raw_log import END_OF_LOG

from can_config import FD_DATA_SAMPLE_POINT, FD_NOMINAL_SAMPLE_POINT
from cyphal_v11 import V11Traffic
from gs_usb_fd import GsUsbFdBus

logger = logging.getLogger(__name__)

_RECV_TIMEOUT = 0.1
_channel_numbers = itertools.count(1)


def _no_filters_on_virtual_channels() -> None:
    """Stop pycyphal setting acceptance filters on python-can virtual channels.

    python-can's ThreadSafeBus guards set_filters and recv with one lock, and
    pycyphal's reader thread holds it through each recv, letting go only to
    take it again at once. Every subscriber made or closed sets the filters,
    from the event loop, which then waits for that lock: seconds, or minutes
    on a busy machine, with the whole dashboard stalled.

    On a virtual channel the filters gain nothing: the channel is software,
    and pycyphal's transport sorts every frame into its sessions anyway.
    Real adapters are opened by the hub itself, not through pycyphal, and
    SocketCAN through pycyphal's own media, so neither is affected.
    """
    from pycyphal.transport.can.media.pythoncan import PythonCANMedia
    configure = PythonCANMedia.configure_acceptance_filters

    def configure_unless_virtual(media, configuration) -> None:
        if not media.interface_name.startswith("virtual:"):
            configure(media, configuration)

    PythonCANMedia.configure_acceptance_filters = configure_unless_virtual  # type: ignore[method-assign,assignment]


_no_filters_on_virtual_channels()

# Cyphal/CAN heartbeat, used to find a free node-ID the way `yakut accommodate`
# does, but on the hub's channel: a child process cannot see an in-process one.
HEARTBEAT_SUBJECT_ID = 7509
HEARTBEAT_MAX_PUBLICATION_PERIOD = 1.0
_MODE_INITIALIZATION = 1
_MAX_NODE_ID = 127
# Cyphal leaves the top two node-IDs to diagnostic tools: taken first, so that
# Cynitor stays clear of the IDs devices use.
_TOOL_NODE_IDS = (127, 126)


# A receive overflow, as SocketCAN reports one and gs_usb_fd passes it on (linux/can/error.h).
_CAN_ERR_CRTL = 0x00000004
_CAN_ERR_CRTL_RX_OVERFLOW = 0x01


def is_rx_overflow(msg: can.Message) -> bool:
    """Whether ``msg`` is an adapter's notice that it lost received frames."""
    return (msg.is_error_frame and bool(msg.arbitration_id & _CAN_ERR_CRTL)
            and len(msg.data) > 1 and bool(msg.data[1] & _CAN_ERR_CRTL_RX_OVERFLOW))


def open_adapter(spec: str, bitrate: int, data_bitrate: Optional[int] = None) -> can.BusABC:
    """Open ``spec`` (pycyphal's ``<interface>:<channel>`` form) as a python-can bus.

    Mirrors how pycyphal reads the same spec, so that ``--can`` means the same
    thing on both paths: ``slcan:COM5@115200`` carries the serial baud rate,
    gs_usb wants its device index as an integer, and numbered channels
    (``kvaser:0``) are integers while named ones (``pcan:PCAN_USBBUS1``) are not.

    With ``data_bitrate`` the adapter is opened as CAN FD, again as pycyphal
    would: PCAN takes a full bit timing, the others the two rates. pycyphal
    runs slcan as CAN FD with the two rates as a bit timing. gs_usb is opened
    with Cynitor's own driver (gs_usb_fd), Classic CAN and CAN FD alike:
    python-can's runs Classic CAN only, and without the flow control some
    adapters need (see gs_usb_fd).
    """
    interface, sep, channel = spec.partition(":")
    if not sep or not interface:
        raise ValueError(f"Expected <interface>:<channel>, got {spec!r}")
    if interface == "gs_usb":
        return GsUsbFdBus(int(channel), bitrate, data_bitrate)
    kwargs: dict = {"bitrate": bitrate}
    if interface == "slcan" and "@" in channel:
        channel, _, baud = channel.rpartition("@")
        kwargs["tty_baudrate"] = int(baud)
    elif channel.isdigit():
        channel = int(channel)  # type: ignore[assignment]  # python-can takes a number for these
    if data_bitrate is not None:
        if interface == "pcan":
            kwargs = {"timing": can.BitTimingFd.from_sample_point(
                f_clock=80_000_000, nom_bitrate=bitrate, nom_sample_point=FD_NOMINAL_SAMPLE_POINT,
                data_bitrate=data_bitrate, data_sample_point=FD_DATA_SAMPLE_POINT,
            ), "fd": True}
        elif interface == "slcan":
            kwargs["timing"] = _rates_only_timing(kwargs.pop("bitrate"), data_bitrate)
        else:
            kwargs.update(data_bitrate=data_bitrate, fd=True)
    return can.ThreadSafeBus(interface=interface, channel=channel, **kwargs)


def _rates_only_timing(bitrate: int, data_bitrate: int) -> can.BitTimingFd:
    """A bit timing that carries just ``bitrate`` and ``data_bitrate``, both exact.

    python-can's slcan takes CAN FD rates only as a BitTimingFd, of which it
    reads nothing but the two rates: the adapter has presets for them (see
    can_config.FD_DATA_BITRATES). Ten time quanta per bit in either phase,
    at a clock both rates divide.
    """
    clock = 10 * math.lcm(bitrate, data_bitrate)
    return can.BitTimingFd(
        f_clock=clock,
        nom_brp=clock // (10 * bitrate), nom_tseg1=7, nom_tseg2=2, nom_sjw=2,
        data_brp=clock // (10 * data_bitrate), data_tseg1=7, data_tseg2=2, data_sjw=2,
    )


def frame_bits(msg: can.Message) -> tuple[int, int]:
    """On-wire length of a frame: (bits at the nominal rate, bits at the data rate).

    Counted the way canbusload counts by default: worst-case bit stuffing,
    SOF through interframe space. A CAN FD frame with bit rate switching
    sends its data phase at the data rate; everything else is nominal.
    """
    length = 0 if msg.is_remote_frame else len(msg.data)
    if not msg.is_fd:
        return (80 if msg.is_extended_id else 55) + 10 * length, 0
    crc = 21 if length >= 16 else 17
    total = (1 + (29 if msg.is_extended_id else 11) + crc + 5 + 12 + 8 * length) * 5 // 4
    data = (1 + 1 + 4 + crc + 8 * length) * 5 // 4 if msg.bitrate_switch else 0
    return total - data, data


class CANHub:
    """One open adapter, shared through a private virtual channel.

    ``error`` is set, and the pumps stop, when the adapter or the channel fails
    for good; the session's health loop reads it. A failed send to the adapter
    is not fatal -- a bus with no other node to acknowledge frames fills the
    adapter's transmit queue -- so it is counted in ``send_failures`` instead.
    """

    def __init__(self, spec: str, bitrate: int, data_bitrate: Optional[int] = None,
                 open_bus: Callable[[str, int, Optional[int]], can.BusABC] = open_adapter) -> None:
        self.spec = spec
        self.bitrate = bitrate
        self.data_bitrate = data_bitrate  # None: Classic CAN
        self.channel = f"cynitor-hub-{next(_channel_numbers)}"
        self.error: Optional[str] = None
        self.frames_from_bus = 0
        self.frames_to_bus = 0
        self.send_failures = 0
        self.error_frames = 0  # reported by the adapter's driver, if it reports them
        self.rx_overflows = 0  # the adapter's notices that it lost frames: no bus error, a loss
        # Called from the pump threads with every frame on the wire, both ways:
        # raw_log.RawLog.write while a raw log runs, frame capture while a
        # dashboard captures. A tuple, replaced whole, for the threads to read.
        self._listeners: tuple[Callable[[can.Message], None], ...] = ()
        self._listeners_lock = threading.Lock()  # for who changes them; the pumps read the tuple as it is
        # Cyphal v1.1 traffic among the received frames (see cyphal_v11).
        self.v11 = V11Traffic()
        # Seconds the forwarded frames occupied the wire, for bus load. One
        # counter per pump thread, so that neither can overwrite the other's.
        self.busy_from_bus = 0.0
        self.busy_to_bus = 0.0
        self._open_bus = open_bus
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
        self._adapter = self._open_bus(self.spec, self.bitrate, self.data_bitrate)
        try:
            # preserve_timestamps passes the adapter's receive time through
            # instead of stamping frames with the moment they were forwarded.
            self._local = can.ThreadSafeBus(
                interface="virtual", channel=self.channel, preserve_timestamps=True,
            )
        except Exception:
            self._adapter.shutdown()
            self._adapter = None
            raise
        self._threads = [
            threading.Thread(target=self._pump, args=(self._pump_from_bus,), name=f"{self.channel}-rx", daemon=True),
            threading.Thread(target=self._pump, args=(self._pump_to_bus,), name=f"{self.channel}-tx", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        rate = f"{self.bitrate} bit/s" + (f", CAN FD data {self.data_bitrate} bit/s" if self.data_bitrate else "")
        logger.info("CAN hub: %s at %s shared as %s", self.spec, rate, self.local_spec)

    def stop(self) -> None:
        """Stop forwarding and close both buses. Idempotent."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []
        for bus in (self._local, self._adapter):
            if bus is not None:
                try:
                    bus.shutdown()
                except Exception as exc:
                    logger.debug("CAN hub: error closing %s: %s", bus, exc)
        self._local = self._adapter = None

    @property
    def busy_seconds(self) -> float:
        """Time on the wire of every frame forwarded either way so far."""
        return self.busy_from_bus + self.busy_to_bus

    def _wire_seconds(self, msg: can.Message) -> float:
        nominal, data = frame_bits(msg)
        return nominal / self.bitrate + data / (self.data_bitrate or self.bitrate)

    def health(self) -> Optional[str]:
        """Why the adapter has become unusable, or None. An unplugged adapter fails the receive
        thread's read (gs_usb_fd's too), which sets it."""
        return self.error

    def link_diagnostics(self) -> dict:
        """What the debug view can show about the adapter; the SocketCAN fields have no source here."""
        return {
            "bitrate": self.bitrate,
            "dbitrate": self.data_bitrate,  # named as SocketCAN's link diagnostics name it
            "adapter_frames_in": self.frames_from_bus,
            "adapter_frames_out": self.frames_to_bus,
            "adapter_send_failures": self.send_failures,
            "adapter_error_frames": self.error_frames,
            "adapter_rx_overflows": self.rx_overflows,
        }

    def add_listener(self, listener: Callable[[can.Message], None]) -> None:
        """Call ``listener`` with every frame on the wire, from the pump threads.

        It and remove_listener may be called from any thread: a raw log starts
        in a worker thread (api_rawlogs), frame capture on the event loop.
        """
        with self._listeners_lock:
            self._listeners = self._listeners + (listener,)

    def remove_listener(self, listener: Callable[[can.Message], None]) -> None:
        with self._listeners_lock:
            self._listeners = tuple(known for known in self._listeners if known != listener)

    def _tell(self, msg: can.Message) -> None:
        for listener in self._listeners:
            try:
                listener(msg)
            except Exception as exc:  # it must not end the pump: drop it, say why
                self.remove_listener(listener)
                logger.error("CAN hub: a frame listener failed and was removed: %s", exc, exc_info=True)

    def _fail(self, message: str) -> None:
        if self._stop.is_set():
            return  # shutting down; errors from closing buses are expected
        self.error = message
        # A played raw log's end is its normal stop (raw_log.END_OF_LOG).
        logger.log(logging.INFO if message.endswith(END_OF_LOG) else logging.ERROR, "CAN hub stopped: %s", message)
        self._stop.set()

    def _said_once(self, exc: Exception, what: str) -> str:
        """``exc`` as the reason the adapter failed: prefixed with the adapter and ``what`` failed,
        unless the driver says both already (gs_usb_fd does: "gs_usb:0: receive failed: …")."""
        reason = str(exc)
        return reason if reason.startswith(f"{self.spec}:") else f"{self.spec}: {what}: {reason}"

    def _pump(self, forward: Callable[[], None]) -> None:
        """Run a pump thread; an error that ends it ends the hub, which the session's health check sees."""
        try:
            forward()
        except Exception as exc:
            logger.error("CAN hub: forwarding failed", exc_info=True)
            self._fail(f"{self.spec}: forwarding stopped: {exc}")

    def _pump_from_bus(self) -> None:
        assert self._adapter is not None and self._local is not None
        while not self._stop.is_set():
            try:
                msg = self._adapter.recv(timeout=_RECV_TIMEOUT)
            except Exception as exc:
                self._fail(self._said_once(exc, "receive failed"))
                return
            if msg is None:
                continue
            # Counted and logged, not forwarded: the Cyphal stack has no use for them.
            if msg.is_error_frame:
                if is_rx_overflow(msg):
                    self.rx_overflows += 1
                else:
                    self.error_frames += 1
                self._tell(msg)
                continue
            # is_rx is False for adapters that echo what they sent (gs_usb
            # does). Forwarding the echo would hand each node its own frames.
            if not msg.is_rx:
                continue
            self.v11.observe(msg)
            try:
                self._local.send(msg)
            except Exception as exc:
                self._fail(f"internal channel {self.channel}: {exc}")
                return
            self.frames_from_bus += 1
            self.busy_from_bus += self._wire_seconds(msg)
            self._tell(msg)

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
            # Taken before the send: a quick node's answer can come back,
            # with the adapter's time on it, before this thread runs again.
            sent_at = time.time()
            try:
                self._adapter.send(msg)
            except Exception as exc:
                self.send_failures += 1
                # Log the first failure and then every 100th, so a bus with no
                # one to acknowledge does not flood the log.
                if self.send_failures == 1 or self.send_failures % 100 == 0:
                    logger.warning("CAN hub: %s (%d so far)", self._said_once(exc, "send failed"), self.send_failures)
                continue
            self.frames_to_bus += 1
            self.busy_to_bus += self._wire_seconds(msg)
            if self._listeners:
                msg.is_rx = False        # sent by Cynitor
                msg.timestamp = sent_at  # pycyphal leaves it unset
                self._tell(msg)


class HubBusLoad:
    """Bus utilization behind the hub, from the frames it forwards.

    Stands in for main.BusLoadMonitor (canbusload), which needs a SocketCAN
    device: same ``utilization`` / ``is_alive`` / ``start`` / ``stop``. Every
    ``interval`` it takes the time the forwarded frames occupied the wire as
    a share of the interval. The hub sees all traffic to and from the
    adapter, so this is the whole bus as the adapter hears it.
    """

    def __init__(self, hub: CANHub, interval: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._hub = hub
        self._interval = interval
        self._clock = clock
        self._task: Optional[asyncio.Task] = None
        self._last: Optional[tuple[float, float]] = None
        self.utilization: float = 0.0

    @property
    def is_alive(self) -> bool:
        # A failing adapter is reported by the hub's own health check.
        return True

    def sample(self) -> float:
        """Update and return utilization since the previous sample, in percent."""
        now, busy = self._clock(), self._hub.busy_seconds
        if self._last is not None:
            elapsed = now - self._last[0]
            if elapsed > 0:
                load = 100.0 * (busy - self._last[1]) / elapsed
                self.utilization = round(min(load, 100.0), 1)
        self._last = (now, busy)
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
                      rng: Optional[random.Random] = None, fd: bool = False) -> Optional[int]:
    """Listen to heartbeats on ``local_spec`` and pick an unused node-ID: 127 or 126, else one at random.

    Same procedure as `yakut accommodate`: listen for two heartbeat periods,
    extending the wait whenever a new node shows up (three periods if it is
    still initializing, as the network may be starting), then choose among
    the node-IDs nobody used. Blocking. Returns None if every node-ID is taken.

    ``fd`` opens the bus for CAN FD frames, which a SocketCAN socket otherwise
    never receives: on a CAN FD bus every heartbeat is one.
    """
    interface, _, channel = local_spec.partition(":")
    candidates = set(range(_MAX_NODE_ID + 1)) - set(exclude)
    bus = can.Bus(interface=interface, channel=channel, fd=fd)
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
    pick = next((node_id for node_id in _TOOL_NODE_IDS if node_id in candidates), None)
    if pick is None:
        pick = (rng or random).choice(sorted(candidates))
    logger.info("Node-ID %d chosen; %d of %d unused", pick, len(candidates), _MAX_NODE_ID + 1)
    return pick
