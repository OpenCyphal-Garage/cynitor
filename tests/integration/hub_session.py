#!/usr/bin/env python3
"""Integration test: a whole CAN session through the CAN hub, without hardware.

The unit tests in server/tests mock the Cyphal stack. This runs the real one:
compiled DSDL, pycyphal, the allocator, the scanner and the hub, on a python-can
`virtual` bus that stands in for an adapter. It is the path every adapter other
than SocketCAN takes -- the whole Windows path -- so it runs on every OS.

On the simulated bus:
  - an ordinary node (node-ID 50) publishing heartbeats and answering GetInfo;
  - an anonymous node asking for a node-ID, as a freshly powered device does.

Checks, in order:
  1. the session connects through the hub, and Cynitor picks itself a node-ID;
  2. the scanner sees node 50 and its GetInfo name;
  3. Cynitor's allocator gives the anonymous node a node-ID;
  4. bus load is measured by the hub, not canbusload;
  5. an adapter that disappears ends the session with an error;
  6. the databases are written to the session's data folder.

With --fd the device and Cynitor run Cyphal/CAN FD (500 kbit/s, 2 Mbit/s data
phase), and Cynitor's own frames on the wire must be CAN FD frames.

Prerequisites (from the repository root):
    pip install -r server/requirements.txt
    python server/startup_setup.py --recompile     # compiled DSDL

Usage:
    python tests/integration/hub_session.py [--fd]

Exits non-zero on the first failed check. Runs in a temporary directory, so
the databases a session writes do not land in the checkout.
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "python_compiled_messages")]

WIRE = "integration-wire"
BITRATE = 500_000
FD = "--fd" in sys.argv[1:]
DATA_BITRATE = 2_000_000 if FD else None
DEVICE_NODE_ID = 50
TIMEOUT = 30.0


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)
    print(f"ok:   {message}")


async def wait_for(predicate, timeout: float = TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.2)
    return predicate()


async def first_frame_from(tap, node_id: int, timeout: float = 10.0):
    """The next frame on the wire sent by ``node_id``, or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = await asyncio.to_thread(tap.recv, 0.2)  # blocking; keep the loop running
        if msg is not None and msg.is_extended_id and msg.arbitration_id & 0x7F == node_id:
            return msg
    return None


async def run() -> None:
    try:
        import uavcan.node  # noqa: F401  (compiled DSDL)
    except ImportError:
        print("Compiled DSDL not found. Run: python server/startup_setup.py --recompile")
        sys.exit(2)

    import can
    import pycyphal.application
    import pycyphal.presentation
    import uavcan.node
    from pycyphal.application.plug_and_play import Allocatee
    from pycyphal.transport.can import CANTransport
    from pycyphal.transport.can.media.pythoncan import PythonCANMedia

    from can_hub import HubBusLoad
    from main import CANSession

    def on_wire(node_id):
        if FD:
            media = PythonCANMedia(f"virtual:{WIRE}", (BITRATE, DATA_BITRATE), 64)
        else:
            media = PythonCANMedia(f"virtual:{WIRE}", BITRATE)
        return CANTransport(media, local_node_id=node_id)

    device = pycyphal.application.make_node(
        uavcan.node.GetInfo_1_0.Response(name="integration.device"),
        transport=on_wire(DEVICE_NODE_ID),
        registry=pycyphal.application.make_registry(None, environment_variables={}),
    )
    device.start()
    anonymous = pycyphal.presentation.Presentation(on_wire(None))
    allocatee = Allocatee(anonymous, bytes(range(16)))

    data = Path.cwd() / "data"
    data.mkdir()
    session = CANSession(data_dir=data)
    tap = can.Bus(interface="virtual", channel=WIRE)  # what an analyzer on the bus would see
    try:
        await session.connect(f"virtual:{WIRE}", bitrate=BITRATE, data_bitrate=DATA_BITRATE)
        check(session.is_running and session.hub is not None, "connected through the CAN hub")
        own_id = os.environ.get("UAVCAN__NODE__ID")
        check(own_id is not None and int(own_id) not in (1, DEVICE_NODE_ID),
              f"picked its own node-ID ({own_id}), clear of the allocator and the device")
        check(session.can_fd == FD, f"session runs {'CAN FD' if FD else 'Classic CAN'}")
        own_frame = await first_frame_from(tap, int(own_id))
        check(own_frame is not None and own_frame.is_fd == FD,
              f"Cynitor's own frames on the wire are {'CAN FD' if FD else 'Classic CAN'}")

        seen = await wait_for(lambda: session.scanner.all_nodes[DEVICE_NODE_ID].has_responded_to_getInfo)
        check(seen, f"scanner sees node {DEVICE_NODE_ID} and its GetInfo")
        name = session.scanner.all_nodes[DEVICE_NODE_ID].info_response.name.tobytes().decode()
        check(name == "integration.device", f"GetInfo name is {name!r}")

        allocated = await wait_for(lambda: allocatee.get_result() is not None)
        check(allocated, f"anonymous node was allocated node-ID {allocatee.get_result()}")

        check(isinstance(session.bus_load, HubBusLoad), "bus load comes from the hub")
        loaded = await wait_for(lambda: session.bus_load.utilization > 0, timeout=5.0)
        check(loaded, f"bus load measured ({session.bus_load.utilization} %)")

        session.hub._still_present = lambda: False  # the adapter goes away
        gone = await wait_for(lambda: not session.is_running, timeout=10.0)
        check(gone, "an adapter that disappears ends the session")
        check("adapter disconnected" in (session.last_error or ""),
              f"with a reason: {session.last_error!r}")

        written = sorted(p.name for p in data.glob("*.db"))
        check(written == ["allocator_2app.db", "monitor_app.db", "telemetry_events.db"],
              f"databases written to the data folder: {written}")
    finally:
        if session.is_running:
            await session.disconnect()
        tap.shutdown()
        allocatee.close()
        anonymous.close()
        device.close()


def main() -> None:
    # Where nnvg (compiling DSDL on a cold start) and the session's databases go.
    scripts = Path(sys.executable).parent
    os.environ["PATH"] = os.pathsep.join([str(scripts), os.environ.get("PATH", "")])
    # Windows cannot delete the current directory, or files still held open.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workdir:
        previous = os.getcwd()
        os.chdir(workdir)
        try:
            asyncio.run(run())
        finally:
            os.chdir(previous)
    print("All integration checks passed.")


if __name__ == "__main__":
    main()
