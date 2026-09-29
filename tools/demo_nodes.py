#!/usr/bin/env python3
"""Demo Cyphal nodes for trying Cynitor without hardware.

    python3 tools/demo_nodes.py [--iface vcan0] [--fd] [--conflict]

Run it next to Cynitor on the same virtual CAN interface (Linux):

    sudo ip link add dev vcan0 type vcan && sudo ip link set vcan0 up

It needs the compiled DSDL, which Cynitor makes on its first start
(or: python3 server/startup_setup.py --recompile).

Node 50, "demo.sensor":
  - publishes uavcan.si.sample.temperature.Scalar.1.0 on subject 1620 at 10 Hz,
    named in its registers, so Cynitor decodes and can plot it;
  - publishes uavcan.si.unit.velocity.Vector3.1.0 on subject 1700 at 5 Hz with
    no register naming it, so Cynitor cannot decode it until you set its type
    (Subjects view: click it, and pick from the types its messages fit);
  - publishes uavcan.primitive.String.1.0 on subject 1800, its state as text
    every 2 s, named in its registers: add it to the log panel with its +;
  - publishes a uavcan.diagnostic.Record every 3 s, going through every
    severity from DEBUG to CRITICAL (the log panel);
  - serves uavcan.node.ExecuteCommand: Restart really restarts it (its uptime
    starts over), Factory reset only answers success, and Update firmware
    acts like a bootloader: it restarts in SOFTWARE_UPDATE mode, reads the
    file from Cynitor with uavcan.file.Read (slowly, so the progress shows),
    and comes back with its software version's minor number one higher.

--conflict starts a second node on the same node-ID 50 after 10 s, which
Cynitor should flag as a node-ID conflict. --fd runs Cyphal/CAN FD; the
interface must be set up for it (e.g. a vcan with mtu 72).
"""

import argparse
import asyncio
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python_compiled_messages"))
try:
    import uavcan.node  # noqa: F401  (compiled DSDL)
except ImportError:
    sys.exit("Compiled DSDL not found: start Cynitor once, or run python3 server/startup_setup.py --recompile")

import pycyphal.application  # noqa: E402
import uavcan.diagnostic  # noqa: E402
import uavcan.file  # noqa: E402
import uavcan.node  # noqa: E402
import uavcan.primitive  # noqa: E402
import uavcan.si.sample.temperature  # noqa: E402
import uavcan.si.unit.velocity  # noqa: E402

NODE_ID = 50
TEMPERATURE_SUBJECT_ID = 1620
UNNAMED_SUBJECT_ID = 1700
STATUS_SUBJECT_ID = 1800
STATES = ["idle", "heating", "holding", "cooling"]
Severity = uavcan.diagnostic.Severity_1_0
# (severity, message) in turn, one every 3 s: every level the log panel shows.
DIAGNOSTICS = [
    (Severity.INFO, "temperature {celsius:.1f} C"),
    (Severity.DEBUG, "adc raw {adc}"),
    (Severity.NOTICE, "calibration table loaded"),
    (Severity.INFO, "temperature {celsius:.1f} C"),
    (Severity.WARNING, "supply voltage low: 4.6 V"),
    (Severity.INFO, "temperature {celsius:.1f} C"),
    (Severity.ERROR, "I2C timeout on bus 1, retrying"),
    (Severity.INFO, "I2C bus 1 recovered"),
    (Severity.CRITICAL, "heater overcurrent, heater off for 5 s"),
]
Command = uavcan.node.ExecuteCommand_1_3


def make_node(name: str, iface: str, fd: bool, minor: int = 0) -> pycyphal.application.Node:
    env = {
        "UAVCAN__CAN__IFACE": iface,
        "UAVCAN__CAN__MTU": "64" if fd else "8",
        "UAVCAN__NODE__ID": str(NODE_ID),
        "UAVCAN__PUB__TEMPERATURE__ID": str(TEMPERATURE_SUBJECT_ID),
        "UAVCAN__PUB__STATUS__ID": str(STATUS_SUBJECT_ID),
    }
    info = uavcan.node.GetInfo_1_0.Response(name=name, software_version=uavcan.node.Version_1_0(major=1, minor=minor))
    node = pycyphal.application.make_node(info, pycyphal.application.make_registry(None, environment_variables=env))
    node.start()
    return node


