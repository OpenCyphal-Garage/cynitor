#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from log_store import InMemoryLogStore, APILogHandler
from startup_setup import prepare_runtime

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
    """Runs canbusload as a subprocess and exposes the latest utilization %."""

    def __init__(self, iface: str) -> None:
        self._iface = iface
        self._bitrate = _get_can_bitrate(iface)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self.utilization: float = 0.0

    async def start(self) -> None:
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

    def __init__(self) -> None:
        self.can_interface: Optional[str] = None
        self.scanner = None
        self.telemetry = None
        self.allocator_manager = None
        self.event_logger = None
        self.bus_load: Optional[BusLoadMonitor] = None
        self.registered_nodes: list[int] = []
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._disconnect_task: Optional[asyncio.Task] = None
        self.last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self.scanner is not None

    async def connect(self, can_iface: str, force_compile: bool = False) -> None:
        """Start all CAN components."""
        async with self._lock:
            if self.is_running:
                raise RuntimeError("Already connected")
            self.last_error = None

            try:
                await asyncio.to_thread(prepare_runtime, can_iface, force_compile)

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

                logger.info("Initializing EventLogger...")
                self.event_logger = EventLogger(db_path="telemetry_events.db", max_events=100000)
                await self.event_logger.start()

                self.scanner.on_node_event = self.event_logger.log_node_event

                logger_queue = self.telemetry.subscribe(max_queue=100)
                self._tasks = [
                    asyncio.create_task(_event_logger_loop(self.event_logger, logger_queue)),
                    asyncio.create_task(_register_loop(self.scanner, self.registered_nodes, self)),
                ]

                logger.info("Initializing BusLoadMonitor...")
                self.bus_load = BusLoadMonitor(can_iface)
                await self.bus_load.start()

                self.can_interface = can_iface
                logger.info("CAN session started on %s", can_iface)
            except Exception:
                try:
                    await self._teardown()
                except Exception as cleanup_err:
                    logger.error("Cleanup error during failed connect: %s", cleanup_err)
                raise

    async def disconnect(self) -> None:
        """Stop all CAN components (reverse order of connect)."""
        async with self._lock:
            await self._teardown()

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
        if self.scanner:
            self.scanner.close()
            self.scanner = None
        self.registered_nodes.clear()
        self.can_interface = None

        logger.info("CAN session stopped")

    def schedule_fatal_disconnect(self, error_msg: str) -> None:
        """Schedule a disconnect due to a fatal CAN error (safe to call from background tasks)."""
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

async def register_nodes(scanner, registered_nodes_list: list[int]) -> None:
    """Register newly discovered nodes and clean up disappeared ones."""
    try:
        for node in scanner.nodes.values():
            if node.has_disappeared and node.node_id in registered_nodes_list:
                registered_nodes_list.remove(node.node_id)
                scanner.cleanup_subscriptions(node.node_id)
                logger.info(f"Node {node.node_id} was removed from the node registration list")
                continue

            if not node.has_appeared or not node.has_registered_ports:
                continue

            if node.node_id in registered_nodes_list or node.has_disappeared:
                continue

            registered_nodes_list.append(node.node_id)
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

def _check_can_health(iface: str) -> Optional[str]:
    """Check CAN interface health via system. Returns error message or None."""
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


async def _register_loop(scanner, registered_nodes: list[int], session: 'CANSession' = None) -> None:
    health_check_counter = 0
    try:
        while True:
            await asyncio.sleep(1)

            health_check_counter += 1
            if session and session.can_interface and health_check_counter >= 3:
                health_check_counter = 0

                error = await asyncio.to_thread(_check_can_health, session.can_interface)
                if not error and session.bus_load and not session.bus_load.is_alive:
                    error = "CAN bus monitor process exited unexpectedly"

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(can_iface: Optional[str] = None, force_compile: bool = False) -> None:
    from websocket_server import WebSocketServer

    session = CANSession()

    ws_server = WebSocketServer(
        session=session,
        host="0.0.0.0",
        port=8080,
        log_store=_log_store,
    )
    await ws_server.start()

    logger.info("=" * 60)
    logger.info("SERVER RUNNING")
    logger.info("=" * 60)
    logger.info("REST API:    http://localhost:8080/api/")
    logger.info("Health:      http://localhost:8080/api/health")
    logger.info("Status:      http://localhost:8080/api/status")
    logger.info("=" * 60)

    try:
        if can_iface:
            await session.connect(can_iface, force_compile=force_compile)

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the telemetry server")
    parser.add_argument(
        "--can",
        default=None,
        help="CAN interface name (if omitted, select from the UI)",
    )
    parser.add_argument(
        "--recompile",
        action="store_true",
        help="Force DSDL recompilation even if already compiled",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(can_iface=args.can, force_compile=args.recompile))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
