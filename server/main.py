#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import ipaddress
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from can_config import (
    is_simulated,
    is_socketcan,
    MediaSettings,
    media_settings,
    normalize_can_iface,
    socketcan_supports_fd,
    resolve_bitrate,
    resolve_data_bitrate,
    socketcan_device,
    validate_bitrate,
    validate_data_bitrate,
)
from bus_errors import BusErrors
from can_discovery import AdapterCatalog, discover_adapters, simulated_adapters
from can_hub import CANHub, HubBusLoad, pick_free_node_id
from bus_load import BusLoadMonitor
from can_link import _CAN_STATE, discover_can_interfaces, get_can_link_diagnostics, rx_overflows
from cyphal_v11 import SOCKETCAN_FILTER, V11Traffic
from data_dir import (EVENTS_DB, SCANNER_DB, TEST_NODES_FILE, prepare_data_dir,
                      resolve_data_dir)
from firmware import FIRMWARE_DIR, FirmwareServer, begin_update, firmware_path
from log_store import InMemoryLogStore, APILogHandler, TokenRedactingFilter
from raw_log import (END_OF_LOG, LogPlayer, SocketcanTap, log_has_fd_frames,
                     log_path, read_sidecar)
from session_errors import SessionBusy
from session_rawlog import RawLogging
from session_subjects import UserSubjectTypes
from simulation import SimulatedBus
from testnodes import TestNodeBus, TestNodeManager
from on_demand import OnDemandDecoding
from update_check import DISABLE_ENV as DISABLE_UPDATE_CHECK_ENV, UpdateCheck
from startup_setup import ensure_libusb_on_path, prepare_runtime, resolve_project_root
from version import __version__

if TYPE_CHECKING:  # for the type checker only: at run time these load on connect, once the compiled DSDL is on the path
    from allocator import NodeIdAllocator
    from event_logger import EventLogger
    from frame_capture import FrameCaptureManager
    from replay import ReplayManager
    from scanner_node import ScannerNode
    from service_calls import ServiceCallRecorder
    from telemetry_manager import TelemetryManager

IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform == "win32"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

_log_store = InMemoryLogStore(max_entries=5000)
_root_logger = logging.getLogger()
for _handler in _root_logger.handlers:  # the console: the journal under systemd
    _handler.addFilter(TokenRedactingFilter())
if not any(isinstance(handler, APILogHandler) for handler in _root_logger.handlers):
    _root_logger.addHandler(APILogHandler(_log_store))




# SocketCAN interfaces plus every other adapter found, for the dashboard's
# list, and after them the simulated buses.
adapter_catalog = AdapterCatalog(lambda: discover_adapters(discover_can_interfaces()) + simulated_adapters())


def _node_id_from_environment() -> Optional[int]:
    """The node-ID the user fixed with UAVCAN__NODE__ID (pycyphal's name for it), if a valid one."""
    value = os.environ.get("UAVCAN__NODE__ID", "").strip()
    if not value:
        return None
    if value.isdigit() and int(value) <= 127:
        return int(value)
    logger.warning("UAVCAN__NODE__ID=%r is no node-ID (0-127); Cynitor picks one per bus instead", value)
    return None


def is_loopback(bind: str) -> bool:
    """Whether ``bind`` listens on this machine only: a loopback address, or localhost."""
    if bind.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind.strip("[]")).is_loopback
    except ValueError:
        return False  # a host name: whatever it resolves to, assume the network


def bound_address(bind: str, port: int) -> str:
    """Where the server listens, as a link a terminal lets the user click.

    A wildcard address is no place a browser can go, so it stays as it is.
    """
    host = f"[{bind}]" if ":" in bind else bind  # an IPv6 address, as a URL writes it
    if bind in ("0.0.0.0", "::"):
        return f"{host}:{port} (all interfaces)"
    return f"http://{host}:{port}/"


# ---------------------------------------------------------------------------
# CAN session lifecycle
# ---------------------------------------------------------------------------

