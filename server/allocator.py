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

from can_config import ALLOCATOR_NODE_ID, media_bitrate, normalize_can_iface


logger = logging.getLogger(__name__)



def _iface_from_env() -> str:
    return os.environ["UAVCAN__CAN__IFACE"].strip()


def _get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class AllocatorApp:
    REGISTER_FILE = "allocator_2app.db"
    NODE_ID = ALLOCATOR_NODE_ID

    def __init__(self, iface_name: Optional[str] = None, register_file: str = REGISTER_FILE) -> None:
        node_info = uavcan.node.GetInfo_1_0.Response(
            software_version=uavcan.node.Version_1(major=1, minor=0),
            name=f"{_get_local_ip()}.allocator",
        )

        can_interface = normalize_can_iface(iface_name or _iface_from_env())
        media = PythonCANMedia(iface_name=can_interface, bitrate=media_bitrate(can_interface))
        transport = CANTransport(media, local_node_id=AllocatorApp.NODE_ID)
        self.registry = pycyphal.application.make_registry(register_file)
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
    can_interface = normalize_can_iface(iface_name or _iface_from_env())
    media = PythonCANMedia(iface_name=can_interface, bitrate=media_bitrate(can_interface))
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
    def __init__(self, check_interval: float = 10.0, check_timeout: float = 3.0,
                 register_file: str = AllocatorApp.REGISTER_FILE) -> None:
        self._check_interval = check_interval
        # The allocator's node-ID table; see data_dir.
        self._register_file = register_file
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
            self._start_local_allocator()

        self._task = asyncio.create_task(self._monitor_loop())

    def _start_local_allocator(self) -> None:
        # NOTE: This must run on the asyncio main thread, not a worker.
        # AllocatorApp -> CANTransport -> PythonCANMedia.start() calls
        # asyncio.get_event_loop() in its calling thread (to capture the loop
        # for its background reader). On Python 3.10+ get_event_loop() raises
        # in a worker thread that has no associated loop, so wrapping this in
        # asyncio.to_thread breaks /api/can/connect for the local-allocator
        # path. Constructor latency is bounded (subprocess thread spawn) so
        # the brief loop occupancy is acceptable here.
        if self._allocator is None:
            self._allocator = AllocatorApp(iface_name=self._iface_name, register_file=self._register_file)

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
                    self._start_local_allocator()
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
