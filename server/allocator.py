#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
import asyncio
import logging
import hashlib
from typing import Optional

import pycyphal
import pycyphal.application

import uavcan.node
from uavcan.node import Heartbeat_1_0

from pycyphal.transport.can import CANTransport
from pycyphal.transport.can.media.pythoncan import PythonCANMedia
from pycyphal.application.plug_and_play import CentralizedAllocator


logger = logging.getLogger(__name__)



def _iface_from_env() -> str:
    return os.environ["UAVCAN__CAN__IFACE"].strip()


def _normalize_pythoncan_iface(iface: str) -> str:
    value = iface.strip()
    if ":" in value:
        return value
    return f"socketcan:{value}"


def _get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class AllocatorApp:
    REGISTER_FILE = "allocator_2app.db"
    NODE_ID = 1

    def __init__(self, iface_name: Optional[str] = None) -> None:
        node_info = uavcan.node.GetInfo_1_0.Response(
            software_version=uavcan.node.Version_1(major=1, minor=0),
            name=f"{_get_local_ip()}.allocator",
        )

        can_interface = _normalize_pythoncan_iface(iface_name or _iface_from_env())
        can_bitrate = 500000
        media = PythonCANMedia(iface_name=can_interface, bitrate=int(can_bitrate))
        transport = CANTransport(media, local_node_id=AllocatorApp.NODE_ID)
        self.registry = pycyphal.application.make_registry(AllocatorApp.REGISTER_FILE)
        self.node = pycyphal.application.make_node(info=node_info, transport=transport, registry=self.registry)
        try:
            self.allocator = CentralizedAllocator(node=self.node)
            self.heartbeat_publisher = self.node.make_publisher(Heartbeat_1_0, "uavcan.node.heartbeat")
            self.node.heartbeat_publisher.mode = uavcan.node.Mode_1.OPERATIONAL
            self.node.heartbeat_publisher.vendor_specific_status_code = os.getpid() % 100
            self.node.start()
        except Exception:
            self.node.close()
            raise
        logger.info("Local allocator started on %s with node-ID %d", can_interface, AllocatorApp.NODE_ID)

    def close(self) -> None:
        """Close allocator resources."""
        self.node.close()
        logger.info("Local allocator stopped")


async def allocator_exists(
    iface_name: Optional[str] = None,
    node_id: int = AllocatorApp.NODE_ID,
    timeout: float = 3.0,
) -> bool:
    media = PythonCANMedia(
        iface_name=_normalize_pythoncan_iface(iface_name or _iface_from_env()),
        bitrate=500000,
    )
    transport = CANTransport(media, local_node_id=None)
    registry = pycyphal.application.make_registry()
    probe = pycyphal.application.make_node(
        info=uavcan.node.GetInfo_1_0.Response(),
        transport=transport,
        registry=registry,
    )
    subscriber = probe.make_subscriber(uavcan.node.Heartbeat_1_0)
    probe.start()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    try:
        while loop.time() < deadline:
            result = await subscriber.receive(monotonic_deadline=deadline)
            if result is None:
                continue
            _, meta = result
            if meta.source_node_id == node_id:
                return True
        return False
    finally:
        subscriber.close()
        probe.close()


class AllocatorManager:
    def __init__(self, check_interval: float = 10.0, check_timeout: float = 3.0) -> None:
        self._check_interval = check_interval
        self._check_timeout = check_timeout
        self._allocator: Optional[AllocatorApp] = None
        self._task: Optional[asyncio.Task] = None
        self._iface_name = _iface_from_env()

    async def start(self) -> None:
        external_exists = await allocator_exists(
            iface_name=self._iface_name,
            timeout=self._check_timeout,
        )
        if external_exists:
            logger.info("External allocator detected on %s (node-ID %d)", self._iface_name, AllocatorApp.NODE_ID)
        else:
            logger.warning("No allocator detected on %s; starting local allocator", self._iface_name)
            await self._start_local_allocator()

        self._task = asyncio.create_task(self._monitor_loop())

    async def _start_local_allocator(self) -> None:
        if self._allocator is None:
            # AllocatorApp.__init__ opens SQLite, configures CAN media/transport,
            # and calls node.start() — all synchronous and potentially slow.
            # Run on a worker thread so we don't stall the event loop.
            self._allocator = await asyncio.to_thread(AllocatorApp, self._iface_name)

    async def _monitor_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._check_interval)
                if self._allocator is not None:
                    continue

                external_exists = await allocator_exists(
                    iface_name=self._iface_name,
                    timeout=self._check_timeout,
                )
                if not external_exists:
                    logger.warning("External allocator disappeared; starting local allocator")
                    await self._start_local_allocator()
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

        if self._allocator is not None:
            self._allocator.close()
            self._allocator = None


async def main() -> None:
    logging.root.setLevel(logging.INFO)
    manager = AllocatorManager(check_interval=10.0, check_timeout=3.0)
    try:
        await manager.start()
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await manager.stop()


if __name__ == "__main__":
    asyncio.run(main())