class CANSession:
    """Manages the lifecycle of all CAN-dependent components."""

    def __init__(self, default_bitrate: Optional[int] = None, data_dir: Path = Path("."),
                 default_data_bitrate: Optional[int] = None, default_allocator: bool = False,
                 decode_on_demand: bool = False, default_listen_only: bool = False,
                 default_no_commands: bool = False) -> None:
        # Where the databases live; main() passes the resolved data folder.
        self.data_dir = Path(data_dir)
        # --bitrate, used when connect() is not given one. There is no built-in
        # default: a guessed bitrate can disrupt the bus.
        self.default_bitrate = None if default_bitrate is None else validate_bitrate(default_bitrate)
        # --data-bitrate, likewise; None means Classic CAN.
        self.default_data_bitrate = (None if default_data_bitrate is None
                                     else validate_data_bitrate(default_data_bitrate))
        # --allocator, likewise: whether Cynitor gives plug-and-play nodes
        # their node-IDs. Off unless asked for: a bus may have one allocator only.
        self.default_allocator = default_allocator
        # --listen-only, likewise: watch the bus without sending anything.
        self.default_listen_only = default_listen_only
        self.listen_only = False  # whether the session that runs sends nothing
        # --no-commands, likewise: read the bus and its nodes, but change no node.
        self.default_no_commands = default_no_commands
        self.no_commands = False  # whether the session that runs refuses writes, calls and updates
        # The interface the server was started with (--can), while the operator
        # has not disconnected it: down, the server is not healthy.
        self.wanted_can: Optional[str] = None
        # --decode-on-demand, and the dashboard's switch: decode only the
        # subjects a dashboard watches or a recording takes, and count the rest
        # from their frames (see transfer_rates). For a bus that carries more
        # than can be decoded; off, every subject is decoded.
        self.decode_on_demand = decode_on_demand
        self.on_demand = OnDemandDecoding(
            lambda on_frame, can_filters: self._open_capture_tap(on_frame, can_filters),
            lambda: self.scanner, lambda: self.event_logger)
        self.can_interface: Optional[str] = None
        self.can_bitrate: Optional[int] = None
        self.can_data_bitrate: Optional[int] = None
        self.can_fd = False
        self._overflows_before: Optional[int] = None  # the SocketCAN interface's rx_over_errors at connect
        # The raw frame log being written, if any, and its SocketCAN tap.
        self.raw_logs = RawLogging(self)
        self.user_types = UserSubjectTypes(self)  # types the user set for subjects no register names  # every frame on the bus to a candump .log file, while asked to
        # Cyphal v1.1 traffic seen on the bus (see cyphal_v11); on SocketCAN
        # from a filtered listen-only socket, behind the hub from the hub.
        self.v11: Optional[V11Traffic] = None
        self._v11_tap: Optional[SocketcanTap] = None
        # Errors on the bus, as the health check samples them (see bus_errors).
        self.bus_errors: Optional[BusErrors] = None
        # The simulated bus and its demo devices, when connected to one.
        self.simulation: Optional[SimulatedBus] = None
        # The nodes made in the Test nodes tab; they run while CAN is connected.
        self.test_nodes = TestNodeManager(self.data_dir / TEST_NODES_FILE)
        # Serves firmware files to nodes being updated; None when Cynitor has
        # no node-ID or plays a raw log, since nothing can be sent then.
        self.firmware: Optional[FirmwareServer] = None
        self.scanner: Optional["ScannerNode"] = None
        self.telemetry: Optional["TelemetryManager"] = None
        self.allocator: Optional["NodeIdAllocator"] = None  # Cynitor's node-ID allocator, while it is on (see allocator)
        self.event_logger: Optional["EventLogger"] = None
        self.frame_capture: Optional["FrameCaptureManager"] = None
        self.service_calls: Optional["ServiceCallRecorder"] = None  # every service call on the bus, for the recordings
        self.bus_load: "Optional[BusLoadMonitor | HubBusLoad]" = None
        # Shares a non-SocketCAN adapter among the components; see can_hub.
        self.hub: Optional[CANHub] = None
        self.registered_nodes: set[int] = set()
        self._tasks: list[asyncio.Task] = []
        # How to stop what the session that runs started, last started first (see connect).
        self._stack = contextlib.AsyncExitStack()
        self._lock = asyncio.Lock()
        self._event_logger_lock = asyncio.Lock()
        self._disconnect_task: Optional[asyncio.Task] = None
        self.last_error: Optional[str] = None
        # A node-ID the user fixed with UAVCAN__NODE__ID, read once here: user
        # input, which no session writes back. Else each session picks one.
        self.fixed_node_id = _node_id_from_environment()
        self.node_id: Optional[int] = None  # Cynitor's own in the session that runs; None: anonymous
        self._media: Optional[MediaSettings] = None  # how its transports open the bus ...
        self._bus_spec: Optional[str] = None          # ... and which (normalize_can_iface's spelling)
        # Recording-replay engine. None unless a replay session is in progress.
        # Mutually exclusive with scanner — the WS handler chooses telemetry
        # vs replay based on which is non-None, and connect()/start_replay()
        # refuse to run while the other side is active.
        self.replay: Optional["ReplayManager"] = None

    @property
    def is_running(self) -> bool:
        return self.scanner is not None

    @property
    def _live_scanner(self) -> "ScannerNode":
        """The scanner; RuntimeError if CAN is not connected, which the API answers as 409."""
        if self.scanner is None:
            raise RuntimeError("CAN is not connected")
        return self.scanner

    async def connect(self, can_iface: str, force_compile: bool = False,
                      bitrate: Optional[int] = None, data_bitrate: Optional[int] = None,
                      *, allocator: Optional[bool] = None, open_bus=None, offline: Optional[dict] = None,
                      listen_only: Optional[bool] = None, no_commands: Optional[bool] = None) -> None:
        """Start all CAN components.

        `bitrate` is ignored by SocketCAN (set it with `ip link`) and required
        for every other interface; None means `default_bitrate`. ValueError is
        raised before anything is opened if it is missing or invalid.

        `data_bitrate` opens a non-SocketCAN adapter as CAN FD; None means
        `default_data_bitrate`, and if that is None too, Classic CAN. SocketCAN
        ignores it and runs CAN FD when the interface is set up for it.

        `allocator` has Cynitor give plug-and-play nodes their node-IDs for
        this session; None means `default_allocator`.

        `listen_only` has the session send nothing: no node-ID, no heartbeat,
        no requests, so no names or registers either; None means
        `default_listen_only`.

        `no_commands` has the session change no node: it names nodes and reads
        registers, but the API refuses register writes, service calls,
        firmware updates and test nodes on a real bus, and no allocator runs;
        None means `default_no_commands`.
        """
        listen_only = self.default_listen_only if listen_only is None else bool(listen_only)
        no_commands = self.default_no_commands if no_commands is None else bool(no_commands)
        bitrate = resolve_bitrate(can_iface, self.default_bitrate if bitrate is None else bitrate)
        data_bitrate = resolve_data_bitrate(
            can_iface, self.default_data_bitrate if data_bitrate is None else data_bitrate)
        # Fast-fail before the slow prepare_runtime step. The double-check inside
        # the lock guards against concurrent connect() calls that both passed
        # this check before prepare_runtime returned.
        if self.is_running:
            raise SessionBusy("Already connected")
        if self.replay is not None:
            raise SessionBusy("Replay session is active — stop it before connecting CAN")

        spec = normalize_can_iface(can_iface)
        if is_socketcan(spec):
            if bitrate is not None:
                logger.info("Connecting to %s; ignoring bitrate %d, SocketCAN's own setting applies", spec, bitrate)
                bitrate = None
            else:
                logger.info("Connecting to %s (bitrate is set by SocketCAN, not by Cynitor)", spec)
        elif is_simulated(spec):
            logger.info("Connecting to the simulated bus %s: %d bit/s%s, its own", spec, bitrate,
                        f", CAN FD data phase {data_bitrate} bit/s" if data_bitrate else "")
        elif data_bitrate is not None:
            logger.info("Connecting to %s at %d bit/s, CAN FD data phase %d bit/s", spec, bitrate, data_bitrate)
        else:
            logger.info("Connecting to %s at %d bit/s", spec, bitrate)

        # prepare_runtime can take seconds (DSDL compile via nnvg, env setup).
        # Running it outside the lock keeps a concurrent disconnect() responsive
        # instead of blocking it behind a cold-start connect.
        hub: Optional[CANHub] = None
        simulation: Optional[SimulatedBus] = None
        media = media_settings(spec, bitrate, data_bitrate)
        if is_socketcan(spec):
            node_id = await self._pick_node_id(spec, fd=media.fd, listen_only=listen_only)
            await asyncio.to_thread(prepare_runtime, force_compile)
        else:
            assert bitrate is not None  # resolve_bitrate gives one for all but SocketCAN
            if is_simulated(spec) and open_bus is None:
                # The hub opens the simulated bus's wire as it would an adapter.
                simulation = SimulatedBus(bitrate, data_bitrate)
                open_bus = simulation.open_wire
            hub, node_id = await self._open_hub(spec, bitrate, force_compile, data_bitrate, open_bus, listen_only)

        async with self._lock:
            if self.is_running:
                if hub is not None:
                    await asyncio.to_thread(hub.stop)
                raise SessionBusy("Already connected")
            self.last_error = None
            # Each component, once started, registers how it stops: _teardown
            # stops them in the reverse order, also after a failure below.
            self._stack = stack = contextlib.AsyncExitStack()
            self.hub = hub
            stack.push_async_callback(self._close_hub)  # last: everything else talks to the adapter through it
            self._media, self._bus_spec, self.node_id = media, spec, node_id
            stack.callback(self._forget_bus)

            try:
                from scanner_node import ScannerNode
                from telemetry_manager import TelemetryManager

                logger.info("Initializing ScannerNode...")
                self.scanner = ScannerNode(register_file=str(self.data_dir / SCANNER_DB),
                                           transport=self._transport(node_id))
                stack.callback(self._close_scanner)
                self.scanner.offline = offline
                self.scanner.listen_only = self.listen_only = listen_only
                self.no_commands = no_commands
                self.scanner.on_own_node_id_conflict = self._own_node_id_taken
                self.user_types.apply_saved()
                if offline is None and self.scanner.node.id is not None:
                    self.firmware = FirmwareServer(self.firmware_folder)
                    self.firmware.serve_on(self.scanner.node)
                    stack.callback(setattr, self, "firmware", None)  # its server closes with the scanner's node
                # Nothing can be sent while a raw log plays, an answer neither.
                if offline is None and not listen_only and not no_commands \
                        and (self.default_allocator if allocator is None else allocator):
                    self._start_allocator()
                    stack.callback(self._close_allocator)  # before the scanner, whose node it answers from

                logger.info("Initializing TelemetryManager...")
                self.telemetry = TelemetryManager(self.scanner)
                await self.telemetry.start()
                stack.push_async_callback(self._stop_telemetry)

                # Frame capture for the Debugging view: its tap opens while a
                # dashboard captures. Error frames read as SocketCAN reports
                # them on SocketCAN, from candleLight (gs_usb) adapters and in
                # raw logs (candump's format).
                from frame_capture import FrameCaptureManager
                self.frame_capture = FrameCaptureManager(
                    self._open_capture_tap, self._own_node_ids,
                    decode_errors=hub is None or hub.spec.partition(":")[0] in ("gs_usb", "rawlog"))
                stack.callback(self._stop_frame_capture)  # before the hub and the scanner, which its tap listens to

                logger.info("Initializing EventLogger...")
                event_logger = await self.ensure_event_logger()
                stack.push_async_callback(self._stop_event_logger)

                saved_identities = await event_logger.load_identity_map()
                if saved_identities:
                    self.scanner.identity_map.load_snapshot(saved_identities)

                saved_node_data = await event_logger.load_all_node_data()
                if saved_node_data:
                    self.scanner.identity_map.load_node_data(saved_node_data)

                self.scanner.on_node_event = event_logger.log_node_event
                self.scanner.on_node_data_save = event_logger.save_node_data

                logger_queue = self.telemetry.subscribe(max_queue=5000, label="logger")
                self._tasks = [
                    asyncio.create_task(_event_logger_loop(self.event_logger, logger_queue)),
                    asyncio.create_task(_register_loop(self.scanner, self.registered_nodes, self)),
                ]
                stack.push_async_callback(self._cancel_tasks)  # before the event logger they write to

                # canbusload reads a SocketCAN device; behind the hub, the
                # hub counts the traffic itself.
                logger.info("Initializing bus load monitor...")
                bus_load: BusLoadMonitor | HubBusLoad
                if hub is None:
                    bus_load = BusLoadMonitor(socketcan_device(can_iface))
                else:
                    bus_load = HubBusLoad(hub)
                self.bus_load = bus_load
                await bus_load.start()
                stack.push_async_callback(self._stop_bus_load)

                if hub is None:
                    self.v11 = V11Traffic()
                    self._v11_tap = self._open_v11_tap(socketcan_device(can_iface), self.v11)
                else:
                    self.v11 = hub.v11
                self.bus_errors = BusErrors(can_iface)
                # The controller's own overflows count from here (behind the hub, the hub counts them).
                self._overflows_before = None if hub is not None else rx_overflows(socketcan_device(can_iface))
                stack.callback(self._stop_bus_watch)

                self.can_interface = can_iface
                self.can_bitrate = bitrate
                self.can_data_bitrate = data_bitrate
                self.can_fd = media.fd

                # Cynitor's own node hears only the calls made to it: the
                # recordings take the calls from a tap on the bus instead.
                from service_calls import ServiceCallRecorder, SOCKETCAN_FILTER as SERVICE_FRAMES
                self.service_calls = ServiceCallRecorder(
                    lambda on_frame: self._open_capture_tap(on_frame, SERVICE_FRAMES),
                    self.scanner.describe_service_transfer, self.event_logger,
                    node_uid=self.scanner._get_node_unique_id_hex)
                try:
                    self.service_calls.start()
                except Exception as exc:  # CAN works on; only the recordings miss the calls
                    logger.warning("Service calls will not be recorded: %s", exc)
                stack.callback(self._stop_service_calls)  # before the event logger it writes to
                # Decoding on demand may be switched on later too: its stop is always due.
                stack.callback(self.on_demand.stop, False)
                if self.decode_on_demand:
                    self.on_demand.start()
                if simulation is not None:
                    # Last, so that the devices join a bus Cynitor already watches.
                    self.simulation = simulation
                    simulation.start()
                    stack.push_async_callback(self._stop_simulation)
                if offline is None and not listen_only:  # nothing can be sent while a raw log plays
                    self.test_nodes.attach(TestNodeBus(self._transport, simulation is not None,
                                                       self._node_ids_in_use))
                    stack.push_async_callback(self.test_nodes.detach)  # first: a bus's nodes go quiet when it is let go
                logger.info("CAN session started on %s", can_iface)
            except Exception:
                try:
                    await self._teardown()
                except Exception as cleanup_err:
                    logger.error("Cleanup error during failed connect: %s", cleanup_err)
                raise

    def _open_capture_tap(self, on_frame: Callable, can_filters: Optional[list] = None) -> Callable[[], None]:
        """Every frame on the bus to ``on_frame`` for frame capture; returns what stops it.

        Behind the hub a listener on its forwarding, on SocketCAN a socket of
        its own: neither changes what Cynitor sends or receives. Stopping a
        socket waits for its thread, so that happens off the event loop. On
        SocketCAN, ``can_filters`` has the kernel pass only the frames they
        accept; behind the hub every frame comes.
        """
        hub = self.hub
        if hub is not None:
            hub.add_listener(on_frame)
            return lambda: hub.remove_listener(on_frame)
        if not self.can_interface:
            raise RuntimeError("CAN is still connecting")
        tap = SocketcanTap(socketcan_device(self.can_interface), self.can_fd, on_frame,
                           can_filters=can_filters, name="capture")
        return lambda: threading.Thread(target=tap.stop, name="capture-stop", daemon=True).start()

    def _own_node_id_taken(self, message: str) -> None:
        """Another node uses Cynitor's node-ID (see ScannerNode): said in /api/status."""
        self.last_error = message

    def _own_node_ids(self) -> set[int]:
        """The node-ID Cynitor's own frames come from, its scanner's; none if it runs anonymously."""
        node_id = getattr(getattr(self.scanner, "node", None), "id", None)
        return set() if node_id is None else {node_id}

    def _start_allocator(self) -> None:
        """Have Cynitor give node-IDs to the nodes that ask for one (see allocator)."""
        if self._live_scanner.node.id is None:
            logger.warning("The allocator is not started: Cynitor has no node-ID of its own to answer from")
            return
        from allocator import NodeIdAllocator
        self.allocator = NodeIdAllocator(self._live_scanner.node, self._node_ids_seen)

    def allocator_status(self) -> Optional[dict]:
        """Cynitor's allocator as /api/status reports it (see NodeIdAllocator.status); None while it is off."""
        return None if self.allocator is None else self.allocator.status()

    def _node_ids_seen(self) -> set[int]:
        """Cynitor's own node-IDs, and those of every node heard on the bus this session, online or not.

        What the allocator must not give out: a node that is off for a while
        comes back on the node-ID it had.
        """
        seen = self._own_node_ids()
        if self.scanner is not None:
            seen |= {node_id for node_id, node in self.scanner.all_nodes.items() if node.has_appeared}
        return seen

    # ── Decoding on demand (see on_demand) ──

    async def set_decode_on_demand(self, on: bool) -> None:
        """Decode only what is watched or recorded (True), or every subject (False). Takes effect at once."""
        async with self._lock:
            self.decode_on_demand = on
            if not self.is_running:
                return
            if on and not self.on_demand.running:
                self.on_demand.start()
            elif not on:
                self.on_demand.stop()

    def set_watched(self, dashboard: object, subject_ids) -> None:
        """The subjects ``dashboard`` shows the values of; None once it has gone."""
        self.on_demand.set_watched(dashboard, subject_ids)

    def _node_ids_in_use(self) -> set[int]:
        """Cynitor's own node-IDs, and those of the nodes online on the bus."""
        in_use = self._own_node_ids()
        if self.scanner is not None:
            in_use |= {node_id for node_id, node in self.scanner.all_nodes.items()
                       if node.has_appeared and not node.has_disappeared}
        return in_use

    def _transport(self, node_id: Optional[int]):
        """A transport on the bus of the session that runs, as ``node_id`` (None: anonymous).

        The scanner's and each test node's: behind the hub on its channel; on
        SocketCAN a socket each, whose frames the kernel loops back to the others.
        """
        from pycyphal.transport.can import CANTransport
        media = self._media
        if media is None:
            raise RuntimeError("CAN is not connected")
        if self.hub is not None:
            from pycyphal.transport.can.media.pythoncan import PythonCANMedia
            return CANTransport(PythonCANMedia(self.hub.local_spec, media.bitrate, media.mtu), node_id)
        from pycyphal.transport.can.media.socketcan import SocketCANMedia
        return CANTransport(SocketCANMedia(socketcan_device(self._bus_spec), media.mtu), node_id)

    @staticmethod
    def _open_v11_tap(device: str, traffic: V11Traffic) -> Optional[SocketcanTap]:
        """A listen-only socket the kernel passes only frames that may be Cyphal v1.1."""
        try:
            return SocketcanTap(device, socketcan_supports_fd(device), traffic.observe,
                                can_filters=SOCKETCAN_FILTER, name="v11-watch")
        except Exception as exc:  # only a notice is lost
            logger.warning("Cannot watch %s for Cyphal v1.1 traffic: %s", device, exc)
            return None

    async def _pick_node_id(self, local_spec: str, fd: bool = False, listen_only: bool = False) -> Optional[int]:
        """Cynitor's own node-ID for the session: the one the user fixed, else
        one no heartbeat on the bus uses; None to run anonymously.

        The way `yakut accommodate` does it, without needing yakut: without a
        node-ID the scanner runs anonymously and can ask no node for its
        GetInfo or registers. Listening only, it runs anonymously on purpose,
        and so sends nothing.
        """
        if listen_only:
            return None
        if self.fixed_node_id is not None:
            return self.fixed_node_id
        node_id = await asyncio.to_thread(pick_free_node_id, local_spec, frozenset(), None, fd)
        if node_id is None:
            logger.warning("Every node-ID is in use; the scanner will run anonymously")
        return node_id

    async def _open_hub(self, spec: str, bitrate: int, force_compile: bool,
                        data_bitrate: Optional[int] = None, open_bus=None,
                        listen_only: bool = False) -> tuple[CANHub, Optional[int]]:
        """Open a non-SocketCAN adapter once and point every component at the hub's channel.

        Most such adapters admit a single open handle, and the node-ID picker,
        the scanner and the test nodes each open the bus; see can_hub.
        """
        ensure_libusb_on_path()  # before gs_usb is opened, not just before yakut
        hub = CANHub(spec, bitrate, data_bitrate) if open_bus is None else CANHub(spec, bitrate, data_bitrate, open_bus)
        await asyncio.to_thread(hub.start)
        try:
            node_id = await self._pick_node_id(hub.local_spec, listen_only=listen_only)
            await asyncio.to_thread(prepare_runtime, force_compile)
        except BaseException:
            await asyncio.to_thread(hub.stop)
            raise
        return hub, node_id

    async def disconnect(self) -> None:
        """Stop all CAN components (reverse order of connect)."""
        async with self._lock:
            await self._teardown()

    def adapter_overflows(self) -> int:
        """How often the adapter lost received frames this session, its buffer full; 0 where it cannot say.

        Behind the hub, its overflow notices (gs_usb sends them); on SocketCAN,
        the growth of the interface's rx_over_errors. Each is one frame or more.
        """
        if self.hub is not None:
            return self.hub.rx_overflows
        if self._overflows_before is None or not self.can_interface:
            return 0
        now = rx_overflows(socketcan_device(self.can_interface))
        return max(0, now - self._overflows_before) if now is not None else 0

    def dropped_events(self) -> Optional[dict[str, int]]:
        """What was discarded because a queue was full, by where it was lost.

        ``scanner``: before reaching anything, CAN frames of subjects whose
        decoding fell too far behind, and decoded events.
        ``logger``: events missing from history and recordings. ``clients``:
        missing from a dashboard's live view. None when CAN is not connected.
        """
        if not self.is_running or not self.scanner or not self.telemetry:
            return None
        return {
            "scanner": self.scanner.dropped_events + self.scanner.dropped_frames() + self.adapter_overflows(),
            "logger": self.telemetry.dropped["logger"]
                      + (self.event_logger.dropped_events if self.event_logger else 0),
            "clients": self.telemetry.dropped["client"],
        }

    @property
    def firmware_folder(self) -> Path:
        return self.data_dir / FIRMWARE_DIR

    async def begin_firmware_update(self, node_id: int, name: str) -> dict:
        """Tell ``node_id`` to update its software from the uploaded file ``name`` (see firmware.begin_update)."""
        if not self.is_running:
            path = firmware_path(self.firmware_folder, name)
            if path is None or not path.is_file():
                raise FileNotFoundError(f"No firmware file {name}")
            raise RuntimeError("CAN is not connected")
        return await begin_update(self.firmware, self._live_scanner.node, self.firmware_folder, node_id, name)

    async def play_raw_log(self, name: str, speed: float = 1.0) -> None:
        """Connect to a saved raw log as if it were the bus (see raw_log.LogPlayer).

        ``speed`` scales the logged pace; 0 plays as fast as frames can be read.
        The session ends when the log does. FileNotFoundError for an unknown log.
        """
        path = log_path(self.raw_logs.folder, name)
        if path is None or not path.is_file():
            raise FileNotFoundError(f"No raw log named {name!r}")
        info = read_sidecar(path)
        fd = info["fd"] if "fd" in info else log_has_fd_frames(path)
        # Rates only size the bus-load estimate here; a SocketCAN session did not record them.
        bitrate = info.get("bitrate") or 500_000
        data_bitrate = (info.get("data_bitrate") or 2_000_000) if fd else None
        player = LogPlayer(path, speed, info.get("own_node_id"))
        await self.connect(
            f"rawlog:{name}", bitrate=bitrate, data_bitrate=data_bitrate, offline=info,
            open_bus=lambda _spec, _bitrate, _data_bitrate: player,
        )
        try:
            # Decode every subject the sidecar names from the first frame on,
            # rather than once each node has registered, which may be too late.
            for node_id, subjects in info.get("publishers", {}).items():
                try:
                    await self._live_scanner.add_subscriptions(int(node_id), {int(s): t for s, t in subjects.items()})
                except Exception as exc:  # e.g. a custom type that is no longer compiled
                    logger.warning("Raw log %s: cannot decode node %s's subjects: %s", name, node_id, exc)
            player.play()
        except BaseException:
            await self.disconnect()  # a session that would never play is not left running
            raise

    def rescan_registrations(self) -> None:
        """Force the register loop to re-attempt every appeared node on its next tick.

        Call this after a successful DSDL compile so that services/publishers whose
        type imports failed earlier (because the type wasn't compiled yet) get
        retried — registered_nodes only re-triggers add_servers / add_subscriptions
        for nodes not already in registered_nodes, and unavailable service_metadata
        entries would otherwise stick until the node disappears.
        """
        if not self.is_running or not self.scanner:
            return
        self.registered_nodes.clear()
        for key, meta in list(self.scanner.service_metadata.items()):
            if meta.get("unavailable"):
                self.scanner.service_metadata.pop(key, None)
        logger.info("Cleared registration state — register loop will re-attempt all nodes")

    async def ensure_event_logger(self) -> "EventLogger":
        """The event logger, started on first use.

        Recordings live in the data folder, so listing, exporting or replaying
        them needs it whether CAN is connected or not. A CAN disconnect stops
        it (flushing what it holds); the next use starts it again.
        """
        async with self._event_logger_lock:  # two first uses at once start one
            event_logger = self.event_logger
            if event_logger is None:
                from event_logger import EventLogger
                event_logger = EventLogger(
                    db_path=self.data_dir / EVENTS_DB,
                    retention_seconds=86400.0,   # keep last 24h of bus traffic
                    max_events=5_000_000,        # safety cap; bounds disk
                )
                await event_logger.start()
                self.event_logger = event_logger
        return event_logger

    async def start_replay(self, recording_id: int, speed: float = 1.0,
                             start_offset_s: float = 0.0) -> dict:
        """Open a recording for playback. Refuses if CAN is connected or
        another replay is already running.
        """
        if self.is_running:
            raise RuntimeError("CAN is connected — disconnect before starting replay")
        if self.replay is not None:
            raise RuntimeError("Replay already in progress")
        event_logger = await self.ensure_event_logger()
        from replay import ReplayManager
        self.replay = ReplayManager(event_logger.db_path,
                                    recording_id=recording_id, speed=speed)

        def _on_finish() -> None:
            # Clear the session attribute when playback ends so the WS handler
            # exits the replay branch and a new replay can be started.
            self.replay = None

        self.replay._on_finish = _on_finish
        try:
            return await self.replay.start(start_offset_s=start_offset_s)
        except Exception:
            self.replay = None
            raise

    async def stop_replay(self) -> None:
        if self.replay is None:
            return
        target = self.replay
        self.replay = None
        await target.stop()

    async def _teardown(self) -> None:
        """Internal cleanup — caller must hold self._lock.

        Stops what connect started, in the reverse order (see self._stack),
        each stop run even if one before it failed.
        """
        logger.info("Tearing down CAN session...")
        # Started while the session runs, so not on the stack; its tap needs the bus. Its file is
        # closed in a worker thread, as the API closes it (api_rawlogs): the disk may be slow.
        await asyncio.to_thread(self.raw_logs.stop)
        stack, self._stack = self._stack, contextlib.AsyncExitStack()
        try:
            await stack.aclose()
        finally:
            self.registered_nodes.clear()
        logger.info("CAN session stopped")

    # What connect registers on self._stack, each the stop of one component.

    async def _close_hub(self) -> None:
        if self.hub is not None:
            await asyncio.to_thread(self.hub.stop)
        self.hub = None

    def _forget_bus(self) -> None:
        self.node_id = None
        self._media = self._bus_spec = None
        self.listen_only = self.no_commands = False
        self.can_interface = self.can_bitrate = self.can_data_bitrate = None
        self.can_fd = False
        self._overflows_before = None

    def _close_scanner(self) -> None:
        if self.scanner is not None:
            self.scanner.close()
        self.scanner = None

    def _close_allocator(self) -> None:
        if self.allocator is not None:
            self.allocator.close()
        self.allocator = None

    async def _stop_telemetry(self) -> None:
        if self.telemetry is not None:
            await self.telemetry.stop()
        self.telemetry = None

    def _stop_frame_capture(self) -> None:
        if self.frame_capture is not None:
            self.frame_capture.stop()
        self.frame_capture = None

    async def _stop_event_logger(self) -> None:
        if self.event_logger is not None:
            if self.scanner is not None:
                await self.event_logger.save_identity_map(self.scanner.identity_map.snapshot())
            await self.event_logger.stop()
        self.event_logger = None

    async def _cancel_tasks(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._tasks.clear()

    async def _stop_bus_load(self) -> None:
        if self.bus_load is not None:
            await self.bus_load.stop()
        self.bus_load = None

    def _stop_bus_watch(self) -> None:
        if self._v11_tap is not None:
            self._v11_tap.stop()
        self._v11_tap = None
        self.v11 = None
        self.bus_errors = None

    def _stop_service_calls(self) -> None:
        if self.service_calls is not None:
            self.service_calls.stop()
        self.service_calls = None

    async def _stop_simulation(self) -> None:
        if self.simulation is not None:
            await self.simulation.stop()
        self.simulation = None

    def schedule_fatal_disconnect(self, error_msg: str) -> None:
        """Schedule a disconnect due to a fatal CAN error (safe to call from background tasks)."""
        if self._disconnect_task and not self._disconnect_task.done():
            logger.warning("Disconnect already in progress, ignoring duplicate")
            return
        if error_msg.endswith(END_OF_LOG):
            logger.info("Raw log played to its end: %s", error_msg)
        else:
            logger.error(f"CAN fatal error: {error_msg}")
        self.last_error = error_msg
        self._disconnect_task = asyncio.create_task(self._deferred_disconnect())

    async def _deferred_disconnect(self) -> None:
        """Disconnect in a separate task to avoid deadlock with background tasks."""
        try:
            await self.disconnect()
        except Exception as e:
            logger.error(f"Error during emergency disconnect: {e}")


# ---------------------------------------------------------------------------
# Node registration
# ---------------------------------------------------------------------------

REGISTRATION_RETRY_S = 10.0


async def register_nodes(scanner, registered_nodes_set: set[int],
                         retry_at: Optional[dict[int, float]] = None) -> None:
    """Register newly discovered nodes and clean up disappeared ones.

    A node counts as registered only once its registers were read in full and
    its subscriptions and clients were created. A node whose port.List names
    no register server, and that does not answer, has none to read: it gets
    clients for the standard services (see update_reg_list). A failure (typically a lost
    response) is logged and the node is retried after REGISTRATION_RETRY_S;
    it does not stop the other nodes from registering.
    """
    retry_at = {} if retry_at is None else retry_at
    loop = asyncio.get_running_loop()
    for node in scanner.nodes.values():
        if node.has_disappeared and node.node_id in registered_nodes_set:
            registered_nodes_set.discard(node.node_id)
            scanner.cleanup_subscriptions(node.node_id)
            logger.info(f"Node {node.node_id} was removed from the node registration list")
            continue

        if not node.has_appeared or not node.has_registered_ports:
            continue

        if node.node_id in registered_nodes_set or node.has_disappeared:
            continue

        if loop.time() < retry_at.get(node.node_id, 0.0):
            continue

        try:
            if isinstance(scanner.offline, dict):  # a raw log plays: nobody answers register reads
                dsdl_pub_messages, dsdl_srv_messages = scanner.offline_ports(node.node_id)
            elif scanner.listen_only is True:  # nothing is asked: the subjects' types are the user's, or guessed
                dsdl_pub_messages, dsdl_srv_messages = {}, {}
            else:
                dsdl_pub_messages, dsdl_srv_messages = await scanner.update_reg_list(node.node_id)
            scanner.node_service_types[node.node_id] = dict(dsdl_srv_messages)
            await scanner.add_subscriptions(node.node_id, dsdl_pub_messages)
            await scanner.add_servers(node.node_id, dsdl_srv_messages)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            retry_at[node.node_id] = loop.time() + REGISTRATION_RETRY_S
            logger.warning(f"Registering node {node.node_id} failed, retrying in "
                           f"{REGISTRATION_RETRY_S:.0f}s: {e}")
            continue

        retry_at.pop(node.node_id, None)
        registered_nodes_set.add(node.node_id)
        logger.info(
            f"Node {node.node_id} registered with publishers: {dsdl_pub_messages} "
            f"and servers: {dsdl_srv_messages}"
        )


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------





def _check_can_health(iface: str) -> Optional[str]:
    """Check CAN interface health via system. Returns error message or None.

    SocketCAN-specific (Linux sysfs + iproute2). On non-Linux platforms we have
    no equivalent introspection; report healthy so the watchdog does not trip.
    """
    if not IS_LINUX:
        return None
    iface_path = Path("/sys/class/net") / iface
    if not iface_path.exists():
        return f"Interface {iface} no longer exists"

    try:
        operstate = (iface_path / "operstate").read_text(encoding="utf-8").strip()
        if operstate == "down":
            return f"Interface {iface} is down"
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["ip", "-details", "link", "show", iface],
            check=False, capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            match = _CAN_STATE.search(result.stdout)
            if match:
                can_state = match.group(1)
                # Off the bus. ERROR-WARNING and ERROR-PASSIVE still pass
                # frames: BusErrors shows them instead.
                if can_state in ("BUS-OFF", "STOPPED"):
                    return f"Interface {iface}: CAN state is {can_state}"
    except Exception:
        pass

    return None


async def _session_health_error(session: 'CANSession') -> Optional[str]:
    """Why the session's CAN link has become unusable, or None if it is fine.

    An adapter behind the hub reports through the hub, which also notices a
    CANable being unplugged; SocketCAN is checked through the kernel, by
    device name rather than spec. The session's CAN FD or Classic MTU is
    fixed when it connects, so an interface switched under it has to be
    connected again.
    """
    if session.hub is not None:
        return await asyncio.to_thread(session.hub.health)
    device = socketcan_device(session.can_interface)
    error = await asyncio.to_thread(_check_can_health, device)
    if not error and socketcan_supports_fd(device) != session.can_fd:
        mode = "Classic CAN" if session.can_fd else "CAN FD"
        error = f"Interface {device} was switched to {mode}; connect again to use it"
    if not error and session.bus_load and not session.bus_load.is_alive:
        error = "CAN bus monitor process exited unexpectedly"
    return error


async def _sample_bus_errors(session: 'CANSession') -> None:
    """Give the session's BusErrors the link's error state now: the hub's counters, or SocketCAN's."""
    if session.bus_errors is None:
        return
    try:
        if session.hub is not None:
            link = session.hub.link_diagnostics()
        else:
            link = await asyncio.to_thread(get_can_link_diagnostics, socketcan_device(session.can_interface))
        session.bus_errors.observe(link)
    except Exception as exc:  # a sample missed must not stop the register loop
        logger.debug("Bus error sample failed: %s", exc)


async def _register_loop(scanner, registered_nodes: set[int], session: Optional['CANSession'] = None) -> None:
    health_check_counter = 0
    registration_retry_at: dict[int, float] = {}
    try:
        while True:
            await asyncio.sleep(1)

            health_check_counter += 1
            if session and session.can_interface and health_check_counter >= 3:
                health_check_counter = 0

                error = await _session_health_error(session)

                if error:
                    session.schedule_fatal_disconnect(error)
                    return
                await _sample_bus_errors(session)
                scanner.report_kernel_drops()

            try:
                scanner.hold_offline_while_losing_frames()
                for node in scanner.all_nodes.values():
                    node.check_disappeared()
                await register_nodes(scanner, registered_nodes, registration_retry_at)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Error in register loop iteration: %s", e, exc_info=True)
    except asyncio.CancelledError:
        logger.debug("Register loop cancelled")
        raise


async def _event_logger_loop(event_logger, queue: asyncio.Queue) -> None:
    try:
        while True:
            event = await queue.get()
            if event.get("undecoded"):  # a rate counted from frames: nothing to keep
                continue
            await event_logger.log_event(event)
    except asyncio.CancelledError:
        logger.debug("Event logger loop cancelled")
        raise


RECONNECT_BACKOFF_S = (5, 10, 20, 40, 60)  # waits between attempts; the last repeats


async def _keep_attached(session, can_iface: str, force_compile: bool = False) -> None:
    """--reconnect: attach `can_iface` again whenever its session is down, until the operator disconnects it."""
    attempt = 0
    while session.wanted_can == can_iface:
        await asyncio.sleep(RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)])
        if session.is_running or session.replay is not None or session.wanted_can != can_iface:
            attempt = 0
            continue
        try:
            await session.connect(can_iface, force_compile=force_compile)
            logger.info("Reconnected to %s", can_iface)
            attempt = 0
        except Exception as exc:
            attempt += 1
            logger.warning("Reconnecting to %s failed (%d): %s", can_iface, attempt, exc)


