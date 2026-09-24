#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

from can_config import (
    ALLOCATOR_NODE_ID,
    is_socketcan,
    normalize_can_iface,
    resolve_bitrate,
    socketcan_device,
    validate_bitrate,
)
from can_discovery import AdapterCatalog, discover_adapters
from can_hub import CANHub, HubBusLoad, pick_free_node_id
from log_store import InMemoryLogStore, APILogHandler
from startup_setup import ensure_libusb_on_path, prepare_runtime, resolve_project_root
from version import __version__

IS_LINUX = sys.platform.startswith("linux")

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

def _get_can_bitrate(iface: str) -> int:
    """Get the bitrate of a physical CAN interface. Returns 500000 as default."""
    try:
        result = subprocess.run(
            ["ip", "-details", "link", "show", iface],
            check=False, capture_output=True, text=True,
        )
        if result.returncode == 0:
            match = re.search(r"bitrate\s+(\d+)", result.stdout)
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return 500000


class BusLoadMonitor:
    """Runs canbusload as a subprocess and exposes the latest utilization %.

    `canbusload` ships with Linux `can-utils`. If it is not on PATH (any non-
    Linux OS, or a minimal Linux install) the monitor becomes a permanent
    no-op: utilization stays 0, `is_alive` reports True so the health watchdog
    in `_register_loop` does not trip a disconnect.
    """

    def __init__(self, iface: str) -> None:
        self._iface = iface
        self._bitrate = _get_can_bitrate(iface)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._disabled = shutil.which("canbusload") is None
        self.utilization: float = 0.0

    async def start(self) -> None:
        if self._disabled:
            logger.info("BusLoadMonitor disabled: 'canbusload' not on PATH (install can-utils on Linux for bus-load metrics)")
            return
        self._proc = await asyncio.create_subprocess_exec(
            "canbusload", f"{self._iface}@{self._bitrate}", "-b",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._task = asyncio.create_task(self._read_loop())
        logger.info("BusLoadMonitor started on %s@%d", self._iface, self._bitrate)

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
        self.utilization = 0.0
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


# ---------------------------------------------------------------------------
# CAN session lifecycle
# ---------------------------------------------------------------------------

class CANSession:
    """Manages the lifecycle of all CAN-dependent components."""

    def __init__(self, default_bitrate: Optional[int] = None) -> None:
        # --bitrate, used when connect() is not given one. There is no built-in
        # default: a guessed bitrate can disrupt the bus.
        self.default_bitrate = None if default_bitrate is None else validate_bitrate(default_bitrate)
        self.can_interface: Optional[str] = None
        self.can_bitrate: Optional[int] = None
        self.scanner = None
        self.telemetry = None
        self.allocator_manager = None
        self.event_logger = None
        self.frame_capture = None
        self.bus_load: Optional[BusLoadMonitor] = None
        # Shares a non-SocketCAN adapter among the components; see can_hub.
        self.hub: Optional[CANHub] = None
        self.registered_nodes: set[int] = set()
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
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
                      bitrate: Optional[int] = None) -> None:
        """Start all CAN components.

        `bitrate` is ignored by SocketCAN (set it with `ip link`) and required
        for every other interface; None means `default_bitrate`. ValueError is
        raised before anything is opened if it is missing or invalid.
        """
        bitrate = resolve_bitrate(can_iface, self.default_bitrate if bitrate is None else bitrate)
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
        else:
            logger.info("Connecting to %s at %d bit/s", spec, bitrate)

        # prepare_runtime can take seconds (DSDL compile via nnvg, env setup).
        # Running it outside the lock keeps a concurrent disconnect() responsive
        # instead of blocking it behind a cold-start connect.
        hub: Optional[CANHub] = None
        if is_socketcan(spec):
            await asyncio.to_thread(prepare_runtime, can_iface, force_compile, bitrate)
        else:
            hub = await self._open_hub(spec, bitrate, force_compile)

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
                from event_logger import EventLogger

                logger.info("Initializing allocator manager...")
                self.allocator_manager = AllocatorManager(check_interval=10.0, check_timeout=3.0)
                await self.allocator_manager.start()

                logger.info("Initializing ScannerNode...")
                self.scanner = ScannerNode()

                logger.info("Initializing TelemetryManager...")
                self.telemetry = TelemetryManager(self.scanner)
                await self.telemetry.start()

                # Frame-capture tap is created up front but stays dormant until a
                # Debugging-view client explicitly starts it (it changes bus
                # behaviour, so it is never auto-enabled).
                from frame_capture import FrameCaptureManager
                self.frame_capture = FrameCaptureManager(self.scanner)

                logger.info("Initializing EventLogger...")
                self.event_logger = EventLogger(
                    db_path="telemetry_events.db",
                    retention_seconds=86400.0,   # keep last 24h of bus traffic
                    max_events=5_000_000,        # safety cap; bounds disk
                )
                await self.event_logger.start()

                saved_identities = await self.event_logger.load_identity_map()
                if saved_identities:
                    self.scanner.identity_map.load_snapshot(saved_identities)

                saved_node_data = await self.event_logger.load_all_node_data()
                if saved_node_data:
                    self.scanner.identity_map.load_node_data(saved_node_data)

                self.scanner.on_node_event = self.event_logger.log_node_event
                self.scanner.on_node_data_save = self.event_logger.save_node_data

                logger_queue = self.telemetry.subscribe(max_queue=100)
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

                self.can_interface = can_iface
                self.can_bitrate = bitrate
                logger.info("CAN session started on %s", can_iface)
            except Exception:
                try:
                    await self._teardown()
                except Exception as cleanup_err:
                    logger.error("Cleanup error during failed connect: %s", cleanup_err)
                raise

    async def _open_hub(self, spec: str, bitrate: int, force_compile: bool) -> CANHub:
        """Open a non-SocketCAN adapter once and point every component at the hub's channel.

        Most such adapters admit a single open handle, and the allocator probe,
        the allocator and the scanner each open the bus; see can_hub.
        """
        ensure_libusb_on_path()  # before gs_usb is opened, not just before yakut
        hub = CANHub(spec, bitrate)
        await asyncio.to_thread(hub.start)
        try:
            # `yakut accommodate` would run in a child process, which cannot
            # see the hub's in-process channel, so pick the node-ID here.
            if "UAVCAN__NODE__ID" not in os.environ:
                node_id = await asyncio.to_thread(
                    pick_free_node_id, hub.local_spec, frozenset({ALLOCATOR_NODE_ID}),
                )
                if node_id is None:
                    logger.warning("Every node-ID is in use; the scanner will run anonymously")
                else:
                    os.environ["UAVCAN__NODE__ID"] = str(node_id)
            await asyncio.to_thread(
                prepare_runtime, hub.local_spec, force_compile, bitrate, False,
            )
        except BaseException:
            await asyncio.to_thread(hub.stop)
            raise
        return hub

    async def disconnect(self) -> None:
        """Stop all CAN components (reverse order of connect)."""
        async with self._lock:
            await self._teardown()

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

    async def start_replay(self, recording_id: int, speed: float = 1.0,
                             start_offset_s: float = 0.0) -> dict:
        """Open a recording for playback. Refuses if CAN is connected or
        another replay is already running.
        """
        if self.is_running:
            raise RuntimeError("CAN is connected — disconnect before starting replay")
        if self.replay is not None:
            raise RuntimeError("Replay already in progress")
        if self.event_logger is None:
            # event_logger lives on the session and gets torn down on disconnect.
            # When CAN has never been connected this session it doesn't exist yet,
            # so we create a transient one bound to the same DB.
            from event_logger import EventLogger
            self.event_logger = EventLogger(db_path="telemetry_events.db",
                                            retention_seconds=86400.0,
                                            max_events=5_000_000)
            await self.event_logger.start()
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
        # Capture ends implicitly when the transport closes in scanner.close().
        self.frame_capture = None
        if self.scanner:
            self.scanner.close()
            self.scanner = None
        # Last: everything above talks to the adapter through it.
        if self.hub:
            await asyncio.to_thread(self.hub.stop)
            self.hub = None
        self.registered_nodes.clear()
        self.can_interface = None
        self.can_bitrate = None

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

