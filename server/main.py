#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from can_config import (
    ALLOCATOR_NODE_ID,
    FD_MTU,
    is_socketcan,
    media_mtu,
    normalize_can_iface,
    socketcan_supports_fd,
    resolve_bitrate,
    resolve_data_bitrate,
    socketcan_device,
    validate_bitrate,
    validate_data_bitrate,
)
from bus_errors import BusErrors
from can_discovery import AdapterCatalog, discover_adapters
from can_hub import CANHub, HubBusLoad, pick_free_node_id
from cyphal_v11 import SOCKETCAN_FILTER, V11Traffic
from data_dir import ALLOCATOR_DB, EVENTS_DB, SCANNER_DB, SUBJECT_TYPES_FILE, prepare_data_dir, resolve_data_dir
from firmware import COMMAND_STATUS, FIRMWARE_DIR, FirmwareServer, firmware_path, send_update_command
from log_store import InMemoryLogStore, APILogHandler
from raw_log import (RAW_LOG_DIR, LogPlayer, RawLog, SocketcanTap, log_has_fd_frames, log_path,
                     new_log_name, read_sidecar, write_sidecar)
from type_guess import load_candidates, rank_types
from startup_setup import ensure_libusb_on_path, prepare_runtime, resolve_project_root
from version import __version__

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
if not any(isinstance(handler, APILogHandler) for handler in _root_logger.handlers):
    _root_logger.addHandler(APILogHandler(_log_store))