async def run_bootloader(iface: str, fd: bool, minor: int, server_node_id: int, path: str) -> bool:
    """Read the firmware file the way a bootloader does; whether it arrived whole."""
    node = make_node("demo.sensor", iface, fd, minor)
    node.heartbeat_publisher.mode = uavcan.node.Mode_1_0.SOFTWARE_UPDATE
    client = node.make_client(uavcan.file.Read_1_1, server_node_id)
    print(f"demo.sensor: bootloader reading {path} from node {server_node_id}", flush=True)
    size = 0
    try:
        while True:
            result = await client.call(uavcan.file.Read_1_1.Request(offset=size, path=uavcan.file.Path_2_0(path)))
            if result is None or result[0].error.value != 0:
                print(f"demo.sensor: bootloader read failed at byte {size}: {result}", flush=True)
                return False
            chunk = len(result[0].data.value)
            size += chunk
            if chunk < 256:
                print(f"demo.sensor: bootloader read {size} bytes; starting the new firmware", flush=True)
                return True
            await asyncio.sleep(0.02)  # slow enough to watch
    finally:
        client.close()
        node.close()


async def run_sensor(iface: str, fd: bool) -> None:
    minor = 0
    update = None  # (server node-ID, file) once an update is asked for
    while True:  # one pass per (re)start
        if update is not None:
            if await run_bootloader(iface, fd, minor, *update):
                minor += 1
            update = None
        node = make_node("demo.sensor", iface, fd, minor)
        temperature = node.make_publisher(uavcan.si.sample.temperature.Scalar_1_0, "temperature")
        diagnostics = node.make_publisher(uavcan.diagnostic.Record_1_1)
        status = node.make_publisher(uavcan.primitive.String_1_0, "status")
        # Straight through the presentation layer: no register names its type.
        velocity = node.presentation.make_publisher(uavcan.si.unit.velocity.Vector3_1_0, UNNAMED_SUBJECT_ID)
        restart = asyncio.Event()

        async def on_command(request: Command.Request, meta) -> Command.Response:
            if request.command == Command.Request.COMMAND_RESTART:
                asyncio.get_running_loop().call_later(0.2, restart.set)  # answer first, then restart
                return Command.Response(status=Command.Response.STATUS_SUCCESS)
            if request.command == Command.Request.COMMAND_FACTORY_RESET:
                return Command.Response(status=Command.Response.STATUS_SUCCESS)
            if request.command == Command.Request.COMMAND_BEGIN_SOFTWARE_UPDATE:
                nonlocal update
                update = (meta.client_node_id, request.parameter.tobytes().decode())
                asyncio.get_running_loop().call_later(0.2, restart.set)
                return Command.Response(status=Command.Response.STATUS_SUCCESS)
            return Command.Response(status=Command.Response.STATUS_BAD_COMMAND)

        node.get_server(Command).serve_in_background(on_command)
        print(f"demo.sensor: node-ID {NODE_ID} on {iface}{' (CAN FD)' if fd else ''}", flush=True)
        tick = 0
        while not restart.is_set():
            kelvin = 293.15 + 5 * math.sin(time.monotonic() / 5)
            await temperature.publish(uavcan.si.sample.temperature.Scalar_1_0(kelvin=kelvin))
            if tick % 2 == 0:
                t = time.monotonic()
                await velocity.publish(uavcan.si.unit.velocity.Vector3_1_0(
                    meter_per_second=[math.cos(t), math.sin(t), 0.5]))
            if tick % 20 == 0:
                state = STATES[tick // 20 % len(STATES)]
                await status.publish(uavcan.primitive.String_1_0(f"state: {state}, {kelvin - 273.15:.1f} C"))
            if tick % 30 == 0:
                severity, text = DIAGNOSTICS[tick // 30 % len(DIAGNOSTICS)]
                await diagnostics.publish(uavcan.diagnostic.Record_1_1(
                    severity=Severity(severity),
                    text=text.format(celsius=kelvin - 273.15, adc=int(kelvin * 10) % 4096),
                ))
            tick += 1
            await asyncio.sleep(0.1)
        print("demo.sensor: restarting on request" + (" into the bootloader" if update else ""), flush=True)
        node.close()  # and straight back: well within Cynitor's 3 s offline threshold


async def run_twin(iface: str, fd: bool, delay: float = 10.0) -> None:
    await asyncio.sleep(delay)  # a later start, so its uptime differs
    make_node("demo.twin", iface, fd)
    print(f"demo.twin: also on node-ID {NODE_ID}", flush=True)
    await asyncio.Event().wait()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iface", default="vcan0", help="SocketCAN name, or a pycyphal spec (default vcan0)")
    parser.add_argument("--fd", action="store_true", help="run Cyphal/CAN FD (MTU 64)")
    parser.add_argument("--conflict", action="store_true", help="add a second node on the same node-ID")
    args = parser.parse_args()
    iface = args.iface if ":" in args.iface else f"socketcan:{args.iface}"
    tasks = [run_sensor(iface, args.fd)]
    if args.conflict:
        tasks.append(run_twin(iface, args.fd))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
