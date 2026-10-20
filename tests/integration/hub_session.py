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
  2. the scanner sees node 50 and its GetInfo name, and decodes the
     uavcan.diagnostic.Record it publishes on the fixed subject-ID (no
     register names it, as with most firmware);
  3. Cynitor's allocator, turned on for the session, gives the anonymous node
     a node-ID no node uses, answering from Cynitor's own;
  4. bus load is measured by the hub, not canbusload;
     and a raw log of the frames reads back through python-can;
  4a. a firmware update: node 50 accepts the command and reads the whole
     file from Cynitor with uavcan.file.Read, as a bootloader does; a
     recording holds both sides' calls, heard on the bus and decoded;
  4b. a subject no register names is not decoded; guessing its type from
     its payloads offers the right one, and setting it decodes the subject;
  4c. Cyphal v1.1 transfers on the wire (16-bit subject-IDs) are noticed,
     though not decoded;
  4e. broken and random frames (anonymous heartbeats, short payloads,
     unfinished transfers, a wrong CRC, non-Cyphal frames, noise) leave the
     session running and the device decoded, and log no error;
  4d. decoding on demand: a subject nobody watches is counted from its
     frames, not decoded, until a dashboard watches it;
  5. an adapter that disappears ends the session with an error;
  6. the databases are written to the session's data folder;
  7. a raw log plays back as a bus;
  8. the simulated bus (sim:classic, or sim:fd with --fd) runs its demo
     sensor, which Cynitor sees, names and decodes; no allocator runs, as
     none was asked for;
  9. a test node made as the Test nodes tab makes one runs on it: Cynitor
     names it, decodes its waveform through the registers it fills in, sees
     its mode change as it runs, and stops it on disconnect.

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
import json
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
TEMPERATURE_SUBJECT_ID = 1620
UNNAMED_SUBJECT_ID = 1700  # published with no register naming its type
TEST_NODE_SUBJECT_ID = 1900
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
        # A subject named in its registers, as firmware names them: Cynitor
        # decodes it live only through the registers, and in a raw log's
        # playback only through the sidecar that keeps what they said.
        registry=pycyphal.application.make_registry(
            None, environment_variables={"UAVCAN__PUB__TEMPERATURE__ID": str(TEMPERATURE_SUBJECT_ID)}),
    )
    device.start()
    import uavcan.si.sample.temperature
    temperature = device.make_publisher(uavcan.si.sample.temperature.Scalar_1_0, "temperature")

    async def publish_temperature():
        while True:
            await temperature.publish(uavcan.si.sample.temperature.Scalar_1_0(kelvin=300.0))
            await asyncio.sleep(0.1)
    temperature_task = asyncio.ensure_future(publish_temperature())
    # Straight through the presentation layer, so no register names its type.
    # Twelve bytes: on CAN FD, padded to a sixteen-byte frame.
    import uavcan.si.unit.velocity
    velocity = device.presentation.make_publisher(uavcan.si.unit.velocity.Vector3_1_0, UNNAMED_SUBJECT_ID)

    async def publish_velocity():
        while True:
            await velocity.publish(uavcan.si.unit.velocity.Vector3_1_0(meter_per_second=[1.0, 2.0, 3.5]))
            await asyncio.sleep(0.1)
    velocity_task = asyncio.ensure_future(publish_velocity())

    # Answers a software update the way a bootloader does: read the file,
    # chunk by chunk, from the node that sent the command.
    import uavcan.file
    Command = uavcan.node.ExecuteCommand_1_3
    downloaded: list[bytes] = []

    async def download(server_node_id: int, path: str) -> None:
        client = device.make_client(uavcan.file.Read_1_1, server_node_id)
        image = b""
        try:
            while True:
                result = await client.call(uavcan.file.Read_1_1.Request(
                    offset=len(image), path=uavcan.file.Path_2_0(path)))
                if result is None or result[0].error.value != 0:
                    return
                chunk = result[0].data.value.tobytes()
                image += chunk
                if len(chunk) < 256:
                    downloaded.append(image)
                    return
        finally:
            client.close()

    async def on_command(request, meta):
        if request.command != Command.Request.COMMAND_BEGIN_SOFTWARE_UPDATE:
            return Command.Response(status=Command.Response.STATUS_BAD_COMMAND)
        asyncio.ensure_future(download(meta.client_node_id, request.parameter.tobytes().decode()))
        return Command.Response(status=Command.Response.STATUS_SUCCESS)
    device.get_server(Command).serve_in_background(on_command)
    anonymous = pycyphal.presentation.Presentation(on_wire(None))
    allocatee = Allocatee(anonymous, bytes(range(16)))

    data = Path.cwd() / "data"
    data.mkdir()
    session = CANSession(data_dir=data)
    tap = can.Bus(interface="virtual", channel=WIRE)  # what an analyzer on the bus would see
    try:
        await session.connect(f"virtual:{WIRE}", bitrate=BITRATE, data_bitrate=DATA_BITRATE, allocator=True)
        check(session.is_running and session.hub is not None, "connected through the CAN hub")
        own_id = str(session.node_id)
        check(own_id == "127" and "UAVCAN__NODE__ID" not in os.environ, f"picked its own node-ID ({own_id}): 127, the first of the two Cyphal leaves to tools")
        check(session.can_fd == FD, f"session runs {'CAN FD' if FD else 'Classic CAN'}")
        own_frame = await first_frame_from(tap, int(own_id))
        check(own_frame is not None and own_frame.is_fd == FD,
              f"Cynitor's own frames on the wire are {'CAN FD' if FD else 'Classic CAN'}")

        seen = await wait_for(lambda: session.scanner.all_nodes[DEVICE_NODE_ID].has_responded_to_getInfo)
        check(seen, f"scanner sees node {DEVICE_NODE_ID} and its GetInfo")
        name = session.scanner.all_nodes[DEVICE_NODE_ID].info_response.name.tobytes().decode()
        check(name == "integration.device", f"GetInfo name is {name!r}")

        import uavcan.diagnostic
        record = uavcan.diagnostic.Record_1_1(
            severity=uavcan.diagnostic.Severity_1_0(uavcan.diagnostic.Severity_1_0.WARNING),
            text="integration diagnostic",
        )
        diagnostics = device.make_publisher(uavcan.diagnostic.Record_1_1)

        async def diagnostic_arrived():
            deadline = time.monotonic() + TIMEOUT
            while time.monotonic() < deadline:
                await diagnostics.publish(record)
                event = session.telemetry.latest_by_subject.get(8184)
                if event:
                    return {a["attribute"]: a["value"] for a in event["attributes"]}
                await asyncio.sleep(0.5)
            return None

        attributes = await diagnostic_arrived()
        check(attributes is not None and attributes.get("severity") == 4
              and attributes.get("text") == "integration diagnostic",
              f"diagnostic record decoded from its fixed subject: {attributes}")

        allocated = await wait_for(lambda: allocatee.get_result() is not None)
        check(allocated and allocatee.get_result() not in (int(own_id), DEVICE_NODE_ID),
              f"anonymous node was allocated node-ID {allocatee.get_result()}, which no node uses")
        check(session.allocator_status() == {"other_allocator": None, "allocated": 1},
              f"by Cynitor's allocator, on its one node-ID: {session.allocator_status()}")

        check(isinstance(session.bus_load, HubBusLoad), "bus load comes from the hub")
        loaded = await wait_for(lambda: session.bus_load.utilization > 0, timeout=5.0)
        check(loaded, f"bus load measured ({session.bus_load.utilization} %)")

        live = await wait_for(lambda: TEMPERATURE_SUBJECT_ID in session.telemetry.latest_by_subject)
        check(live, f"subject {TEMPERATURE_SUBJECT_ID}, named in the device's registers, decoded live")
        log = session.raw_logs.start()
        await asyncio.sleep(4)  # heartbeats each way, the port list, temperatures
        session.raw_logs.stop()
        logged = list(can.LogReader(str(log.path)))
        from_device = [m for m in logged if m.is_rx and m.arbitration_id & 0x7F == DEVICE_NODE_ID]
        own = [m for m in logged if not m.is_rx and m.arbitration_id & 0x7F == int(own_id)]
        check(from_device and own and all(m.is_fd == FD for m in from_device + own),
              f"raw log {log.path.name}: {len(logged)} frames, the device's and Cynitor's own "
              f"({'CAN FD' if FD else 'Classic CAN'}), read back by python-can")

        # Cynitor's command to the device and the device's reads from Cynitor,
        # as an analyzer on the bus would hear them.
        calls_recording = await session.event_logger.create_recording(
            name="update calls", filter_spec={"service_ids": [408, 435]})
        image = os.urandom(3000)  # eleven full chunks and a short one
        session.firmware_folder.mkdir()
        (session.firmware_folder / "integration-2.0.app.bin").write_bytes(image)
        update = await session.begin_firmware_update(DEVICE_NODE_ID, "integration-2.0.app.bin")
        # A fast node may be reading already: the update is followed from before the command.
        check(update["state"] in ("requested", "reading", "transferred"),
              f"node {DEVICE_NODE_ID} accepted the update command")
        done = await wait_for(lambda: downloaded)
        check(done and downloaded[0] == image,
              f"node {DEVICE_NODE_ID} read the whole firmware file from Cynitor ({len(image)} bytes)")
        check(update["state"] == "transferred" and update["read"] == len(image),
              f"the update's progress followed the reads: {update['state']}, {update['read']} bytes")
        # The recorder hands calls over every 50 ms, and writing them can wait
        # for a slow disk: read the recording once it holds the command and
        # every read, however long that takes.
        expected_calls = 1 + len(range(0, len(image), 256))

        async def recorded_calls() -> list:
            return [e for e in await session.event_logger.get_recording_events(calls_recording)
                    if e["kind"] == "service_call"]

        deadline = time.monotonic() + TIMEOUT
        calls = await recorded_calls()
        while len(calls) < expected_calls and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            calls = await recorded_calls()
        commands = [c for c in calls if c["service_id"] == 435]
        check(len(commands) == 1 and commands[0]["publisher_node_id"] == DEVICE_NODE_ID
              and commands[0]["attributes"]["client_node_id"] == int(own_id)
              and commands[0]["attributes"]["status"] == "ok"
              and json.loads(commands[0]["attributes"]["request"])["command"]
              == Command.Request.COMMAND_BEGIN_SOFTWARE_UPDATE,
              f"the recording holds Cynitor's update command to node {DEVICE_NODE_ID}, decoded"
              f" ({len(calls)} calls recorded)")
        reads = [c for c in calls if c["service_id"] == 408]
        recorded = bytes(b for c in reads for b in json.loads(c["attributes"]["response"])["data"]["value"])
        check(all(c["publisher_node_id"] == int(own_id) and c["attributes"]["client_node_id"] == DEVICE_NODE_ID
                  and c["message_type"] == "uavcan.file.Read_1_1" for c in reads)
              and [json.loads(c["attributes"]["request"])["offset"] for c in reads] == list(range(0, len(image), 256))
              and recorded == image,
              f"and node {DEVICE_NODE_ID}'s {len(reads)} reads from Cynitor, their responses the whole file")

        listed = await wait_for(lambda: UNNAMED_SUBJECT_ID in
                                (session.telemetry.get_all_nodes_info()["nodes"][DEVICE_NODE_ID]["publishers"]))
        check(listed and UNNAMED_SUBJECT_ID not in session.scanner.subject_types,
              f"subject {UNNAMED_SUBJECT_ID} is in the device's port list, but no register names its type")
        from dsdl_manager import DsdlManager
        types = DsdlManager(ROOT, data_dir=data).message_types()
        guess = await session.user_types.guess(UNNAMED_SUBJECT_ID, types)
        offered = {c["type"]: c["preview"] for c in guess["candidates"]}
        right = "uavcan.si.unit.velocity.Vector3.1.0"
        check(guess["samples"] > 0 and offered.get(right) == {"meter_per_second": [1.0, 2.0, 3.5]},
              f"guessing from {guess['samples']} payloads offers {right} among {guess['matches']} fitting types")
        check("uavcan.si.sample.velocity.Vector3.1.0" not in offered,
              "a type of another size (the same with a timestamp) is not offered")
        session.user_types.set(UNNAMED_SUBJECT_ID, right)
        decoded = await wait_for(lambda: UNNAMED_SUBJECT_ID in session.telemetry.latest_by_subject)
        values = decoded and session.telemetry.latest_by_subject[UNNAMED_SUBJECT_ID]["attributes"][0]["value"]
        check(values == [1.0, 2.0, 3.5], f"subject {UNNAMED_SUBJECT_ID} decodes once its type is set: {values}")

        # A Cyphal v1.1 node: 16-bit subject-IDs, a frame format v1.0 lacks.
        check(session.v11.status() is None, "no Cyphal v1.1 traffic seen yet")
        for subject in (0x1234, 0xBEEF):
            tap.send(can.Message(arbitration_id=(4 << 26) | (subject << 8) | (1 << 7) | 77,
                                 data=b"v1.1" + bytes([0xE0]), is_fd=FD))  # one whole transfer
        seen = await wait_for(lambda: session.v11.status() is not None and session.v11.status()["transfers"] == 2,
                              timeout=5)
        v11 = session.v11.status()
        check(seen and v11["nodes"] == [77] and v11["subject_ids"] == [0x1234, 0xBEEF],
              f"Cyphal v1.1 traffic noticed: {v11}")

        # Hostile and broken frames, as a faulty or foreign device sends them:
        # the session runs on, the device's data keeps coming, nothing is
        # logged as an error.
        import logging
        import random

        class Errors(logging.Handler):
            def __init__(self):
                super().__init__(logging.ERROR)
                self.records = []

            def emit(self, record):
                self.records.append(f"{record.name}: {record.getMessage()}")
        errors = Errors()
        logging.getLogger().addHandler(errors)
        heartbeat = 7509
        broken = [
            # an anonymous heartbeat (anonymous nodes may not send one)
            can.Message(arbitration_id=(4 << 26) | (1 << 24) | (3 << 21) | (heartbeat << 8) | 99,
                        data=bytes(7) + b"\xe0"),
            # a heartbeat one byte long
            can.Message(arbitration_id=(4 << 26) | (3 << 21) | (heartbeat << 8) | 99, data=b"\x01\xe0"),
            # the start of a transfer that never ends
            can.Message(arbitration_id=(4 << 26) | (3 << 21) | (heartbeat << 8) | 98, data=bytes(7) + b"\xa1"),
            # a two-frame transfer whose CRC is wrong
            can.Message(arbitration_id=(4 << 26) | (3 << 21) | (heartbeat << 8) | 97, data=bytes(7) + b"\xa2"),
            can.Message(arbitration_id=(4 << 26) | (3 << 21) | (heartbeat << 8) | 97, data=b"\x00\x00\x00\x42"),
            # a GetInfo request to Cynitor that carries bytes it should not
            can.Message(arbitration_id=(4 << 26) | (1 << 25) | (1 << 24) | (430 << 14) | (int(own_id) << 7) | 96,
                        data=b"\xff" * 7 + b"\xe3"),
            # what is not Cyphal/CAN: an 11-bit frame, a remote frame, an error frame, an empty frame
            can.Message(arbitration_id=0x123, is_extended_id=False, data=b"\x01\x02"),
            can.Message(arbitration_id=0x1ABCDE, is_remote_frame=True, dlc=4),
            can.Message(arbitration_id=0x4, is_error_frame=True, data=bytes(8)),
            can.Message(arbitration_id=0x10000000 | 95, data=b""),
        ]
        noise = random.Random(1)
        keep_clear = {DEVICE_NODE_ID, int(own_id)}
        while len(broken) < 300:  # and noise, from node-IDs and subjects the checks below don't use
            can_id = noise.getrandbits(29)
            subject = (can_id >> 8) & 0x1FFF
            if can_id & 0x7F in keep_clear or (not can_id >> 25 & 1 and subject in
                                                (heartbeat, TEMPERATURE_SUBJECT_ID, UNNAMED_SUBJECT_ID)):
                continue
            size = noise.choice([1, 2, 5, 8, 12, 16, 32, 64] if FD else range(9))
            broken.append(can.Message(arbitration_id=can_id, data=noise.randbytes(size), is_fd=FD and size > 8))
        received_before = session.telemetry.latest_by_subject[TEMPERATURE_SUBJECT_ID]["timestamp_unix"]
        for msg in broken:
            msg.is_fd = msg.is_fd or (FD and not msg.is_error_frame and not msg.is_remote_frame
                                      and msg.is_extended_id)
            tap.send(msg)
            await asyncio.sleep(0.002)
        await asyncio.sleep(1.0)
        logging.getLogger().removeHandler(errors)
        check(session.is_running, f"{len(broken)} broken and random frames leave the session running")
        check(await wait_for(lambda: session.telemetry.latest_by_subject[TEMPERATURE_SUBJECT_ID]["timestamp_unix"]
                             > received_before, timeout=5)
              and not session.telemetry.get_all_nodes_info()["nodes"][DEVICE_NODE_ID].get("has_disappeared"),
              f"node {DEVICE_NODE_ID}'s temperature is still decoded")
        check(not errors.records, f"and none of them is logged as an error: {errors.records[:5]}")

        # Decoding on demand: a subject nobody watches is counted from its
        # frames, not decoded, until a dashboard shows its values.
        def latest_temperature() -> dict:
            return session.telemetry.latest_by_subject.get(TEMPERATURE_SUBJECT_ID, {})

        await session.set_decode_on_demand(True)
        counted = await wait_for(lambda: latest_temperature().get("undecoded") and latest_temperature()["rate"] > 0,
                                 timeout=10)
        event = latest_temperature()
        check(counted and event["attributes"] == [] and 5 <= event["rate"] <= 15
              and event["publisher_node_id"] == DEVICE_NODE_ID,
              f"decoding on demand, subject {TEMPERATURE_SUBJECT_ID} is counted at {event.get('rate')} Hz "
              "from its frames, not decoded")
        check(TEMPERATURE_SUBJECT_ID not in session.scanner.publishers_subscribers
              and 8184 in session.scanner.publishers_subscribers,
              "pycyphal no longer receives it; diagnostics are decoded all the same")
        session.set_watched("a dashboard", [TEMPERATURE_SUBJECT_ID])
        decoded = await wait_for(
            lambda: latest_temperature().get("attributes") and not latest_temperature().get("undecoded"), timeout=5)
        check(decoded and latest_temperature()["attributes"][-1]["value"] == 300.0,
              "watched by a dashboard, it is decoded again at once")
        session.set_watched("a dashboard", None)
        await session.set_decode_on_demand(False)
        check(TEMPERATURE_SUBJECT_ID in session.scanner.publishers_subscribers and session.on_demand.rates is None,
              "switched off, every subject is decoded again")

        def unplugged(timeout=None):  # as a driver's read fails when its adapter goes away
            raise can.CanOperationError("No such device (it may have been disconnected)")
        session.hub._adapter.recv = unplugged
        gone = await wait_for(lambda: not session.is_running, timeout=10.0)
        check(gone, "an adapter that disappears ends the session")
        check("receive failed" in (session.last_error or "") and "No such device" in (session.last_error or ""),
              f"with a reason: {session.last_error!r}")

        written = sorted(p.name for p in data.glob("*.db"))
        check(written == ["monitor_app.db", "telemetry_events.db"],
              f"databases written to the data folder: {written}")

        # Play the raw log back: the bus as it was, from the file alone.
        temperature_task.cancel()
        await session.play_raw_log(log.path.name, speed=5)
        check(session.is_running and session.can_interface == f"rawlog:{log.path.name}",
              "playing the raw log as a bus")
        decoded = await wait_for(lambda: session.telemetry is not None
                                 and TEMPERATURE_SUBJECT_ID in session.telemetry.latest_by_subject, timeout=10)
        check(decoded, f"playback decodes subject {TEMPERATURE_SUBJECT_ID} from the log's sidecar")
        named = await wait_for(lambda: session.telemetry is not None and session.telemetry.get_all_nodes_info()["nodes"]
                               .get(DEVICE_NODE_ID, {}).get("name") == "integration.device", timeout=10)
        check(named, "playback names the device from the sidecar")
        ended = await wait_for(lambda: not session.is_running, timeout=15)
        check(ended and "end of the raw log" in (session.last_error or ""),
              f"playback ends with the log: {session.last_error!r}")

        # The simulated bus: its demo devices, with nothing but Cynitor.
        sim = "sim:fd" if FD else "sim:classic"
        await session.connect(sim)
        check(session.is_running and session.simulation is not None and session.can_fd == FD,
              f"connected to the simulated bus {sim}")
        check(session.allocator is None and session.allocator_status() is None,
              "with no allocator, as none was asked for")
        named = await wait_for(lambda: session.telemetry.get_all_nodes_info()["nodes"]
                               .get(DEVICE_NODE_ID, {}).get("name") == "demo.sensor")
        check(named, f"its demo sensor appears, named, on node-ID {DEVICE_NODE_ID}")
        decoded = await wait_for(lambda: TEMPERATURE_SUBJECT_ID in session.telemetry.latest_by_subject)
        check(decoded, f"and its subject {TEMPERATURE_SUBJECT_ID} decodes")

        # A test node, defined as the dashboard defines one, counting up. It sets
        # no node-ID: Cynitor gives it a free one.
        from testnodes import normalize_definition, value_schema
        dsdl = DsdlManager(ROOT, data_dir=data)
        definition = normalize_definition({
            "name": "integration.test_node",
            "publishers": [{"subject_id": TEST_NODE_SUBJECT_ID, "type": "uavcan.si.unit.velocity.Scalar.1.0",
                            "rate_hz": 20, "value": {"meter_per_second": {"$wave": "count", "min": 0, "max": 1000}}}],
        }, lambda name: value_schema(dsdl.read_model(name)))
        test_id = session.test_nodes.add(definition)["id"]
        started = await session.test_nodes.start(test_id)  # the simulated bus asks for no confirmation
        test_node_id = started["assigned_node_id"]
        check(started["running"] and test_node_id not in (None, int(own_id), DEVICE_NODE_ID),
              f"a test node starts on the node-ID Cynitor gives it, {test_node_id}, clear of the bus's")

        def test_node():
            return session.telemetry.get_all_nodes_info()["nodes"].get(test_node_id, {})

        check(await wait_for(lambda: test_node().get("name") == "integration.test_node"), "Cynitor names it")
        check(session.test_nodes.identify(test_node_id, test_node().get("unique_id_hex")) == test_id,
              "and knows it for a test node by its unique-ID")

        def speed():
            event = session.telemetry.latest_by_subject.get(TEST_NODE_SUBJECT_ID)
            return None if event is None else event["attributes"][0]["value"]

        check(await wait_for(lambda: speed() is not None),
              f"its subject {TEST_NODE_SUBJECT_ID} decodes, its type named in its registers")
        first = speed()
        check(await wait_for(lambda: speed() > first, timeout=5), f"counting up: {first}, then {speed()}")

        await session.test_nodes.replace(test_id, {**definition, "mode": "maintenance"})
        check(await wait_for(lambda: session.scanner.node_mode(test_node_id) == "MAINTENANCE", timeout=5),
              "its mode changes as it runs")
        await session.disconnect()
        check(not session.is_running and session.simulation is None, "disconnecting stops the simulated bus")
        check(session.test_nodes.running_node_ids() == set() and not session.test_nodes.view(test_id)["running"],
              "and the test node")

        # Cynitor's node-ID is the free one of the bus it was picked on: forgotten
        # with the session, picked again on the next bus, where 127 is now taken.
        check(session.node_id is None and "UAVCAN__NODE__ID" not in os.environ,
              "the node-ID Cynitor picked is the session's, gone when it disconnects, never in the environment")
        on_127 = pycyphal.application.make_node(uavcan.node.GetInfo_1_0.Response(name="integration.on127"),
                                                transport=on_wire(127))
        on_127.start()
        await asyncio.sleep(1.5)  # heartbeating before Cynitor comes
        await session.connect(f"virtual:{WIRE}", bitrate=BITRATE, data_bitrate=DATA_BITRATE)
        check(session.scanner.node.id == 126, f"connected again, it takes 126, not 127: {session.scanner.node.id}")
        # A node that comes up later on Cynitor's own node-ID is said, not left unnoticed.
        on_126 = pycyphal.application.make_node(uavcan.node.GetInfo_1_0.Response(name="integration.on126"),
                                                transport=on_wire(126))
        on_126.start()
        said = await wait_for(lambda: "own node-ID 126" in (session.last_error or ""), timeout=10)
        check(said, f"another node on Cynitor's own node-ID is reported: {session.last_error!r}")
        await session.disconnect()
        on_126.close()
        on_127.close()
    finally:
        temperature_task.cancel()
        velocity_task.cancel()
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