async def attach_or_fall_back(session, can_iface: str, force_compile: bool = False) -> bool:
    """Attach to `can_iface`, or warn and leave the server in selection mode.

    A bad --can is not worth killing the server over. The dashboard is already
    serving by this point, and it is the obvious place to pick the right
    interface, so say what went wrong, show what is actually available, and
    carry on. Returns whether the attach succeeded.
    """
    try:
        await session.connect(can_iface, force_compile=force_compile)
        return True
    except Exception as exc:
        logger.warning("Could not attach to %r: %s", can_iface, exc)
        try:
            available = await asyncio.to_thread(discover_can_interfaces)
        except Exception:
            # Nothing on this path may take the server down; that is the whole
            # point of falling back rather than exiting.
            available = []
        if available:
            logger.warning("Available CAN interfaces: %s", ", ".join(available))
        elif IS_LINUX:
            logger.warning(
                "No CAN interfaces found. Create a virtual one with: "
                "sudo modprobe vcan && sudo ip link add dev vcan0 type vcan "
                "&& sudo ip link set up vcan0, or try the simulated bus: --can sim:classic"
            )
        else:
            # Only SocketCAN is discovered so far, so off Linux the list is
            # always empty and the adapter has to be named explicitly.
            logger.warning(
                "CAN adapters are not listed automatically on this OS. Name the "
                "python-can interface and channel instead, e.g. gs_usb:0 "
                "(CANable/candleLight), pcan:PCAN_USBBUS1, slcan:COM5@115200 "
                "or kvaser:0, and set the bus speed with --bitrate. Without hardware, "
                "try the simulated bus: --can sim:classic."
            )
        logger.warning("Continuing in selection mode — pick an interface in the dashboard.")
        return False