async def register_nodes(scanner, registered_nodes_set: set[int]) -> None:
    """Register newly discovered nodes and clean up disappeared ones."""
    try:
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

            registered_nodes_set.add(node.node_id)
            dsdl_pub_messages, dsdl_srv_messages = await scanner.update_reg_list(node.node_id)
            scanner.node_service_types[node.node_id] = dict(dsdl_srv_messages)
            await scanner.add_subscriptions(node.node_id, dsdl_pub_messages)
            await scanner.add_servers(node.node_id, dsdl_srv_messages)
            logger.info(
                f"Node {node.node_id} registered with publishers: {dsdl_pub_messages} "
                f"and servers: {dsdl_srv_messages}"
            )

    except Exception as e:
        logger.error(f"Error in register_nodes: {str(e)}")
        raise


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

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

    m = re.search(r"can state\s+(\S+)", out)
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
            match = re.search(r"can state\s+(\S+)", result.stdout)
            if match:
                can_state = match.group(1)
                if can_state in ("BUS-OFF", "STOPPED", "ERROR-PASSIVE"):
                    return f"Interface {iface}: CAN state is {can_state}"
    except Exception:
        pass

    return None


async def _session_health_error(session: 'CANSession') -> Optional[str]:
    """Why the session's CAN link has become unusable, or None if it is fine.

    An adapter behind the hub reports through the hub, which also notices a
    CANable being unplugged; SocketCAN is checked through the kernel, by
    device name rather than spec.
    """
    if session.hub is not None:
        return await asyncio.to_thread(session.hub.health)
    error = await asyncio.to_thread(_check_can_health, socketcan_device(session.can_interface))
    if not error and session.bus_load and not session.bus_load.is_alive:
        error = "CAN bus monitor process exited unexpectedly"
    return error


async def _register_loop(scanner, registered_nodes: set[int], session: 'CANSession' = None) -> None:
    health_check_counter = 0
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

            try:
                for node in scanner.all_nodes.values():
                    node.check_disappeared()
                await register_nodes(scanner, registered_nodes)
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

async def main(can_iface: Optional[str] = None, force_compile: bool = False, bind: str = "127.0.0.1",
               port: int = 8080, serve_frontend: bool = True,
               can_bitrate: Optional[int] = None) -> None:
    from websocket_server import WebSocketServer
    from dsdl_manager import DsdlManager

    session = CANSession(default_bitrate=can_bitrate)
    project_root = resolve_project_root()
    dsdl_mgr = DsdlManager(project_root)

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
    logger.info("Bound to:    %s:%d", bind, port)
    logger.info("Auth:        %s", "token required (CYNITOR_AUTH_TOKEN set)" if auth_token else "OPEN (no token)")
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
    logger.info("  --bind <host>    bind HTTP server to <host>  (default 127.0.0.1; 0.0.0.0 to expose on the network)")
    logger.info("  --port <n>       listen on <n> instead of 8080")
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

    Linux only; a no-op elsewhere.
    """
    if not IS_LINUX:
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
        ))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