def discover_can_interfaces() -> list[str]:
    interfaces: set[str] = set()

    def maybe_add_interface(name: str) -> None:
        interface_name = name.split("@", 1)[0].strip()
        if not interface_name:
            return

        type_file = Path("/sys/class/net") / interface_name / "type"
        try:
            if type_file.exists() and type_file.read_text(encoding="utf-8").strip() == "280":
                interfaces.add(interface_name)
                return
        except Exception:
            pass

        if re.match(r"^(v?can\d+|slcan\d+)$", interface_name):
            interfaces.add(interface_name)

    try:
        result = subprocess.run(
            ["ip", "-o", "link", "show"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                match = re.match(r"^\d+:\s+([^:]+):", line)
                if match:
                    maybe_add_interface(match.group(1))
    except Exception:
        pass

    try:
        for iface in Path("/sys/class/net").iterdir():
            maybe_add_interface(iface.name)
    except Exception:
        pass

    return sorted(interfaces)


# SocketCAN interfaces plus every other adapter found, for the dashboard's list.
adapter_catalog = AdapterCatalog(lambda: discover_adapters(discover_can_interfaces()))


# ---------------------------------------------------------------------------
# CAN bus load monitor
# ---------------------------------------------------------------------------

def _get_can_bitrates(iface: str) -> tuple[int, Optional[int]]:
    """Bitrate and CAN FD data bitrate of a SocketCAN interface.

    The bitrate defaults to 500000 where there is none (vcan); the data
    bitrate is None unless the interface is set up for CAN FD.
    """
    bitrate, dbitrate = 500000, None
    try:
        result = subprocess.run(
            ["ip", "-details", "link", "show", iface],
            check=False, capture_output=True, text=True,
        )
        if result.returncode == 0:
            match = re.search(r"\bbitrate\s+(\d+)", result.stdout)
            if match:
                bitrate = int(match.group(1))
            match = re.search(r"\bdbitrate\s+(\d+)", result.stdout)
            if match:
                dbitrate = int(match.group(1))
    except Exception:
        pass
    return bitrate, dbitrate


class BusLoadMonitor:
    """Runs canbusload as a subprocess and exposes the latest utilization %.

    `canbusload` ships with Linux `can-utils`. If it is not on PATH (any non-
    Linux OS, or a minimal Linux install) the monitor becomes a permanent
    no-op: utilization stays None, unknown rather than an idle 0 %, and
    `is_alive` reports True so the health watchdog in `_register_loop` does
    not trip a disconnect.
    """

    def __init__(self, iface: str) -> None:
        self._iface = iface
        self._bitrate, self._dbitrate = _get_can_bitrates(iface)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._disabled = shutil.which("canbusload") is None
        self.utilization: Optional[float] = None if self._disabled else 0.0

    async def start(self) -> None:
        if self._disabled:
            logger.info("BusLoadMonitor disabled: 'canbusload' not on PATH (install can-utils on Linux for bus-load metrics)")
            return
        # canbusload counts CAN FD frames' data phase at ",<dbitrate>". Releases
        # before mid-2021 (Ubuntu 22.04 ships 2020.11) accept the suffix but
        # count Classic CAN frames only, so CAN FD load reads low with them.
        rates = f"{self._bitrate},{self._dbitrate}" if self._dbitrate else str(self._bitrate)
        self._proc = await asyncio.create_subprocess_exec(
            "canbusload", f"{self._iface}@{rates}", "-b",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._task = asyncio.create_task(self._read_loop())
        logger.info("BusLoadMonitor started on %s@%s", self._iface, rates)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._proc:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._proc.kill()
            self._proc = None
        self.utilization = None if self._disabled else 0.0
        logger.info("BusLoadMonitor stopped")

    @property
    def is_alive(self) -> bool:
        if self._disabled:
            return True
        return self._proc is not None and self._proc.returncode is None

    async def _read_loop(self) -> None:
        try:
            assert self._proc and self._proc.stdout
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                match = re.search(r"(\d+)%", text)
                if match:
                    self.utilization = float(match.group(1))
        except asyncio.CancelledError:
            raise


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
                 default_data_bitrate: Optional[int] = None) -> None:
        # Where the databases live; main() passes the resolved data folder.
        self.data_dir = Path(data_dir)
        # --bitrate, used when connect() is not given one. There is no built-in
        # default: a guessed bitrate can disrupt the bus.
        self.default_bitrate = None if default_bitrate is None else validate_bitrate(default_bitrate)
        # --data-bitrate, likewise; None means Classic CAN.
        self.default_data_bitrate = (None if default_data_bitrate is None
                                     else validate_data_bitrate(default_data_bitrate))
        self.can_interface: Optional[str] = None
        self.can_bitrate: Optional[int] = None
        self.can_data_bitrate: Optional[int] = None
        self.can_fd = False
        # The raw frame log being written, if any, and its SocketCAN tap.
        self.raw_log: Optional[RawLog] = None
        self._raw_tap: Optional[SocketcanTap] = None
        # Cyphal v1.1 traffic seen on the bus (see cyphal_v11); on SocketCAN
        # from a filtered listen-only socket, behind the hub from the hub.
        self.v11: Optional[V11Traffic] = None
        self._v11_tap: Optional[SocketcanTap] = None
        # Errors on the bus, as the health check samples them (see bus_errors).
        self.bus_errors: Optional[BusErrors] = None
        # Serves firmware files to nodes being updated; None when Cynitor has
        # no node-ID or plays a raw log, since nothing can be sent then.
        self.firmware: Optional[FirmwareServer] = None
        self.scanner = None
        self.telemetry = None
        self.allocator_manager = None
        self.event_logger = None
        self.frame_capture = None
        self.service_calls = None  # every service call on the bus, for the recordings
        self.bus_load: Optional[BusLoadMonitor] = None
        # Shares a non-SocketCAN adapter among the components; see can_hub.
        self.hub: Optional[CANHub] = None
        self.registered_nodes: set[int] = set()
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._event_logger_lock = asyncio.Lock()
        self._disconnect_task: Optional[asyncio.Task] = None
        self.last_error: Optional[str] = None
        # Recording-replay engine. None unless a replay session is in progress.
        # Mutually exclusive with scanner — the WS handler chooses telemetry
        # vs replay based on which is non-None, and connect()/start_replay()
        # refuse to run while the other side is active.
        self.replay = None

    @property
    def is_running(self) -> bool:
        return self.scanner is not None

    async def connect(self, can_iface: str, force_compile: bool = False,
                      bitrate: Optional[int] = None, data_bitrate: Optional[int] = None,
                      *, open_bus=None, offline: Optional[dict] = None) -> None:
        """Start all CAN components.

        `bitrate` is ignored by SocketCAN (set it with `ip link`) and required
        for every other interface; None means `default_bitrate`. ValueError is
        raised before anything is opened if it is missing or invalid.

        `data_bitrate` opens a non-SocketCAN adapter as CAN FD; None means
        `default_data_bitrate`, and if that is None too, Classic CAN. SocketCAN
        ignores it and runs CAN FD when the interface is set up for it.
        """
        bitrate = resolve_bitrate(can_iface, self.default_bitrate if bitrate is None else bitrate)
        data_bitrate = resolve_data_bitrate(
            can_iface, self.default_data_bitrate if data_bitrate is None else data_bitrate)
        # Fast-fail before the slow prepare_runtime step. The double-check inside
        # the lock guards against concurrent connect() calls that both passed
        # this check before prepare_runtime returned.
        if self.is_running:
            raise RuntimeError("Already connected")
        if self.replay is not None:
            raise RuntimeError("Replay session is active — stop it before connecting CAN")

        spec = normalize_can_iface(can_iface)
        if is_socketcan(spec):
            if bitrate is not None:
                logger.info("Connecting to %s; ignoring bitrate %d, SocketCAN's own setting applies", spec, bitrate)
                bitrate = None
            else:
                logger.info("Connecting to %s (bitrate is set by SocketCAN, not by Cynitor)", spec)
        elif data_bitrate is not None:
            logger.info("Connecting to %s at %d bit/s, CAN FD data phase %d bit/s", spec, bitrate, data_bitrate)
        else:
            logger.info("Connecting to %s at %d bit/s", spec, bitrate)

        # prepare_runtime can take seconds (DSDL compile via nnvg, env setup).
        # Running it outside the lock keeps a concurrent disconnect() responsive
        # instead of blocking it behind a cold-start connect.
        hub: Optional[CANHub] = None
        if is_socketcan(spec):
            device = socketcan_device(spec)
            await self._pick_node_id(spec, fd=socketcan_supports_fd(device))
            await asyncio.to_thread(prepare_runtime, can_iface, force_compile, bitrate)
        else:
            hub = await self._open_hub(spec, bitrate, force_compile, data_bitrate, open_bus)

        async with self._lock:
            if self.is_running:
                if hub is not None:
                    await asyncio.to_thread(hub.stop)
                raise RuntimeError("Already connected")
            self.last_error = None
            # From here on _teardown owns the hub, including on failure below.
            self.hub = hub

            try:
                from scanner_node import ScannerNode
                from telemetry_manager import TelemetryManager
                from allocator import AllocatorManager

                logger.info("Initializing allocator manager...")
                self.allocator_manager = AllocatorManager(
                    check_interval=10.0, check_timeout=3.0,
                    register_file=str(self.data_dir / ALLOCATOR_DB),
                )
                await self.allocator_manager.start()

                logger.info("Initializing ScannerNode...")
                self.scanner = ScannerNode(register_file=str(self.data_dir / SCANNER_DB))
                self.scanner.offline = offline
                self._apply_saved_subject_types()
                if offline is None and self.scanner.node.id is not None:
                    self.firmware = FirmwareServer(self.firmware_folder)
                    self.firmware.serve_on(self.scanner.node)

                logger.info("Initializing TelemetryManager...")
                self.telemetry = TelemetryManager(self.scanner)
                await self.telemetry.start()

                # Frame capture for the Debugging view: its tap opens while a
                # dashboard captures. Error frames read as SocketCAN reports
                # them on SocketCAN, from candleLight (gs_usb) adapters and in
                # raw logs (candump's format).
                from frame_capture import FrameCaptureManager
                self.frame_capture = FrameCaptureManager(
                    self._open_capture_tap, self._own_node_ids,
                    decode_errors=hub is None or hub.spec.partition(":")[0] in ("gs_usb", "rawlog"))

                logger.info("Initializing EventLogger...")
                await self.ensure_event_logger()

                saved_identities = await self.event_logger.load_identity_map()
                if saved_identities:
                    self.scanner.identity_map.load_snapshot(saved_identities)

                saved_node_data = await self.event_logger.load_all_node_data()
                if saved_node_data:
                    self.scanner.identity_map.load_node_data(saved_node_data)

                self.scanner.on_node_event = self.event_logger.log_node_event
                self.scanner.on_node_data_save = self.event_logger.save_node_data

                logger_queue = self.telemetry.subscribe(max_queue=5000, label="logger")
                self._tasks = [
                    asyncio.create_task(_event_logger_loop(self.event_logger, logger_queue)),
                    asyncio.create_task(_register_loop(self.scanner, self.registered_nodes, self)),
                ]

                # canbusload reads a SocketCAN device; behind the hub, the
                # hub counts the traffic itself.
                logger.info("Initializing bus load monitor...")
                if hub is None:
                    self.bus_load = BusLoadMonitor(socketcan_device(can_iface))
                else:
                    self.bus_load = HubBusLoad(hub)
                await self.bus_load.start()

                if hub is None:
                    self.v11 = V11Traffic()
                    self._v11_tap = self._open_v11_tap(socketcan_device(can_iface), self.v11)
                else:
                    self.v11 = hub.v11
                self.bus_errors = BusErrors(can_iface)

                self.can_interface = can_iface
                self.can_bitrate = bitrate
                self.can_data_bitrate = data_bitrate
                self.can_fd = media_mtu() == FD_MTU

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

    def _own_node_ids(self) -> set[int]:
        """The node-IDs Cynitor's own frames come from: its scanner's, and its allocator's."""
        own = {ALLOCATOR_NODE_ID}
        node = getattr(self.scanner, "node", None)
        if getattr(node, "id", None) is not None:
            own.add(node.id)
        return own

    @staticmethod
    def _open_v11_tap(device: str, traffic: V11Traffic) -> Optional[SocketcanTap]:
        """A listen-only socket the kernel passes only frames that may be Cyphal v1.1."""
        try:
            return SocketcanTap(device, socketcan_supports_fd(device), traffic.observe,
                                can_filters=SOCKETCAN_FILTER, name="v11-watch")
        except Exception as exc:  # only a notice is lost
            logger.warning("Cannot watch %s for Cyphal v1.1 traffic: %s", device, exc)
            return None

    async def _pick_node_id(self, local_spec: str, fd: bool = False) -> None:
        """Choose Cynitor's own node-ID from the heartbeats on the bus, unless one is set.

        The way `yakut accommodate` does it, without needing yakut: without a
        node-ID the scanner runs anonymously and can ask no node for its
        GetInfo or registers.
        """
        if "UAVCAN__NODE__ID" in os.environ:
            return
        node_id = await asyncio.to_thread(
            pick_free_node_id, local_spec, frozenset({ALLOCATOR_NODE_ID}), None, fd,
        )
        if node_id is None:
            logger.warning("Every node-ID is in use; the scanner will run anonymously")
        else:
            os.environ["UAVCAN__NODE__ID"] = str(node_id)

    async def _open_hub(self, spec: str, bitrate: int, force_compile: bool,
                        data_bitrate: Optional[int] = None, open_bus=None) -> CANHub:
        """Open a non-SocketCAN adapter once and point every component at the hub's channel.

        Most such adapters admit a single open handle, and the allocator probe,
        the allocator and the scanner each open the bus; see can_hub.
        """
        ensure_libusb_on_path()  # before gs_usb is opened, not just before yakut
        hub = CANHub(spec, bitrate, data_bitrate) if open_bus is None else CANHub(spec, bitrate, data_bitrate, open_bus)
        await asyncio.to_thread(hub.start)
        try:
            await self._pick_node_id(hub.local_spec)
            await asyncio.to_thread(
                prepare_runtime, hub.local_spec, force_compile, bitrate, False, data_bitrate,
            )
        except BaseException:
            await asyncio.to_thread(hub.stop)
            raise
        return hub

    async def disconnect(self) -> None:
        """Stop all CAN components (reverse order of connect)."""
        async with self._lock:
            await self._teardown()

    def dropped_events(self) -> Optional[dict[str, int]]:
        """Decoded events discarded because a queue was full, by where they were lost.

        ``scanner``: before reaching anything. ``logger``: missing from history
        and recordings. ``clients``: missing from a dashboard's live view.
        None when CAN is not connected.
        """
        if not self.is_running or not self.scanner or not self.telemetry:
            return None
        return {
            "scanner": self.scanner.dropped_events,
            "logger": self.telemetry.dropped["logger"]
                      + (self.event_logger.dropped_events if self.event_logger else 0),
            "clients": self.telemetry.dropped["client"],
        }

    @property
    def raw_log_folder(self) -> Path:
        return self.data_dir / RAW_LOG_DIR

    # ── Subject types the user sets, for subjects no register names ──

    GUESS_LISTEN_S = 3.0  # how long guess_subject_type listens for payloads

    @property
    def subject_types_file(self) -> Path:
        return self.data_dir / SUBJECT_TYPES_FILE

    def saved_subject_types(self) -> dict[int, str]:
        """{subject_id: DSDL type name} as saved in the data folder."""
        try:
            saved = json.loads(self.subject_types_file.read_text(encoding="utf-8"))
            return {int(sid): str(name) for sid, name in saved.items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def set_subject_type(self, subject_id: int, type_name: Optional[str]) -> None:
        """Decode ``subject_id`` as ``type_name`` from now on and in later sessions; None forgets it.

        RuntimeError if CAN is not connected or the subject's registers name
        its type; ValueError if the type is not a compiled message type.
        """
        if not self.is_running:
            raise RuntimeError("CAN is not connected")
        self.scanner.set_subject_type(subject_id, type_name)
        saved = self.saved_subject_types()
        if type_name is None:
            saved.pop(subject_id, None)
        else:
            saved[subject_id] = type_name
        try:
            self.subject_types_file.write_text(
                json.dumps({str(sid): name for sid, name in sorted(saved.items())}, indent=1), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not save the subject types: %s", exc)

    def _apply_saved_subject_types(self) -> None:
        for subject_id, type_name in self.saved_subject_types().items():
            try:
                self.scanner.set_subject_type(subject_id, type_name)
            except (RuntimeError, ValueError) as exc:
                logger.warning("Subject %d: not decoding it as %s, which was set earlier: %s",
                               subject_id, type_name, exc)

    async def guess_subject_type(self, subject_id: int, types: list[dict]) -> dict:
        """Listen to an undecoded subject and rank the ``types`` its payloads fit (see type_guess).

        {samples, matches, candidates: [{type, custom, preview}]}; no samples
        if nothing was published on it meanwhile.
        """
        if not self.is_running:
            raise RuntimeError("CAN is not connected")
        payloads = await self.scanner.sample_subject(subject_id, self.GUESS_LISTEN_S)
        if not payloads:
            return {"samples": 0, "matches": 0, "candidates": []}
        ranked = await asyncio.to_thread(lambda: rank_types(payloads, load_candidates(types), self.can_fd))
        return {"samples": len(payloads), **ranked}

    @property
    def firmware_folder(self) -> Path:
        return self.data_dir / FIRMWARE_DIR

    async def begin_firmware_update(self, node_id: int, name: str) -> dict:
        """Tell ``node_id`` to update its software from the uploaded file ``name``.

        FileNotFoundError for an unknown file; RuntimeError if nothing can be
        sent or the node refuses (its ExecuteCommand status); TimeoutError if
        it does not answer.
        """
        path = firmware_path(self.firmware_folder, name)
        if path is None or not path.is_file():
            raise FileNotFoundError(f"No firmware file {name}")
        if not self.is_running:
            raise RuntimeError("CAN is not connected")
        if self.firmware is None:
            raise RuntimeError("Cynitor cannot send here: a raw log is playing, or it has no node-ID")
        # Followed from before the command: the node may start reading at once.
        update = self.firmware.begin(node_id, name, path.stat().st_size)
        try:
            status = await send_update_command(self.scanner.node, node_id, name)
            if status is None:
                raise TimeoutError(f"Node {node_id} did not answer the update command")
            if status != 0:
                reason = COMMAND_STATUS[status] if status < len(COMMAND_STATUS) else f"status {status}"
                raise RuntimeError(f"Node {node_id} refused the update: {reason}")
        except BaseException:
            self.firmware.updates.pop(node_id, None)
            raise
        logger.info("Node %d is updating from %s (%d bytes)", node_id, name, update["bytes"])
        return update

    def start_raw_log(self) -> RawLog:
        """Start logging every frame on the bus to a new candump .log file.

        Behind the hub the frames come from its forwarding loops; on SocketCAN
        from a second, listen-only socket. RuntimeError if CAN is not connected
        or a log is already running.
        """
        if not self.is_running:
            raise RuntimeError("CAN is not connected")
        if self.raw_log is not None:
            raise RuntimeError("A raw log is already running")
        self.raw_log_folder.mkdir(parents=True, exist_ok=True)
        path = self.raw_log_folder / new_log_name()
        if self.hub is not None:
            log = RawLog(path, channel="can0")
            self.hub.add_listener(log.write)
        else:
            device = socketcan_device(self.can_interface)
            log = RawLog(path, channel=device)
            try:
                self._raw_tap = SocketcanTap(device, self.can_fd, log.write)
            except Exception:
                log.close()
                path.unlink(missing_ok=True)
                raise
        self.raw_log = log
        logger.info("Raw CAN log started: %s", path)
        return log

    def stop_raw_log(self) -> Optional[RawLog]:
        """Stop the raw log, if one runs, and return it."""
        log, self.raw_log = self.raw_log, None
        if log is None:
            return None
        if self.hub is not None:
            self.hub.remove_listener(log.write)
        if self._raw_tap is not None:
            self._raw_tap.stop()
            self._raw_tap = None
        log.close()
        if self.scanner is not None:
            write_sidecar(log.path, self._bus_knowledge())
        logger.info("Raw CAN log stopped: %s (%d frames)", log.path, log.frames)
        return log

    def _bus_knowledge(self) -> dict:
        """What this session knows about the bus that a raw log cannot hold, for playing it."""
        scanner = self.scanner
        publishers: dict[str, dict[str, str]] = {}
        for subject_id, node_ids in scanner.active_publishers.items():
            subject_type = scanner.subject_types.get(subject_id)
            for node_id in node_ids if subject_type else ():
                publishers.setdefault(str(node_id), {})[str(subject_id)] = subject_type
        names = {}
        for node_id, node in scanner.all_nodes.items():
            info = getattr(node, "info_response", None)
            if info is not None:
                names[str(node_id)] = info.name.tobytes().decode("utf-8", errors="replace")
        own = os.environ.get("UAVCAN__NODE__ID")
        return {
            "version": 1,
            "own_node_id": int(own) if own and own.isdigit() else None,
            "bitrate": self.can_bitrate,
            "data_bitrate": self.can_data_bitrate,
            "fd": self.can_fd,
            "publishers": publishers,
            "servers": {str(n): {str(s): t for s, t in types.items()}
                        for n, types in scanner.node_service_types.items()},
            "names": names,
        }

    async def play_raw_log(self, name: str, speed: float = 1.0) -> None:
        """Connect to a saved raw log as if it were the bus (see raw_log.LogPlayer).

        ``speed`` scales the logged pace; 0 plays as fast as frames can be read.
        The session ends when the log does. FileNotFoundError for an unknown log.
        """
        path = log_path(self.raw_log_folder, name)
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
        # Decode every subject the sidecar names from the first frame on,
        # rather than once each node has registered, which may be too late.
        for node_id, subjects in info.get("publishers", {}).items():
            try:
                await self.scanner.add_subscriptions(int(node_id), {int(s): t for s, t in subjects.items()})
            except Exception as exc:  # e.g. a custom type that is no longer compiled
                logger.warning("Raw log %s: cannot decode node %s's subjects: %s", name, node_id, exc)
        player.play()

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

    async def ensure_event_logger(self):
        """The event logger, started on first use.

        Recordings live in the data folder, so listing, exporting or replaying
        them needs it whether CAN is connected or not. A CAN disconnect stops
        it (flushing what it holds); the next use starts it again.
        """
        async with self._event_logger_lock:  # two first uses at once start one
            if self.event_logger is None:
                from event_logger import EventLogger
                event_logger = EventLogger(
                    db_path=self.data_dir / EVENTS_DB,
                    retention_seconds=86400.0,   # keep last 24h of bus traffic
                    max_events=5_000_000,        # safety cap; bounds disk
                )
                await event_logger.start()
                self.event_logger = event_logger
        return self.event_logger

    async def start_replay(self, recording_id: int, speed: float = 1.0,
                             start_offset_s: float = 0.0) -> dict:
        """Open a recording for playback. Refuses if CAN is connected or
        another replay is already running.
        """
        if self.is_running:
            raise RuntimeError("CAN is connected — disconnect before starting replay")
        if self.replay is not None:
            raise RuntimeError("Replay already in progress")
        await self.ensure_event_logger()
        from replay import ReplayManager
        self.replay = ReplayManager(self.event_logger.db_path,
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
        """Internal cleanup — caller must hold self._lock."""
        logger.info("Tearing down CAN session...")

        # Before the event logger it writes to, and the hub its tap listens to.
        if self.service_calls is not None:
            self.service_calls.stop()
        self.service_calls = None
        self.stop_raw_log()
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._tasks.clear()

        if self.event_logger and self.scanner:
            await self.event_logger.save_identity_map(self.scanner.identity_map.snapshot())

        if self.event_logger:
            await self.event_logger.stop()
            self.event_logger = None
        if self.telemetry:
            await self.telemetry.stop()
            self.telemetry = None
        if self.bus_load:
            await self.bus_load.stop()
            self.bus_load = None
        if self.allocator_manager:
            await self.allocator_manager.stop()
            self.allocator_manager = None
        # Before the hub and the scanner, which its tap listens to.
        if self.frame_capture is not None:
            self.frame_capture.stop()
        self.frame_capture = None
        self.firmware = None  # its server closes with the scanner's node
        if self.scanner:
            self.scanner.close()
            self.scanner = None
        if self._v11_tap is not None:
            self._v11_tap.stop()
            self._v11_tap = None
        self.v11 = None
        self.bus_errors = None
        # Last: everything above talks to the adapter through it.
        if self.hub:
            await asyncio.to_thread(self.hub.stop)
            self.hub = None
        self.registered_nodes.clear()
        self.can_interface = None
        self.can_bitrate = None
        self.can_data_bitrate = None
        self.can_fd = False

        logger.info("CAN session stopped")

    def schedule_fatal_disconnect(self, error_msg: str) -> None:
        """Schedule a disconnect due to a fatal CAN error (safe to call from background tasks)."""
        if self._disconnect_task and not self._disconnect_task.done():
            logger.warning("Disconnect already in progress, ignoring duplicate")
            return
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
    its subscriptions and clients were created. A failure (typically a lost
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

# The controller's state in `ip -details link show`: "can state ERROR-ACTIVE",
# or with the controller's modes between, as "can <FD> state ERROR-WARNING".
_CAN_STATE = re.compile(r"\bcan\s+(?:<[^>]*>\s+)?state\s+(\S+)")


def get_can_link_diagnostics(iface: str) -> dict:
    """Best-effort controller/bus diagnostics for a CAN interface.

    Parses ``ip -details -statistics link show <iface>`` plus sysfs operstate.
    SocketCAN-specific (Linux). All fields are optional — virtual interfaces
    (vcan) expose no CAN controller state, so most values come back ``None``.
    Never raises; returns whatever could be read.
    """
    result: dict = {
        "operstate": None, "state": None, "bitrate": None, "dbitrate": None,
        "berr_tx": None, "berr_rx": None, "restart_ms": None,
        "restarts": None, "bus_errors": None, "arbitration_lost": None,
        "error_warning": None, "error_passive": None, "bus_off": None,
    }
    if not IS_LINUX:
        return result

    try:
        result["operstate"] = (
            (Path("/sys/class/net") / iface / "operstate").read_text(encoding="utf-8").strip()
        )
    except Exception:
        pass

    try:
        proc = subprocess.run(
            ["ip", "-details", "-statistics", "link", "show", iface],
            check=False, capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return result
    if proc.returncode != 0:
        return result
    out = proc.stdout

    def _int(pattern: str, group: int = 1):
        m = re.search(pattern, out)
        return int(m.group(group)) if m else None

    m = _CAN_STATE.search(out)
    if m:
        result["state"] = m.group(1)
    result["bitrate"] = _int(r"\bbitrate\s+(\d+)")
    result["dbitrate"] = _int(r"\bdbitrate\s+(\d+)")
    result["restart_ms"] = _int(r"restart-ms\s+(\d+)")
    berr = re.search(r"berr-counter\s+tx\s+(\d+)\s+rx\s+(\d+)", out)
    if berr:
        result["berr_tx"], result["berr_rx"] = int(berr.group(1)), int(berr.group(2))

    # CAN error-state-change counters appear as a labelled header row followed
    # by the values, only with -statistics and only on real controllers.
    counters = re.search(
        r"re-started\s+bus-errors\s+arbit-lost\s+error-warn\s+error-pass\s+bus-off"
        r"\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
        out,
    )
    if counters:
        (result["restarts"], result["bus_errors"], result["arbitration_lost"],
         result["error_warning"], result["error_passive"], result["bus_off"]) = (
            int(counters.group(i)) for i in range(1, 7)
        )
    return result


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


async def _register_loop(scanner, registered_nodes: set[int], session: 'CANSession' = None) -> None:
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

            try:
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
            await event_logger.log_event(event)
    except asyncio.CancelledError:
        logger.debug("Event logger loop cancelled")
        raise


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
                "&& sudo ip link set up vcan0"
            )
        else:
            # Only SocketCAN is discovered so far, so off Linux the list is
            # always empty and the adapter has to be named explicitly.
            logger.warning(
                "CAN adapters are not listed automatically on this OS. Name the "
                "python-can interface and channel instead, e.g. gs_usb:0 "
                "(CANable/candleLight), pcan:PCAN_USBBUS1, slcan:COM5@115200 "
                "or kvaser:0, and set the bus speed with --bitrate."
            )
        logger.warning("Continuing in selection mode — pick an interface in the dashboard.")
        return False


def show_token_on_terminal(token: str, stream=None) -> bool:
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
               can_data_bitrate: Optional[int] = None) -> None:
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
                         default_data_bitrate=can_data_bitrate)
    project_root = resolve_project_root()
    dsdl_mgr = DsdlManager(project_root, data_dir=data_path)
    dsdl_mgr.make_importable()

    # Optional bearer-token auth. When CYNITOR_AUTH_TOKEN is set in the
    # environment, every REST/WS request outside /api/health must present
    # the token. When unset, the server runs open. Strongly recommended
    # whenever --bind 0.0.0.0 is used.
    auth_token = os.environ.get("CYNITOR_AUTH_TOKEN", "").strip() or None

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
    await ws_server.start()

    logger.info("=" * 60)
    logger.info("SERVER RUNNING")
    logger.info("=" * 60)
    logger.info("Bound to:    %s", bound_address(bind, port))
    logger.info("Auth:        %s", "token required (CYNITOR_AUTH_TOKEN set)" if auth_token else "OPEN (no token)")
    logger.info("Data:        %s", data_path)
    show_token_on_terminal(auth_token)
    if bind == "0.0.0.0" and not auth_token:
        logger.warning("Server is bound to 0.0.0.0 with NO auth — reachable from any network peer. Set CYNITOR_AUTH_TOKEN to require a bearer token.")
    elif bind == "0.0.0.0":
        logger.info("Server is bound to 0.0.0.0 with token auth — clients must present Authorization: Bearer <token>.")
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
    logger.info("Startup options:")
    logger.info("  --can <iface>    attach to a CAN interface at startup (e.g. vcan0, can0, gs_usb:0, pcan:PCAN_USBBUS1)")
    logger.info("  --bitrate <n>    bus speed in bit/s; required with --can for any adapter except SocketCAN")
    logger.info("  --data-bitrate <n>  CAN FD data-phase speed; opens PCAN/Kvaser/Vector/IXXAT as CAN FD")
    logger.info("  --bind <host>    bind HTTP server to <host>  (default 127.0.0.1; 0.0.0.0 to expose on the network)")
    logger.info("  --port <n>       listen on <n> instead of 8080")
    logger.info("  --data-dir <dir> keep history, recordings and node-IDs in <dir>")
    logger.info("  --recompile      force DSDL recompilation via nnvg")
    logger.info("  --no-frontend    serve only the API and WebSocket, not the dashboard")
    logger.info("  --help           full reference")
    if not can_iface:
        logger.info("Selection-mode startup (no --can): connect from the UI or POST /api/can/connect")
    logger.info("=" * 60)

    try:
        if can_iface:
            await attach_or_fall_back(session, can_iface, force_compile)

        await asyncio.Event().wait()

    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
    finally:
        logger.info("Cleanup starting...")
        if session.is_running:
            await session.disconnect()
        await ws_server.stop()
        logger.info("Shutdown complete")


def _shutdown_on_sigterm(_signum, _frame) -> None:
    """Turn SIGTERM into the interrupt the entry point already handles.

    Without this, SIGTERM kills the process outright and `main`'s finally
    block never runs, so the CAN session and HTTP server are not closed down.
    """
    raise KeyboardInterrupt


def _exit_when_parent_dies() -> None:
    """Ask the kernel to signal us when our parent process goes away.

    The packaged server is a PyInstaller single-file binary: a bootloader
    parent with this interpreter as its child. A SIGKILL to the bootloader
    cannot be forwarded, so without this the interpreter outlives whatever
    started it and keeps holding port 8080. The next start would then find a
    stale server answering on the port it wanted.

    Linux and Windows (see _exit_when_windows_parent_dies); a no-op elsewhere.
    """
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
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        parent = kernel32.OpenProcess(SYNCHRONIZE, False, os.getppid())
    except Exception as exc:
        logger.debug("Parent-death cleanup unavailable: %s", exc)
        return
    if not parent:
        logger.debug("Parent-death cleanup unavailable: OpenProcess failed (%d)", ctypes.get_last_error())
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
        help="CAN interface: a SocketCAN name (vcan0, can0) or a python-can spec "
             "such as gs_usb:0, pcan:PCAN_USBBUS1, slcan:COM5@115200 "
             "(if omitted, select from the UI)",
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
             "Kvaser, Vector, IXXAT). Leave out for Classic CAN. Ignored for SocketCAN, "
             "which runs CAN FD when the interface is set up for it.",
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
        help="Folder for history, recordings and the allocator's node-ID table "
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
    args = parser.parse_args()
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
        ))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