def show_token_on_terminal(token: Optional[str], stream=None) -> bool:
    """Print the auth token where a person can copy it, and nowhere else.

    Deliberately not logged. The server's own log buffer is served through
    /api/logs and rendered in the dashboard's log panel, and under a service
    manager anything on stdout is captured into the system journal — so
    logging a secret would scatter copies of it.

    Printed only when a terminal is attached, which is exactly when someone is
    there to read it. Supervised runs get nothing.

    Returns whether it printed.
    """
    stream = stream or sys.stderr
    if not token or not getattr(stream, "isatty", lambda: False)():
        return False
    print(
        f"\n  Auth token (paste this into the dashboard when prompted):"
        f"\n\n      {token}\n",
        file=stream,
        flush=True,
    )
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _quiet_completion_of_cancelled_futures(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Drop one known-harmless asyncio error; hand everything else to the default handler.

    pycyphal's python-can media (1.27.1) completes each send's future from its
    transmit thread with call_soon_threadsafe(future.set_result). If the task
    that was awaiting the send is cancelled meanwhile -- as happens on a
    disconnect with a send in flight -- the future is already cancelled, and
    asyncio logs "InvalidStateError: invalid state" with a traceback. Nothing
    is lost: the send had been abandoned.

    Only that case is dropped: an InvalidStateError from a callback that
    completes a future (set_result / set_exception) which is cancelled.
    """
    callback = getattr(context.get("handle"), "_callback", None)
    completer = getattr(callback, "func", None)  # functools.partial(future.set_result, ...)
    future = getattr(completer, "__self__", None)
    if (isinstance(context.get("exception"), asyncio.InvalidStateError)
            and getattr(completer, "__name__", None) in ("set_result", "set_exception")
            and isinstance(future, asyncio.Future) and future.cancelled()):
        logger.debug("Ignored completion of an already cancelled future: %s", context.get("handle"))
        return
    loop.default_exception_handler(context)


async def main(can_iface: Optional[str] = None, force_compile: bool = False, bind: str = "127.0.0.1",
               port: int = 8080, serve_frontend: bool = True,
               can_bitrate: Optional[int] = None, data_dir: Optional[str] = None,
               can_data_bitrate: Optional[int] = None, allocator: bool = False,
               decode_on_demand: bool = False, update_check: bool = True,
               allow_any_interface: bool = False, listen_only: bool = False, access_log: bool = False,
               reconnect: bool = False, no_commands: bool = False) -> None:
    from websocket_server import WebSocketServer
    from dsdl_manager import DsdlManager

    asyncio.get_running_loop().set_exception_handler(_quiet_completion_of_cancelled_futures)

    data_path = resolve_data_dir(data_dir)
    try:
        moved = prepare_data_dir(data_path, legacy_dir=Path.cwd())
    except OSError as exc:
        logger.error("Cannot use %s as the data folder: %s. Choose another with --data-dir.", data_path, exc)
        raise SystemExit(1) from None
    if moved:
        logger.info("Moved %s from %s into the data folder", ", ".join(moved), Path.cwd())

    session = CANSession(default_bitrate=can_bitrate, data_dir=data_path,
                         default_data_bitrate=can_data_bitrate, default_allocator=allocator,
                         decode_on_demand=decode_on_demand, default_listen_only=listen_only,
                         default_no_commands=no_commands)
    project_root = resolve_project_root()
    dsdl_mgr = DsdlManager(project_root, data_dir=data_path)
    dsdl_mgr.make_importable()

    # Optional bearer-token auth. When CYNITOR_AUTH_TOKEN is set in the
    # environment, every REST/WS request outside /api/health must present
    # the token. When unset, the server runs open. Strongly recommended
    # whenever --bind 0.0.0.0 is used.
    auth_token = os.environ.get("CYNITOR_AUTH_TOKEN", "").strip() or None
    if auth_token is not None and len(auth_token) < 16:
        logger.warning("CYNITOR_AUTH_TOKEN is %d characters long: a short token can be guessed. "
                       "Use a long random one, such as $(openssl rand -hex 24)", len(auth_token))

    ws_server = WebSocketServer(
        session=session,
        host=bind,
        port=port,
        log_store=_log_store,
        dsdl_manager=dsdl_mgr,
        auth_token=auth_token,
        # Serve the dashboard too, so deploying to a server is one binary and
        # clients need only a browser. --no-frontend turns that off for
        # API-only deployments, and a checkout without website/ is API-only
        # regardless.
        website_dir=(project_root / "website") if serve_frontend else None,
        adapter_catalog=adapter_catalog,
    )
    ws_server.allow_any_interface = allow_any_interface
    ws_server.access_log = access_log
    await ws_server.start()
    if update_check and os.environ.get(DISABLE_UPDATE_CHECK_ENV, "").strip() not in ("1", "true", "yes"):
        ws_server.update_check = UpdateCheck(__version__)
        ws_server.update_check.start()

    logger.info("=" * 60)
    logger.info("SERVER RUNNING")
    logger.info("=" * 60)
    logger.info("Bound to:    %s", bound_address(bind, port))
    logger.info("Auth:        %s", "token required (CYNITOR_AUTH_TOKEN set)" if auth_token else "OPEN (no token)")
    logger.info("Data:        %s", data_path)
    show_token_on_terminal(auth_token)
    if not is_loopback(bind) and not auth_token:  # only with --insecure-no-auth
        logger.warning("Server is bound to %s with NO auth (--insecure-no-auth): anyone who can reach it can "
                       "command the nodes on the bus. Set CYNITOR_AUTH_TOKEN to require a bearer token.", bind)
    elif not is_loopback(bind):
        logger.info("Server is bound to %s with token auth: clients must present Authorization: Bearer <token>. "
                    "The traffic is plain HTTP; put a TLS reverse proxy in front beyond a trusted network.", bind)
    logger.info("REST API:    http://localhost:%d/api/", port)
    logger.info("Health:      http://localhost:%d/api/health", port)
    logger.info("Status:      http://localhost:%d/api/status", port)
    if ws_server.website_dir:
        logger.info("Dashboard:   http://localhost:%d/", port)
    else:
        logger.info("Dashboard:   not served (API only)")
    if can_iface:
        logger.info("Mode:        direct  (attached to %s at startup)", can_iface)
    else:
        logger.info("Mode:        selection  (waiting for the UI or POST /api/can/connect)")
    logger.info("-" * 60)
    logger.info("Options:     --help lists them")
    if not can_iface:
        logger.info("Selection-mode startup (no --can): connect from the UI or POST /api/can/connect")
    logger.info("=" * 60)

    try:
        if can_iface:
            session.wanted_can = can_iface
            await attach_or_fall_back(session, can_iface, force_compile)
            if reconnect:
                asyncio.create_task(_keep_attached(session, can_iface, force_compile))

        await asyncio.Event().wait()

    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
    finally:
        logger.info("Cleanup starting...")
        if session.is_running:
            await session.disconnect()
        if ws_server.update_check is not None:
            await ws_server.update_check.stop()
        await ws_server.stop()
        logger.info("Shutdown complete")
        _shutdown_complete.set()


def _shutdown_on_sigterm(_signum, _frame) -> None:
    """Turn SIGTERM into the interrupt the entry point already handles.

    Without this, SIGTERM kills the process outright and `main`'s finally
    block never runs, so the CAN session and HTTP server are not closed down.
    """
    raise KeyboardInterrupt


# Set when main's cleanup has run, for _on_console_event to wait for.
_shutdown_complete = threading.Event()
# Windows ends a process right after its console control handlers return, at
# most this long after the event (it allows 5 s for CTRL_CLOSE_EVENT).
_CONSOLE_CLOSE_WAIT_S = 4.5
_CTRL_CLOSE_EVENT, _CTRL_LOGOFF_EVENT, _CTRL_SHUTDOWN_EVENT = 2, 5, 6
_console_handler = None  # the ctypes callback, kept alive while Windows may call it


def _on_console_event(ctrl_type: int) -> bool:
    """A console control handler: the window closed, the user logs off, Windows shuts down.

    It runs the orderly shutdown, as SIGTERM does, and waits for it: Windows
    ends the process as soon as the handlers return, and Python's own returns
    at once (it would deliver the event as SIGBREAK, which no code ever saw).
    Ctrl+C and Ctrl+Break are left to Python (False).
    """
    if ctrl_type not in (_CTRL_CLOSE_EVENT, _CTRL_LOGOFF_EVENT, _CTRL_SHUTDOWN_EVENT):
        return False
    import _thread
    _thread.interrupt_main(signal.SIGTERM)
    _shutdown_complete.wait(_CONSOLE_CLOSE_WAIT_S)
    return True


def _shut_down_when_console_closes() -> None:
    """Windows: register _on_console_event, so that closing the console window shuts Cynitor down
    in order. A no-op elsewhere, where a closing terminal sends SIGHUP and systemd SIGTERM."""
    global _console_handler
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)  # type: ignore[attr-defined]  # Windows only
        _console_handler = handler_type(_on_console_event)
        if not ctypes.WinDLL("kernel32", use_last_error=True).SetConsoleCtrlHandler(_console_handler, True):  # type: ignore[attr-defined]  # Windows only
            logger.debug("Console close handler not set (%d)", ctypes.get_last_error())  # type: ignore[attr-defined]  # Windows only
    except Exception as exc:
        logger.debug("Console close handler unavailable: %s", exc)


def _exit_when_parent_dies() -> None:
    """Ask the kernel to signal us when our parent process goes away.

    The packaged server is a PyInstaller single-file binary: a bootloader
    parent with this interpreter as its child. A SIGKILL to the bootloader
    cannot be forwarded, so without this the interpreter outlives whatever
    started it and keeps holding port 8080. The next start would then find a
    stale server answering on the port it wanted.

    Linux and Windows (see _exit_when_windows_parent_dies); a no-op elsewhere,
    and from source, which has no bootloader: there the parent is a shell, and
    a server left running with nohup or & must outlive it.
    """
    if not getattr(sys, "frozen", False):
        return
    if not IS_LINUX:
        if IS_WINDOWS:
            _exit_when_windows_parent_dies()
        return
    original_ppid = os.getppid()
    try:
        import ctypes

        PR_SET_PDEATHSIG = 1
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
            logger.debug("prctl(PR_SET_PDEATHSIG) failed; parent-death cleanup disabled")
            return
    except Exception as exc:
        logger.debug("Parent-death cleanup unavailable: %s", exc)
        return

    # Closes the race where the parent exited before the call above landed, in
    # which case the signal will never arrive. Compare against the parent we
    # started with rather than against pid 1: an orphan is reparented to the
    # nearest subreaper, which is only init when no other one is registered.
    if os.getppid() != original_ppid:
        logger.warning("Parent process exited during startup; shutting down")
        raise SystemExit(0)


def _exit_when_windows_parent_dies() -> None:
    """Windows counterpart of the parent-death signal: watch the bootloader.

    Windows has no such signal, and killing the bootloader of the frozen
    binary leaves this interpreter running and holding its port, as on Linux.
    A thread waits on the parent's process handle instead, and when it ends,
    delivers SIGTERM to the main thread, which the handler installed in
    __main__ turns into the usual orderly shutdown.

    Frozen builds only. There the parent is the bootloader, which waits for us
    and so is certainly still the process that started us; run from a shell,
    the parent could be anything, and closing a console ends us anyway.
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        import _thread
        import ctypes
        import threading
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        INFINITE = 0xFFFFFFFF
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]  # Windows only
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        parent = kernel32.OpenProcess(SYNCHRONIZE, False, os.getppid())
    except Exception as exc:
        logger.debug("Parent-death cleanup unavailable: %s", exc)
        return
    if not parent:
        logger.debug("Parent-death cleanup unavailable: OpenProcess failed (%d)", ctypes.get_last_error())  # type: ignore[attr-defined]
        return

    def wait_for_parent() -> None:
        kernel32.WaitForSingleObject(parent, INFINITE)
        logger.warning("Parent process exited; shutting down")
        _thread.interrupt_main(signal.SIGTERM)

    threading.Thread(target=wait_for_parent, name="parent-watch", daemon=True).start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the telemetry server")
    parser.add_argument("--version", action="version", version=f"cynitor-server {__version__}")
    parser.add_argument(
        "--can",
        default=None,
        help="CAN interface: a SocketCAN name (vcan0, can0), a python-can spec "
             "such as gs_usb:0, pcan:PCAN_USBBUS1, slcan:COM5@115200, or a simulated "
             "bus with demo devices, sim:classic or sim:fd (if omitted, select from the UI)",
    )

    def _bitrate_arg(text: str) -> int:
        try:
            return validate_bitrate(int(text))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    parser.add_argument(
        "--bitrate",
        type=_bitrate_arg,
        default=None,
        help="CAN bitrate in bit/s; must match the bus. Required with --can for every "
             "adapter Cynitor opens itself (PCAN, gs_usb, slcan, Kvaser, ...). Ignored "
             "for SocketCAN, whose bitrate is set with `ip link`.",
    )

    def _data_bitrate_arg(text: str) -> int:
        try:
            return validate_data_bitrate(int(text))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    parser.add_argument(
        "--data-bitrate",
        type=_data_bitrate_arg,
        default=None,
        help="CAN FD data-phase bitrate in bit/s; opens the adapter as CAN FD (PCAN, "
             "Kvaser, Vector, IXXAT, gs_usb with CAN FD firmware, slcan at 2000000 or "
             "5000000). Leave out for Classic CAN. Ignored for SocketCAN, which runs "
             "CAN FD when the interface is set up for it.",
    )
    parser.add_argument(
        "--allocator",
        action="store_true",
        help="Give plug-and-play nodes their node-IDs. Off by default: a bus may have "
             "one allocator only, so turn it on only for a bus that has none of its own. "
             "The dashboard and POST /api/can/connect can also turn it on for one session.",
    )
    parser.add_argument(
        "--decode-on-demand",
        action="store_true",
        help="Decode only the subjects a dashboard shows the values of or a recording takes; "
             "every other subject is counted from its frames, so its rate still shows. For a "
             "bus that carries more than the server can decode. History and 'save the last "
             "N seconds' then hold only what was decoded. The dashboard can switch it too.",
    )
    parser.add_argument(
        "--recompile",
        action="store_true",
        help="Force DSDL recompilation even if already compiled",
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Host/IP to bind the HTTP server to (default: 127.0.0.1; use 0.0.0.0 to expose on the network)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="TCP port to listen on (default: 8080)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Folder for history, recordings and raw logs "
             "(default: CYNITOR_DATA_DIR, else the per-user data folder: "
             "%%LOCALAPPDATA%%\\Cynitor, ~/.local/share/cynitor, or "
             "~/Library/Application Support/Cynitor). Data found in the current "
             "directory from earlier versions is moved there once.",
    )
    parser.add_argument(
        "--no-frontend",
        action="store_true",
        help="Serve only the REST API and WebSocket; do not serve the dashboard",
    )
    parser.add_argument(
        "--reconnect",
        action="store_true",
        help="With --can: attach to it again whenever its session ends (an adapter unplugged, a bus fault), "
             "waiting 5 s, then longer, up to a minute, between attempts",
    )
    parser.add_argument(
        "--access-log",
        action="store_true",
        help="Log every HTTP request (a dashboard makes a few a second); off by default",
    )
    parser.add_argument(
        "--listen-only",
        action="store_true",
        help="Watch the bus without sending anything: no node-ID, heartbeat or requests, so node names "
             "and registers are not read. The dashboard's Listen only box does the same for one session",
    )
    parser.add_argument(
        "--no-commands",
        action="store_true",
        help="Watch the bus and read its nodes, but change none: register writes, service calls, firmware "
             "updates and test nodes on a real bus are refused, and no allocator runs. The dashboard's "
             "No commands box does the same for one session",
    )
    parser.add_argument(
        "--insecure-no-auth",
        action="store_true",
        help="Allow a --bind beyond loopback without CYNITOR_AUTH_TOKEN: anyone who can reach the "
             "port can then command the nodes on the bus",
    )
    parser.add_argument(
        "--allow-any-interface",
        action="store_true",
        help="Let the dashboard and API open any python-can interface, not only the CAN adapters "
             "Cynitor knows; python-can also has network and serial back-ends",
    )
    parser.add_argument(
        "--no-update-check",
        action="store_true",
        help="Do not ask GitHub, at start and once a day, whether a newer release is out "
             f"(or set {DISABLE_UPDATE_CHECK_ENV}=1)",
    )
    args = parser.parse_args()
    if not is_loopback(args.bind) and not os.environ.get("CYNITOR_AUTH_TOKEN", "").strip() \
            and not args.insecure_no_auth:
        parser.error(f"--bind {args.bind} reaches beyond this machine, and the API commands the nodes on the bus: "
                     "set CYNITOR_AUTH_TOKEN to require a token, or pass --insecure-no-auth on a network you trust")
    if args.can:
        # Refuse up front rather than start and fall back to selection mode:
        # the fix is on the command line, not in the dashboard.
        try:
            resolve_bitrate(args.can, args.bitrate)
        except ValueError as exc:
            parser.error(f"{exc}. Pass it with --bitrate.")
        try:
            resolve_data_bitrate(args.can, args.data_bitrate)
        except ValueError as exc:
            parser.error(str(exc))

    signal.signal(signal.SIGTERM, _shutdown_on_sigterm)
    _exit_when_parent_dies()
    _shut_down_when_console_closes()

    try:
        asyncio.run(main(
            can_iface=args.can,
            force_compile=args.recompile,
            bind=args.bind,
            port=args.port,
            serve_frontend=not args.no_frontend,
            can_bitrate=args.bitrate,
            data_dir=args.data_dir,
            can_data_bitrate=args.data_bitrate,
            allocator=args.allocator,
            decode_on_demand=args.decode_on_demand,
            update_check=not args.no_update_check,
            allow_any_interface=args.allow_any_interface,
            listen_only=args.listen_only,
            no_commands=args.no_commands,
            access_log=args.access_log,
            reconnect=args.reconnect,
        ))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        # A failed start (the port taken, an address not there yet) must not
        # read as success: systemd's Restart=on-failure, scripts and CI go by it.
        sys.exit(1)
    finally:
        _shutdown_complete.set()  # also when main ended before its cleanup could
