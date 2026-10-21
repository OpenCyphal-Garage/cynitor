#!/usr/bin/env python3

import os
import asyncio
import collections
import logging
import datetime
import re
import importlib
import socket
import struct
import time
import numpy as np
from pycyphal.dsdl import get_model, to_builtin
from pydsdl import Field

from typing import Any, Optional, Callable
import pycyphal
import pycyphal.application

import uavcan.node
import uavcan.node.port
import uavcan.diagnostic
import uavcan.pnp

from events import SubjectEvent
from local_time import local_isoformat
from node_info import NodeInfo
from update_check import parse_version
from version import __version__
from node_identity_map import NodeIdentityMap
from scanner_registers import ScannerRegisters
from scanner_services import ScannerServices

# pycyphal logs each frame it drops from a full queue, at INFO: thousands of
# lines a second from a bus that outruns the decoding. They are counted
# instead (ScannerNode.dropped_frames).
logging.getLogger("pycyphal.transport.can._session._input").setLevel(logging.WARNING)

NODE_NAME = "org.opencyphal.cynitor"  # Cynitor's name on the bus (uavcan.node.GetInfo)

# Linux's getsockopt(SOL_SOCKET, SO_MEMINFO): unsigned ints, of which the
# ninth (SK_MEMINFO_DROPS) counts what the socket's full receive queue let go.
_SO_MEMINFO = 55
_SK_MEMINFO_DROPS = 8


def _fire_and_log(coro, context: str = "background task"):
    """Schedule a coroutine and log any exception with full traceback."""
    task = asyncio.ensure_future(coro)
    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logging.error("%s failed", context, exc_info=(type(exc), exc, exc.__traceback__))
    task.add_done_callback(_on_done)
    return task

class ScannerNode(ScannerRegisters, ScannerServices):
    # Constants
    REGISTER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_app.db")
    NUM_NODES = 128
    HEARTBEAT_SUBJECT_ID = 7509
    PORT_LIST_SUBJECT_ID = 7510
    PNP_SUBJECT_ID_V2 = 8165
    PNP_SUBJECT_ID_V1 = 8166
    DIAGNOSTIC_SUBJECT_ID = 8184
    MESSAGE_QUEUE_SIZE = 1000
    REGISTER_TIMEOUT = 2.0
    SERVICE_CALL_TIMEOUT = 5.0
    MESSAGE_RATE_WINDOW_SECONDS = 10
    # A publisher counts in its subject's total rate while it still sends: its
    # last message within three of its periods, two seconds at least. The
    # dashboard calls one quiet for longer silent by the same rule.
    PUBLISHING_PERIODS = 3
    PUBLISHING_MIN_SECONDS = 2.0
    MAX_SUBJECT_ID = 8191
    MAX_SERVICE_ID = 511
    # A port-ID register's "unset" (register/384.Access): the port is inactive.
    UNSET_PORT_ID = 65535
    MAX_REGISTERS = 256
    MAX_UNANSWERED_INDEXES = 2  # register list indexes in a row a node may not answer
    INFO_REFRESH_S = 60.0   # GetInfo refresh period once a node has answered
    INFO_RETRY_S = 10.0     # retry period while it has not
    NODE_ID_CONFLICT_REPORT_S = 60.0  # report a shared node-ID at most this often
    KERNEL_DROPS_REPORT_S = 60.0      # warn of frames lost in the kernel at most this often
    _kernel_drops_seen = 0            # the kernel's drop count at the last offline sweep
    _kernel_drops_reported = 0        # the kernel's drop count at the last warning ...
    _kernel_drops_reported_at = float("-inf")  # ... and when it was given
    # The furthest the decoding of a subject may fall behind the bus. pycyphal
    # keeps the frames that wait to be decoded without limit, so a subject that
    # outran the decoding put the live view ever further behind, with nothing
    # counted as lost. A burst is still taken whole; but once a message is this
    # old when its turn comes, the frames waiting behind it are let go, all at
    # once, and counted (dropped_frames). Dropping them one by one as they
    # arrive would leave no transfer of several frames whole.
    MAX_DECODING_LAG_S = 1.0
    offline: Optional[dict] = None    # set per session while a raw log plays (see __init__)
    listen_only = False               # set per session that sends nothing: no GetInfo then
    # Called with a message when another node uses Cynitor's own node-ID (the session's last_error).
    on_own_node_id_conflict: Optional[Callable[[str], None]] = None
    _dropped_frames_closed = 0        # frames dropped on subjects no longer decoded (see dropped_frames)
    # While Cynitor decodes on demand: the only subjects decoded, of those
    # whose type is known (see set_decoding). None: all of them.
    decode_only: Optional[frozenset] = None
    # Decoded whatever is asked for: what the dashboard cannot do without, each
    # a message a second or so from a node.
    ALWAYS_DECODED = frozenset({7509, 7510, 8165, 8166, 8184})
    STANDARD_SERVICES = {
        384: 'uavcan.register.Access_1_0',
        385: 'uavcan.register.List_1_0',
        430: 'uavcan.node.GetInfo_1_0',
        435: 'uavcan.node.ExecuteCommand_1_3',
    }
    # Every standard service with a fixed ID, for reading the calls heard on
    # the bus: a node may serve one without a register saying so, and Cynitor
    # serves uavcan.file.Read itself to a node updating its firmware.
    FIXED_SERVICES = {
        **STANDARD_SERVICES,
        405: 'uavcan.file.GetInfo_0_2',
        406: 'uavcan.file.List_0_2',
        407: 'uavcan.file.Modify_1_1',
        408: 'uavcan.file.Read_1_1',
        409: 'uavcan.file.Write_1_1',
        434: 'uavcan.node.GetTransportStatistics_0_1',
        510: 'uavcan.time.GetSynchronizationMasterInfo_0_1',
    }

    def __init__(self, register_file: Optional[str] = None, transport=None) -> None:
        """``register_file`` is where the node's registers persist; the session
        puts it in the data folder (see data_dir). Defaults to REGISTER_FILE.
        ``transport`` is the CAN transport, with the node-ID, the session made
        (main.CANSession._transport); without one, pycyphal makes one from the
        UAVCAN__* environment variables, as its own tools do."""
        # Cynitor's own name and version, as another tool on the bus sees them
        # (it named itself after the pycyphal example it began as).
        major, minor = (parse_version(__version__) or (0, 0))[:2]
        node_information = uavcan.node.GetInfo_1.Response(
            software_version=uavcan.node.Version_1(major=major, minor=minor),
            name=NODE_NAME,
        )

        self._node = pycyphal.application.make_node(node_information, register_file or ScannerNode.REGISTER_FILE,
                                                    transport=transport)
        self._node.heartbeat_publisher.mode = uavcan.node.Mode_1.OPERATIONAL
        self._node.heartbeat_publisher.vendor_specific_status_code = os.getpid() % 100

        self.heartbeat_subscriber = self._node.make_subscriber(uavcan.node.Heartbeat_1_0, self.HEARTBEAT_SUBJECT_ID)
        self.heartbeat_subscriber.receive_in_background(self.heartbeat_callback)

        self.port_subscriber = self._node.make_subscriber(uavcan.node.port.List_1_0, self.PORT_LIST_SUBJECT_ID)
        self.port_subscriber.receive_in_background(self.port_callback)

        self.pnp_v2_subscriber = self._node.make_subscriber(uavcan.pnp.NodeIDAllocationData_2_0, self.PNP_SUBJECT_ID_V2)
        self.pnp_v2_subscriber.receive_in_background(self.pnp_v2_callback)

        self.pnp_v1_subscriber = self._node.make_subscriber(uavcan.pnp.NodeIDAllocationData_1_0, self.PNP_SUBJECT_ID_V1)
        self.pnp_v1_subscriber.receive_in_background(self.pnp_v1_callback)

        self.all_nodes: dict[int, NodeInfo] = {node_id: NodeInfo(node_id=node_id) for node_id in range(self.NUM_NODES)}
        self.identity_map = NodeIdentityMap()
        self.subject_attributes: dict[int, list[str]] = {}                # Maps subject_id to list of attribute names
        self.publishers_subscribers: dict[int, Any] = {}  # pycyphal Subscribers
        self.message_queue: asyncio.Queue[SubjectEvent] = asyncio.Queue(maxsize=self.MESSAGE_QUEUE_SIZE)
        self.dropped_events = 0  # events discarded because message_queue was full
        self.active_publishers: dict[int, set] = {}                        # Maps subject_id to set of publisher node_ids
        self.services: set[str] = set()
        self.service_metadata = {}  # (node_id, service_id) -> {"namespace": str, "service_name": str}
        self.service_clients = {}
        self.node_service_types: dict[int, dict[int, str]] = {}  # node_id -> {service_id -> type_string}
        self.on_node_event: Optional[Callable] = None
        self.on_node_data_save: Optional[Callable] = None
        self._prev_health: dict[int, int] = {}
        self._prev_mode: dict[int, int] = {}
        self._prev_uptime: dict[int, int] = {}
        self._prev_ports: dict[int, dict] = {}  # node_id -> {publishers: [...], ...}
        self._info_in_flight: set[int] = set()  # node_ids with a GetInfo running
        self._rate_timestamps: dict[int, dict[Optional[int], collections.deque]] = {}  # subject_id -> node_id (None: anonymous) -> times
        self.subject_types: dict[int, str] = {}           # subject_id -> the type it is decoded as
        # Those of subject_types the user set, for subjects no register names.
        self.user_subject_types: dict[int, str] = {}
        self._sampling: set[int] = set()                  # subjects sample_subject listens to
        self._uptime_before_drop: dict[int, int] = {}     # node_id -> uptime before a drop, until judged
        self._node_id_conflict_reported: dict[int, float] = {}  # node_id -> monotonic time of last report
        # While a raw log plays: its sidecar (see raw_log). Nodes then answer
        # nothing, so ports and names come from it instead of the nodes.
        self.offline: Optional[dict] = None
        # Nodes publish uavcan.diagnostic.Record on its fixed subject-ID with no
        # register naming it, so it is decoded for every node regardless.
        self._subscribe(self.DIAGNOSTIC_SUBJECT_ID, uavcan.diagnostic.Record_1_1, "uavcan.diagnostic.Record_1_1")
        try:
            self._node.start()
        except Exception as e:
            logging.error(f"Failed to start PyCyphal node: {e}")
            raise

    def get_transport_info(self) -> dict:
        """Read-only snapshot of transport-layer protocol params and statistics.

        Reaches the shared CAN transport (``node.presentation.transport``) and
        reads pycyphal's own counters. Synchronous and cheap (attribute reads +
        a dataclass copy), so it is safe to call directly from the event loop.
        Returns ``{}`` if the transport is unavailable for any reason.
        """
        try:
            transport = self._node.presentation.transport
        except Exception:
            return {}

        info: dict = {}
        try:
            pp = transport.protocol_parameters
            info["protocol"] = {
                "mtu": pp.mtu,
                "transfer_id_modulo": pp.transfer_id_modulo,
                "max_nodes": pp.max_nodes,
                # Single-frame payload limit: 7 for Classic CAN, up to 63 for CAN FD.
                "is_fd": pp.mtu > 7,
            }
        except Exception:
            pass
        try:
            st = transport.sample_statistics()
            info["statistics"] = {
                "in_frames": st.in_frames,
                "in_frames_cyphal": st.in_frames_cyphal,
                "in_frames_cyphal_accepted": st.in_frames_cyphal_accepted,
                "in_frames_errored": st.in_frames_errored,
                "in_frames_loopback": st.in_frames_loopback,
                "out_frames": st.out_frames,
                "out_frames_timeout": st.out_frames_timeout,
                "out_frames_loopback": st.out_frames_loopback,
                "media_acceptance_filtering_efficiency": st.media_acceptance_filtering_efficiency,
                "lost_loopback_frames": st.lost_loopback_frames,
            }
        except Exception:
            pass
        return info

    @staticmethod
    def _dsdl_type_to_module_name(dsdl_type: str) -> str:
        """
        Map a wire type name to its compiled Python module name.

        "uavcan.node.Heartbeat.1.0" becomes "uavcan.node.Heartbeat_1_0": only
        the dots before a version digit are rewritten, so namespace dots
        survive and the result still splits into (namespace, class name).
        """
        return re.sub(r'(?<=\d)\.(?=\d)|(?<=\w)\.(?=\d)', '_', dsdl_type)

    @staticmethod
    def _canonical_type_name(module_name: str) -> str:
        """The spelling the compiled type actually has, if ``module_name`` differs only in case.

        DSDL type names are case-sensitive, but some firmware advertises them
        in lowercase ("uavcan.register.access.1.0"), and the class lookup then
        fails. When the exact name does not exist and exactly one type in the
        namespace matches it ignoring case, that one is meant. Anything else --
        an unknown namespace, an exact match, no match, several -- is returned
        unchanged.
        """
        namespace, _, name = module_name.rpartition(".")
        try:
            module = importlib.import_module(namespace)
        except ImportError:
            return module_name
        if hasattr(module, name):
            return module_name
        matches = [attr for attr in dir(module) if attr.lower() == name.lower()]
        return f"{namespace}.{matches[0]}" if len(matches) == 1 else module_name

    def offline_ports(self, node_id: int) -> tuple[dict[int, str], dict[int, str]]:
        """(publishers, servers) of a node as a raw log's sidecar recorded them."""
        def ports(kind: str) -> dict[int, str]:
            return {int(k): v for k, v in (self.offline or {}).get(kind, {}).get(str(node_id), {}).items()}
        return ports("publishers"), ports("servers")

    async def add_subscriptions(self, node_id: int, dsdl_pub_messages: dict[int, str]) -> None:
        """Decode every subject the node publishes, subscribing to those not decoded yet."""
        for subject_id, message_type in dsdl_pub_messages.items():
            self.active_publishers.setdefault(subject_id, set()).add(node_id)

            # "uavcan.primitive.String_1_0" -> module "uavcan.primitive", class "String_1_0"
            namespace, _, data_type = message_type.rpartition('.')
            if not namespace:
                continue

            if subject_id in self.user_subject_types:
                self.set_subject_type(subject_id, None)  # a register's word over the user's guess
            subscribed = self.subject_types.get(subject_id)
            if subscribed is None:
                data_type_class = getattr(importlib.import_module(namespace), data_type)
                self._subscribe(subject_id, data_type_class, message_type)
            elif self._major_type(subscribed) != self._major_type(message_type):
                self._report_type_conflict(subject_id, node_id, message_type, subscribed)

    SAMPLE_EXTENT = 4096  # bytes kept of each payload sample_subject collects

    def set_subject_type(self, subject_id: int, message_type: Optional[str]) -> None:
        """Decode ``subject_id`` as ``message_type`` ("ns.Name_1_0"), chosen by the user; None stops that.

        Only for subjects no publisher names in its registers: RuntimeError if
        one does, or while sample_subject listens to the subject. ValueError
        if ``message_type`` is not a compiled message type.
        """
        if subject_id in self.subject_types and subject_id not in self.user_subject_types:
            raise RuntimeError(f"Subject {subject_id} is decoded as {self.subject_types[subject_id]}, "
                               "as its publisher's registers say")
        if subject_id in self._sampling:
            raise RuntimeError(f"Subject {subject_id} is being listened to for a type guess; try again shortly")
        if message_type is not None:
            message_type = self._dsdl_type_to_module_name(message_type)  # "ns.Name.1.0" -> "ns.Name_1_0"
        data_type_class = None if message_type is None else self._message_class(message_type)
        if subject_id in self.user_subject_types:
            self._unsubscribe(subject_id)
            self.subject_types.pop(subject_id, None)
            del self.user_subject_types[subject_id]
        if data_type_class is not None and message_type is not None:
            self._subscribe(subject_id, data_type_class, message_type)
            self.user_subject_types[subject_id] = message_type

    @staticmethod
    def _message_class(message_type: str):
        namespace, _, name = message_type.rpartition('.')
        try:
            data_type_class = getattr(importlib.import_module(namespace), name)
        except (ImportError, AttributeError, ValueError):
            raise ValueError(f"{message_type} is not a compiled DSDL type") from None
        if hasattr(data_type_class, "Request"):
            raise ValueError(f"{message_type} is a service type, not a message type")
        return data_type_class

    async def sample_subject(self, subject_id: int, duration: float, limit: int = 20) -> list[bytes]:
        """The raw payloads published on an undecoded subject within ``duration`` seconds.

        Listens at the transport level, as no type is known yet.
        """
        if subject_id in self.subject_types:
            raise RuntimeError(f"Subject {subject_id} is already decoded")
        if subject_id in self._sampling:
            raise RuntimeError(f"Subject {subject_id} is already being listened to")
        import pycyphal.transport as transport
        spec = transport.InputSessionSpecifier(transport.MessageDataSpecifier(subject_id), None)
        session = self._node.presentation.transport.get_input_session(
            spec, transport.PayloadMetadata(self.SAMPLE_EXTENT))
        self._sampling.add(subject_id)
        payloads: list[bytes] = []
        deadline = asyncio.get_running_loop().time() + duration
        try:
            while len(payloads) < limit:
                received = await session.receive(deadline)
                if received is None:
                    break
                payloads.append(b"".join(received.fragmented_payload))
        finally:
            session.close()
            self._sampling.discard(subject_id)
        return payloads

    def _subscribe(self, subject_id: int, data_type_class, message_type: str) -> None:
        """Decode ``subject_id`` as ``data_type_class`` from now on, whenever it is to be decoded."""
        model = get_model(data_type_class)
        self.subject_attributes[subject_id] = [a.name for a in model.attributes if isinstance(a, Field)]
        self.subject_types[subject_id] = message_type
        if self._decoded_now(subject_id):
            self._listen(subject_id, data_type_class)

    def _decoded_now(self, subject_id: int) -> bool:
        return self.decode_only is None or subject_id in self.decode_only or subject_id in self.ALWAYS_DECODED

    def set_decoding(self, subject_ids) -> None:
        """Decode only ``subject_ids`` of the subjects whose type is known; None decodes them all.

        A subject not decoded costs next to nothing, as its frames are let go
        on arrival; its type is kept, so decoding it again is a matter of
        listening. What is decoded whatever is asked for: ALWAYS_DECODED.
        """
        self.decode_only = None if subject_ids is None else frozenset(subject_ids)
        for subject_id, message_type in list(self.subject_types.items()):
            listening = subject_id in self.publishers_subscribers
            if listening and not self._decoded_now(subject_id):
                self._unsubscribe(subject_id)
            elif not listening and self._decoded_now(subject_id):
                try:
                    self._listen(subject_id, self._message_class(message_type))
                except ValueError as exc:  # its type is compiled no more
                    logging.warning(f"Subject {subject_id} cannot be decoded: {exc}")

    def _listen(self, subject_id: int, data_type_class) -> None:
        """Receive ``subject_id``'s messages and turn them into events."""
        async def callback(msg, transfer, sub_id=subject_id):
            await self._publisher_callback(msg, transfer, sub_id)

        subscriber = self._node.make_subscriber(data_type_class, subject_id)
        subscriber.receive_in_background(callback)
        self.publishers_subscribers[subject_id] = subscriber
        logging.debug(f"Listening to subject {subject_id}")

    def _unsubscribe(self, subject_id: int) -> None:
        """Stop receiving ``subject_id``, if it is received, keeping count of the frames it lost."""
        subscriber = self.publishers_subscribers.pop(subject_id, None)
        if subscriber is None:
            return
        self._dropped_frames_closed += self._frames_dropped(subscriber)
        subscriber.close()

    @staticmethod
    def _frames_dropped(subscriber) -> int:
        try:
            return int(subscriber.transport_session.sample_statistics().drops)
        except (AttributeError, TypeError, ValueError):
            return 0

    def dropped_frames(self) -> int:
        """CAN frames let go before decoding: their subject's messages came faster than they were decoded,
        or the bus faster than they were read (kernel_dropped_frames)."""
        return self._dropped_frames_closed + self.kernel_dropped_frames() + sum(
            self._frames_dropped(subscriber) for subscriber in self.publishers_subscribers.values())

    def kernel_dropped_frames(self) -> int:
        """CAN frames the kernel let go, its receive queue for the scanner's SocketCAN socket being full.

        Past what can be decoded, most frames are lost there, before pycyphal
        sees them. The socket is pycyphal's own (its media's ``_sock``); 0 when
        there is none, behind the hub or off Linux.
        """
        try:
            sock = self._node.presentation.transport._maybe_media._sock
            meminfo = sock.getsockopt(socket.SOL_SOCKET, _SO_MEMINFO, 4 * (_SK_MEMINFO_DROPS + 1))
            return struct.unpack_from(f"{_SK_MEMINFO_DROPS + 1}I", meminfo)[_SK_MEMINFO_DROPS]
        except (AttributeError, OSError, struct.error):
            return 0

    def hold_offline_while_losing_frames(self, losing_elsewhere: bool = False) -> None:
        """Call no node offline while the kernel drops the scanner's frames, nor for a threshold after;
        nor while ``losing_elsewhere`` (the CAN hub falling behind its adapter, can_hub.LATE_FRAME_S).

        Its heartbeats are lost with the rest: a node declared gone had its
        subjects unsubscribed, and the kernel then discarded their frames
        before any counter saw them (F-042: 13 % of a 12,000/s flood).
        """
        drops = self.kernel_dropped_frames()
        if drops > self._kernel_drops_seen or losing_elsewhere:
            until = time.monotonic() + NodeInfo.OFFLINE_THRESHOLD_S
            for node in self.all_nodes.values():
                node.silence_unreliable_until = until
        self._kernel_drops_seen = drops

    def report_kernel_drops(self) -> None:
        """Warn of frames the kernel let go since the last warning, at most once a KERNEL_DROPS_REPORT_S."""
        drops = self.kernel_dropped_frames()
        now = time.monotonic()
        if drops <= self._kernel_drops_reported or now - self._kernel_drops_reported_at < self.KERNEL_DROPS_REPORT_S:
            return
        logging.warning(f"{drops - self._kernel_drops_reported} CAN frames lost in the kernel's receive queue: "
                        "the bus carries more than Cynitor decodes. Decode on demand handles such a bus.")
        self._kernel_drops_reported, self._kernel_drops_reported_at = drops, now

    @staticmethod
    def _major_type(type_name: str) -> str:
        """``type_name`` without its minor version: "ns.Rec_1_1" -> "ns.Rec_1".

        Minor versions of one major version are compatible on the wire, so
        only a different type or major version is a conflict.
        """
        return re.sub(r'_(\d+)_\d+$', r'_\1', type_name)

    def _report_type_conflict(self, subject_id: int, node_id: int, advertised: str, subscribed: str) -> None:
        """A publisher advertises another type on a subject already decoded as ``subscribed``.

        The subject stays decoded as the first publisher's type, so this
        node's messages on it may decode wrongly or not at all; say so.
        """
        logging.warning(
            f"Subject {subject_id}: node {node_id} publishes {advertised}, but the subject is "
            f"decoded as {subscribed}, which an earlier publisher advertised"
        )
        self._emit_node_event(node_id, "type_conflict", {
            "subject_id": subject_id, "type": advertised, "decoded_as": subscribed,
        })

    def _extract_attributes(self, msg: Any, field_names: list[str]) -> list[dict]:
        """Recursively extract attributes from a DSDL message, flattening nested composites."""
        results: list[dict] = []
        for name in field_names:
            val = getattr(msg, name, None)
            if val is None:
                continue
            self._extract_value(name, val, results)
        return results

    def _extract_value(self, name: str, val: Any, results: list[dict]) -> None:
        """Extract a single value, recursing into composite DSDL types."""
        # Byte arrays / strings
        if isinstance(val, (bytes, bytearray)):
            results.append({"attribute": name, "value": val.decode("utf-8", errors="ignore")})
        # DSDL strings arrive as uint8 arrays; any other array is numbers.
        elif isinstance(val, np.ndarray) and val.dtype == np.uint8:
            results.append({"attribute": name, "value": bytes(val).decode("utf-8", errors="ignore")})
        # An array of composites (port.List's SubjectID[]) holds DSDL objects:
        # each as builtins, a one-field type (an ID) as its value alone.
        elif isinstance(val, np.ndarray) and val.dtype == object:
            items = [to_builtin(item) for item in val]
            results.append({"attribute": name, "value": [
                next(iter(item.values())) if isinstance(item, dict) and len(item) == 1 else item for item in items
            ]})
        # A port list's SubjectIDList as a mask (8192 bits): the IDs it sets.
        elif isinstance(val, np.ndarray) and val.dtype == bool and name.endswith(".mask"):
            results.append({"attribute": name, "value": np.flatnonzero(val).tolist()})
        elif isinstance(val, np.ndarray):
            results.append({"attribute": name, "value": val.tolist()})
        # Primitives
        elif isinstance(val, (str, int, float, bool, np.integer, np.floating)):
            results.append({"attribute": name, "value": self._make_json_serializable(val)})
        # Nested composite DSDL type — recurse into its fields
        else:
            try:
                nested_model = get_model(type(val))
                nested_fields = [a.name for a in nested_model.attributes if isinstance(a, Field)]
                # Single-field wrappers (e.g. enums): unwrap, keep parent name
                if len(nested_fields) == 1:
                    field_name = nested_fields[0]
                    sub_val = getattr(val, field_name, None)
                    # A mask (port.ServiceIDList's 512 bits): the IDs it sets, not 512 booleans.
                    if field_name == "mask" and isinstance(sub_val, np.ndarray) and sub_val.dtype == bool:
                        results.append({"attribute": name, "value": np.flatnonzero(sub_val).tolist()})
                    elif sub_val is not None:
                        self._extract_value(name, sub_val, results)
                        # Add unit hint from field name (skip generic "value")
                        if field_name != "value" and results and results[-1]["attribute"] == name:
                            results[-1]["unit"] = field_name
                else:
                    for field_name in nested_fields:
                        sub_val = getattr(val, field_name, None)
                        if sub_val is not None:
                            self._extract_value(f"{name}.{field_name}", sub_val, results)
            except Exception:
                results.append({"attribute": name, "value": str(val)})

    def _make_json_serializable(self, value: Any) -> Any:
        """
        Convert numpy and other types to JSON-serializable Python types.
        
        Args:
            value: The value to convert.
        
        Returns:
            JSON-serializable value (int, float, str, bool, or list).
        """
        if isinstance(value, (np.integer, np.floating)):
            return value.item()  # Convert numpy scalar to Python scalar
        if isinstance(value, np.ndarray):
            return value.tolist()  # Convert array to list
        return value

    def _windowed_rate(self, times: collections.deque, now: float) -> float:
        """Messages per second in ``times`` over the rate window ending at ``now``."""
        cutoff = now - self.MESSAGE_RATE_WINDOW_SECONDS
        while times and times[0] <= cutoff:
            times.popleft()
        if len(times) < 2 or times[-1] == times[0]:
            return 0.0
        return (len(times) - 1) / (times[-1] - times[0])

    def _still_publishing(self, times: collections.deque, rate: float, now: float) -> bool:
        """Whether a publisher whose messages came at ``times`` still sends at ``now``."""
        period = 1.0 / rate if rate > 0 else 0.0
        return bool(times) and now - times[-1] <= max(self.PUBLISHING_MIN_SECONDS, self.PUBLISHING_PERIODS * period)

    def _track_rate(self, subject_id: int, node_id: Optional[int], timestamp: float) -> tuple[float, float]:
        """Record one message and return (this publisher's rate, the whole subject's rate).

        Rates are kept per publisher: several nodes publish the same subject
        (every node publishes Heartbeat), and a per-subject rate would credit
        each of them with all the others' traffic. The subject's rate counts
        only the publishers still sending: one that stopped would otherwise
        keep its full rate in it until its messages left the window.
        """
        publishers = self._rate_timestamps.setdefault(subject_id, {})
        times = publishers.setdefault(node_id, collections.deque())
        times.append(timestamp)
        publisher_rate = self._windowed_rate(times, timestamp)
        subject_rate = 0.0
        for other in publishers.values():
            other_rate = self._windowed_rate(other, timestamp)
            if self._still_publishing(other, other_rate, timestamp):
                subject_rate += other_rate
        return round(publisher_rate, 1), round(subject_rate, 1)

    @staticmethod
    def _transfer_times(transfer) -> tuple[float, float]:
        """(wall-clock, monotonic) receive time of ``transfer`` in seconds.

        pycyphal stamps a frame when the driver receives it (SocketCAN: in the
        kernel), which is earlier and steadier than the moment our callback
        runs. Falls back to now when no transfer is given.
        """
        stamp = getattr(transfer, "timestamp", None)
        try:
            return float(stamp.system), float(stamp.monotonic)  # type: ignore[union-attr]  # None: below
        except (AttributeError, TypeError, ValueError):
            return time.time(), time.monotonic()

    @staticmethod
    def _transfer_payload_bytes(transfer) -> Optional[int]:
        """Best-effort size of a received transfer's serialized payload, in bytes."""
        payload = getattr(transfer, "fragmented_payload", None)
        if payload is None:
            return None
        try:
            return sum(len(fragment) for fragment in payload)
        except (TypeError, AttributeError):
            return None

    async def _queue_event(self, subject_id: int, message_type: str, attributes: list,
                           publisher_node_id: Optional[int], payload_bytes: Optional[int] = None,
                           transfer=None) -> None:
        """Build a standardized event dict and put it on the message queue."""
        timestamp, monotonic = self._transfer_times(transfer)
        rate, subject_rate = self._track_rate(subject_id, publisher_node_id, monotonic)
        event: SubjectEvent = {
            "subject_id": subject_id,
            "timestamp": local_isoformat(timestamp, timespec="seconds"),
            "timestamp_unix": timestamp,
            "rate": rate,
            "subject_rate": subject_rate,
            "message_type": message_type,
            "attributes": attributes,
            "publisher_node_id": publisher_node_id,
            "unique_id": self._get_node_unique_id_hex(publisher_node_id) or self.identity_map.get_uid(publisher_node_id),
            "payload_bytes": payload_bytes,
        }
        await self._enqueue(event)

    async def _enqueue(self, event: SubjectEvent) -> None:
        try:
            self.message_queue.put_nowait(event)
        except asyncio.QueueFull:
            self.message_queue.get_nowait()
            self.dropped_events += 1
            await self.message_queue.put(event)

    async def queue_rates(self, rates: dict[tuple[int, int], tuple[float, float]]) -> None:
        """Report the rates of the subjects not decoded, as counted from their frames (transfer_rates).

        One event for each publisher of each such subject, as a message of
        its would give but with no values and ``"undecoded": true``: the rate
        and the time of the last message are what a dashboard shows of a
        subject nobody watches. ``rates``: {(subject-ID, node-ID): (messages
        a second, time of the last)}.
        """
        own = self._node.id
        reported = {key: value for key, value in rates.items()
                    if key[0] not in self.publishers_subscribers and key[0] not in self.ALWAYS_DECODED
                    and key[1] != own}
        subject_rates: dict[int, float] = collections.defaultdict(float)
        for (subject_id, _node_id), (rate, _last) in reported.items():
            subject_rates[subject_id] += rate
        for (subject_id, node_id), (rate, last_seen) in reported.items():
            await self._enqueue({
                "subject_id": subject_id,
                "timestamp": local_isoformat(last_seen, timespec="seconds"),
                "timestamp_unix": last_seen,
                "rate": round(rate, 1),
                "subject_rate": round(subject_rates[subject_id], 1),
                "message_type": self.subject_types.get(subject_id, "").rpartition(".")[2] or None,
                "attributes": [],
                "publisher_node_id": node_id,
                "unique_id": self._get_node_unique_id_hex(node_id) or self.identity_map.get_uid(node_id),
                "payload_bytes": None,
                "undecoded": True,
            })

    def _skip_backlog_if_behind(self, subject_id: int, transfer) -> None:
        """Let go of the frames ``subject_id`` has waiting, if ``transfer`` waited longer than may be."""
        try:
            lag = time.monotonic() - float(transfer.timestamp.monotonic)
        except (AttributeError, TypeError, ValueError):
            return
        subscriber = self.publishers_subscribers.get(subject_id)
        # A raw log played from a file is behind nothing: all of it is wanted.
        if lag <= self.MAX_DECODING_LAG_S or subscriber is None or self.offline is not None:
            return
        # Shrinking pycyphal's queue discards what does not fit and counts it
        # (see dropped_frames); unlimited again, it takes what arrives next.
        session = subscriber.transport_session
        session.frame_queue_capacity = 1
        session.frame_queue_capacity = None

    async def _publisher_callback(self, msg, transfer: pycyphal.transport.TransferFrom, subject_id: int) -> None:
        """Callback for publisher messages. Extracts attributes and queues standardized event."""
        if subject_id not in self.subject_attributes:
            logging.warning(f"Subject attributes not found for subject_id {subject_id}")
            return
        self._skip_backlog_if_behind(subject_id, transfer)

        attributes = self._extract_attributes(msg, self.subject_attributes[subject_id])
        await self._queue_event(
            subject_id=subject_id,
            message_type=msg.__class__.__name__,
            attributes=attributes,
            publisher_node_id=transfer.source_node_id,
            payload_bytes=self._transfer_payload_bytes(transfer),
            transfer=transfer,
        )

    def cleanup_subscriptions(self, node_id: int) -> None:
        self._snapshot_node_for_identity(node_id)
        self._prev_ports.pop(node_id, None)
        self._prev_health.pop(node_id, None)
        self._prev_mode.pop(node_id, None)
        self._prev_uptime.pop(node_id, None)
        self._uptime_before_drop.pop(node_id, None)
        self._emit_node_event(node_id, "disappeared")
        to_remove = []

        for subject_id, nodes in self.active_publishers.items():
            if node_id in nodes:
                nodes.remove(node_id)

            # The diagnostic subject is decoded for every node, whoever advertises
            # it, and a subject the user typed until the user says otherwise.
            if not nodes and subject_id in self.subject_types \
                    and subject_id != self.DIAGNOSTIC_SUBJECT_ID and subject_id not in self.user_subject_types:
                logging.info(f"Unsubscribing from subject {subject_id}, no active publishers")
                self._unsubscribe(subject_id)
                self.subject_types.pop(subject_id, None)
                to_remove.append(subject_id)

        for subject_id in to_remove:
            del self.active_publishers[subject_id]

        # Release per-node service state. Each entry in service_clients holds a
        # pycyphal Client object — if we don't close them here, they live until
        # the backend restarts (one-way ratchet during long debug sessions
        # that cycle many short-lived nodes). _migrate_node_state still closes
        # any clients that survive into an identity migration; once cleanup has
        # run on the old slot, that path is a no-op and the new node gets
        # fresh clients via add_servers — same end state, no stale references.
        stale_service_keys = [k for k in self.service_clients if k[0] == node_id]
        for key in stale_service_keys:
            client = self.service_clients.pop(key, None)
            if client is not None:
                try:
                    client.close()
                except Exception as e:
                    logging.warning(f"Failed to close service client {key}: {e}")
        for key in [k for k in self.service_metadata if k[0] == node_id]:
            self.service_metadata.pop(key, None)
        self.node_service_types.pop(node_id, None)


    HEALTH_NAMES = {0: "NOMINAL", 1: "ADVISORY", 2: "CAUTION", 3: "WARNING"}
    MODE_NAMES = {0: "OPERATIONAL", 1: "INITIALIZATION", 2: "MAINTENANCE", 3: "SOFTWARE_UPDATE"}
    MODE_SOFTWARE_UPDATE = 3

    def _get_node_unique_id_hex(self, node_id: Optional[int]) -> Optional[str]:
        node = None if node_id is None else self.all_nodes.get(node_id)
        if node and node.has_responded_to_getInfo and node.unique_id.size > 0:
            uid = bytes(node.unique_id).hex()  # run for every message: no loop in Python over its bytes
            if uid.strip('0'):
                return uid
        return None

    def _emit_node_event(self, node_id: int, event_type: str, detail: Optional[dict] = None) -> None:
        if self.on_node_event:
            uid = self._get_node_unique_id_hex(node_id) or self.identity_map.get_uid(node_id)
            _fire_and_log(self.on_node_event(node_id, event_type, detail, unique_id=uid), f"node_event({event_type})")

    async def port_callback(self, msg: uavcan.node.port.List_1_0, transfer: pycyphal.transport.TransferFrom):
        node_id = transfer.source_node_id
        if node_id is None:
            return  # an anonymous node may not send one

        node = self.all_nodes[node_id]
        def _port_ids(ids):
            return sorted(self._make_json_serializable(s.value) if hasattr(s, 'value') else int(s) for s in ids)

        node.set_port_list(port_list=msg)

        # Detect port changes
        new_ports = {
            "publishers": _port_ids(node.publisher_SubjectIDs) if node.has_publishers else [],
            "subscribers": _port_ids(node.subscriber_SubjectIDs) if node.has_subscribers else [],
            "servers": _port_ids(node.server_ServiceIDs) if node.has_servers else [],
            "clients": _port_ids(node.client_ServiceIDs) if node.has_clients else [],
        }
        prev = self._prev_ports.get(node_id)
        if prev is not None and new_ports != prev:
            self._emit_node_event(node_id, "port_change", {"old": prev, "new": new_ports})
        self._prev_ports[node_id] = new_ports

        # Count advertised ports for telemetry
        pub_count = len(node.publisher_SubjectIDs) if node.has_publishers else 0
        sub_count = len(node.subscriber_SubjectIDs) if node.has_subscribers else 0
        srv_count = len(node.server_ServiceIDs) if node.has_servers else 0
        clt_count = len(node.client_ServiceIDs) if node.has_clients else 0
        await self._queue_event(
            subject_id=self.PORT_LIST_SUBJECT_ID,
            message_type="List_1_0",
            attributes=[
                {"attribute": "publishers", "value": pub_count},
                {"attribute": "subscribers", "value": sub_count},
                {"attribute": "servers", "value": srv_count},
                {"attribute": "clients", "value": clt_count},
            ],
            publisher_node_id=node_id,
            transfer=transfer,
        )

    async def heartbeat_callback(self, msg: uavcan.node.Heartbeat_1_0, transfer: pycyphal.transport.TransferFrom):
        node_id = transfer.source_node_id
        if node_id is None:
            return  # an anonymous node may not send one; it would be no node in the list
        node    = self.all_nodes[node_id]
        now     = datetime.datetime.now()

        # Cynitor never hears its own heartbeats: one from its node-ID is another node's.
        if node_id == getattr(getattr(self, "_node", None), "id", None):
            self._report_own_node_id_taken(node_id, self._make_json_serializable(msg.uptime))

        if not node.has_appeared:
            node.mark_appeared(first_seen=now)
            logging.debug(f"Node {node_id} has appeared for the first time.")
            self._emit_node_event(node_id, "first_seen")
            self._schedule_info_refresh(node_id, was_disappeared=False)

        else:
            was_disappeared = node.has_disappeared
            node.mark_seen(last_seen=now)

            if was_disappeared:
                self._prev_ports.pop(node_id, None)

            self._check_uptime(node_id, self._make_json_serializable(msg.uptime))

            node.uptime = msg.uptime

            # Refresh getInfo: immediately on reappearance, otherwise periodically
            scheduled = (was_disappeared or self._info_due(node, now)) and \
                self._schedule_info_refresh(node_id, was_disappeared)
            if was_disappeared and not scheduled:
                self._emit_node_event(node_id, "reappeared")

        # Track health/mode changes
        health_val = self._make_json_serializable(msg.health.value)
        mode_val = self._make_json_serializable(msg.mode.value)

        prev_health = self._prev_health.get(node_id)
        if prev_health is not None and health_val != prev_health:
            self._emit_node_event(node_id, "health_change", {
                "old": self.HEALTH_NAMES.get(prev_health, str(prev_health)),
                "new": self.HEALTH_NAMES.get(health_val, str(health_val)),
            })
        self._prev_health[node_id] = health_val

        prev_mode = self._prev_mode.get(node_id)
        if prev_mode is not None and mode_val != prev_mode:
            self._emit_node_event(node_id, "mode_change", {
                "old": self.MODE_NAMES.get(prev_mode, str(prev_mode)),
                "new": self.MODE_NAMES.get(mode_val, str(mode_val)),
            })
            if prev_mode == self.MODE_SOFTWARE_UPDATE:  # new firmware runs: its version, at once
                self._schedule_info_refresh(node_id, was_disappeared=False)
        self._prev_mode[node_id] = mode_val

        self._prev_uptime[node_id] = self._make_json_serializable(msg.uptime)

        # Queue heartbeat as a telemetry event
        health_val = self._make_json_serializable(msg.health.value)
        mode_val = self._make_json_serializable(msg.mode.value)
        await self._queue_event(
            subject_id=self.HEARTBEAT_SUBJECT_ID,
            message_type="Heartbeat_1_0",
            attributes=[
                {"attribute": "uptime", "value": self._make_json_serializable(msg.uptime), "unit": "s"},
                {"attribute": "health", "value": self.HEALTH_NAMES.get(health_val, str(health_val))},
                {"attribute": "mode", "value": self.MODE_NAMES.get(mode_val, str(mode_val))},
                {"attribute": "vssc", "value": self._make_json_serializable(msg.vendor_specific_status_code)},
            ],
            publisher_node_id=node_id,
            transfer=transfer,
        )

    async def pnp_v2_callback(self, msg: uavcan.pnp.NodeIDAllocationData_2_0, transfer: pycyphal.transport.TransferFrom):
        raw_uid = msg.unique_id
        unique_id = bytes(raw_uid).hex() if hasattr(raw_uid, '__iter__') else str(raw_uid)
        node_id_value = self._make_json_serializable(msg.node_id.value)
        logging.info(f"PnP v2 allocation: unique_id={unique_id}, node_id={node_id_value}")

        if isinstance(node_id_value, int) and 0 <= node_id_value < self.NUM_NODES:
            old_node_id = self.identity_map.register(node_id_value, unique_id)
            if old_node_id is not None:
                self._migrate_node_state(old_node_id, node_id_value, unique_id)

        attrs = [
            {"attribute": "unique_id", "value": unique_id},
            {"attribute": "node_id", "value": node_id_value},
        ]
        await self._queue_event(
            subject_id=self.PNP_SUBJECT_ID_V2,
            message_type="NodeIDAllocationData_2_0",
            attributes=attrs,
            publisher_node_id=transfer.source_node_id if transfer.source_node_id is not None else -1,
            transfer=transfer,
        )

    async def pnp_v1_callback(self, msg: uavcan.pnp.NodeIDAllocationData_1_0, transfer: pycyphal.transport.TransferFrom):
        unique_id_hash = self._make_json_serializable(msg.unique_id_hash)
        allocated = msg.allocated_node_id
        allocated_id = self._make_json_serializable(allocated[0].value) if len(allocated) > 0 else None
        logging.info(f"PnP v1 allocation: unique_id_hash={unique_id_hash}, allocated_node_id={allocated_id}")
        attrs = [{"attribute": "unique_id_hash", "value": unique_id_hash}]
        if allocated_id is not None:
            attrs.append({"attribute": "allocated_node_id", "value": allocated_id})
        await self._queue_event(
            subject_id=self.PNP_SUBJECT_ID_V1,
            message_type="NodeIDAllocationData_1_0",
            attributes=attrs,
            publisher_node_id=transfer.source_node_id if transfer.source_node_id is not None else -1,
            transfer=transfer,
        )

    def _snapshot_node_for_identity(self, node_id: int) -> None:
        """Save last known state of a node into the identity map before it gets displaced."""
        try:
            uid = self.identity_map.get_uid(node_id)
            if not uid:
                return
            node = self.all_nodes.get(node_id)
            if not node or not node.has_appeared or not node.has_responded_to_getInfo:
                return
            info = getattr(node, "info_response", None)

            def _ids(attr):
                items = getattr(node, attr, [])
                return sorted(self._make_json_serializable(s.value) if hasattr(s, 'value') else int(s) for s in items)

            name = None
            if info:
                n = info.name
                if hasattr(n, 'tobytes'):
                    n = n.tobytes().decode('utf-8', errors='replace')
                name = str(n) if n else None

            snapshot = {
                "unique_id": [int(b) for b in node.unique_id] if hasattr(node.unique_id, '__iter__') else [],
                "name": name or self.identity_map._node_names.get(uid),
                "software_version": {
                    "major": self._make_json_serializable(info.software_version.major),
                    "minor": self._make_json_serializable(info.software_version.minor),
                } if info else None,
                "publishers": _ids("publisher_SubjectIDs") if getattr(node, 'has_publishers', False) else [],
                "subscribers": _ids("subscriber_SubjectIDs") if getattr(node, 'has_subscribers', False) else [],
                "servers": _ids("server_ServiceIDs") if getattr(node, 'has_servers', False) else [],
                "clients": _ids("client_ServiceIDs") if getattr(node, 'has_clients', False) else [],
                "uptime": self._make_json_serializable(node.uptime) if getattr(node, 'uptime', None) else None,
                "last_seen": local_isoformat(node.last_seen[-1]) if getattr(node, 'last_seen', None) and len(node.last_seen) else None,
            }
            self.identity_map.save_node_snapshot(uid, snapshot)
            if self.on_node_data_save:
                _fire_and_log(self.on_node_data_save(uid, snapshot), "node_data_save")
        except Exception as e:
            logging.warning(f"Failed to snapshot node {node_id}: {e}")

    def _migrate_node_state(self, old_node_id: int, new_node_id: int, unique_id_hex: str) -> None:
        """Transfer accumulated state from old node_id slot to new one after re-allocation."""
        logging.info(f"Migrating node state: node_id {old_node_id} -> {new_node_id} (unique_id={unique_id_hex})")

        for subject_id, publishers in self.active_publishers.items():
            if old_node_id in publishers:
                publishers.discard(old_node_id)
                publishers.add(new_node_id)

        old_service_types = self.node_service_types.pop(old_node_id, None)
        if old_service_types:
            self.node_service_types[new_node_id] = old_service_types

        old_keys = [k for k in self.service_metadata if k[0] == old_node_id]
        for old_key in old_keys:
            service_id = old_key[1]
            self.service_metadata[(new_node_id, service_id)] = self.service_metadata.pop(old_key)
            client = self.service_clients.pop(old_key, None)
            if client:
                client.close()

        for tracker in (self._prev_health, self._prev_mode, self._prev_uptime, self._uptime_before_drop):
            val = tracker.pop(old_node_id, None)
            if val is not None:
                tracker[new_node_id] = val

        prev_ports = self._prev_ports.pop(old_node_id, None)
        if prev_ports is not None:
            self._prev_ports[new_node_id] = prev_ports

        self.all_nodes[old_node_id] = NodeInfo(node_id=old_node_id)

        self._emit_node_event(new_node_id, "node_id_migration", {
            "old_node_id": old_node_id,
            "new_node_id": new_node_id,
            "unique_id": unique_id_hex,
        })

    def _check_uptime(self, node_id: int, uptime: int) -> None:
        """Tell a node restarting from two nodes sharing one node-ID.

        Both make the heartbeat's uptime drop. After a restart the uptime then
        counts on from its new value; with two nodes the next heartbeat usually
        comes from the other one, back on the old count. So a drop is judged
        on the heartbeat after it.
        """
        prev = self._prev_uptime.get(node_id)
        before_drop = self._uptime_before_drop.pop(node_id, None)
        if before_drop is not None:
            if uptime >= before_drop:
                self._report_node_id_conflict(node_id, prev, uptime)
            else:
                self._emit_node_event(node_id, "restart_suspected", {
                    "old_uptime": before_drop, "new_uptime": prev,
                })
        elif prev is not None and uptime < prev and prev > 5:
            self._uptime_before_drop[node_id] = prev

    def _report_node_id_conflict(self, node_id: int, uptime_a: Optional[int], uptime_b: int) -> None:
        now = time.monotonic()
        last = self._node_id_conflict_reported.get(node_id)
        if last is not None and now - last < self.NODE_ID_CONFLICT_REPORT_S:
            return
        self._node_id_conflict_reported[node_id] = now
        logging.warning(
            f"Node-ID {node_id} is used by more than one node: its heartbeats "
            f"alternate between uptimes {uptime_a}s and {uptime_b}s"
        )
        self._emit_node_event(node_id, "node_id_conflict", {"uptimes": [uptime_a, uptime_b]})

    def _report_own_node_id_taken(self, node_id: int, uptime: int) -> None:
        """Another node sends heartbeats from Cynitor's own node-ID: calls to it reach either of the two."""
        now = time.monotonic()
        last = self._node_id_conflict_reported.get(node_id)
        if last is not None and now - last < self.NODE_ID_CONFLICT_REPORT_S:
            return
        self._node_id_conflict_reported[node_id] = now
        message = (f"Another node uses Cynitor's own node-ID {node_id} (its uptime {uptime} s): "
                   f"what other nodes send to node {node_id} may reach Cynitor instead. "
                   f"Disconnect and connect again for a free node-ID")
        logging.warning(message)
        self._emit_node_event(node_id, "node_id_conflict", {"own_node_id": True, "uptime": uptime})
        if self.on_own_node_id_conflict is not None:
            self.on_own_node_id_conflict(message)

    def _info_due(self, node: NodeInfo, now: datetime.datetime) -> bool:
        """Whether a node's GetInfo should be sent again: periodically once it
        has answered, sooner while it has not."""
        if node.last_info_attempt is None:
            return True
        interval = self.INFO_REFRESH_S if node.has_responded_to_getInfo else self.INFO_RETRY_S
        return (now - node.last_info_attempt).total_seconds() >= interval

    def _schedule_info_refresh(self, node_id: int, was_disappeared: bool) -> bool:
        """Start a GetInfo refresh in the background; False if one is already running.

        GetInfo can take up to the response timeout. Awaiting it inside the
        heartbeat callback would hold up every other node's heartbeats behind it.
        """
        if node_id in self._info_in_flight or self.offline is not None or self.listen_only:
            return False
        self._info_in_flight.add(node_id)
        self.all_nodes[node_id].last_info_attempt = datetime.datetime.now()
        _fire_and_log(self._refresh_info(node_id, was_disappeared), f"getInfo({node_id})")
        return True

    async def _refresh_info(self, node_id: int, was_disappeared: bool) -> None:
        """Run GetInfo and handle a changed identity behind the same node-ID."""
        try:
            node = self.all_nodes[node_id]
            old_unique_id = ''.join(f'{byte:02x}' for byte in node.unique_id) if node.has_responded_to_getInfo else None
            await self.getInfo(node_id)
            new_unique_id = ''.join(f'{byte:02x}' for byte in node.unique_id) if node.has_responded_to_getInfo else None

            is_replacement = old_unique_id and new_unique_id and old_unique_id != new_unique_id
            if is_replacement and new_unique_id:
                known_nid = self.identity_map.get_nid(new_unique_id)
                if known_nid is None or known_nid == node_id:
                    logging.info(f"Node {node_id} identity changed: {old_unique_id} -> {new_unique_id}. Resetting node state.")
                    self._prev_ports.pop(node_id, None)
                    self._prev_health.pop(node_id, None)
                    self._prev_mode.pop(node_id, None)
                    self._prev_uptime.pop(node_id, None)
                    self._uptime_before_drop.pop(node_id, None)
                    self.all_nodes[node_id] = NodeInfo(node_id=node.node_id)
                    await self.getInfo(node_id)
            elif was_disappeared:
                self._emit_node_event(node_id, "reappeared")
        finally:
            self._info_in_flight.discard(node_id)

    def _no_answer_hint(self) -> str:
        """What to check when a node sends heartbeats but answers nothing."""
        if self.get_transport_info().get("protocol", {}).get("is_fd"):
            # Cynitor then sends CAN FD frames, which a Classic CAN node never
            # receives, while its own Classic frames still reach Cynitor.
            return (" This bus runs CAN FD: a node in Classic CAN mode receives none of "
                    "Cynitor's requests. Run it in CAN FD too, or set the interface to Classic CAN.")
        return ""

    async def getInfo(self, node_id: int) -> None:
        info_client    = self._node.make_client(uavcan.node.GetInfo_1, node_id)
        try:
            request         = uavcan.node.GetInfo_1.Request()
            response        = await info_client.call(request)
            if response is None:
                logging.warning(f"Node {node_id} does not answer GetInfo, though its heartbeats "
                                f"arrive.{self._no_answer_hint()}")
                return
            self._snapshot_node_for_identity(node_id)
            self.all_nodes[node_id].set_info(get_info_response=response[0], transfer_from=response[1])
            self.all_nodes[node_id].last_info_time = datetime.datetime.now()
            logging.debug(f"Node {node_id} has responded to getInfo service.")

            uid_hex = self._get_node_unique_id_hex(node_id)
            if uid_hex:
                displaced_uid = self.identity_map.get_uid(node_id)
                if displaced_uid and displaced_uid != uid_hex and self.on_node_event:
                    _fire_and_log(self.on_node_event(node_id, "lost_node_id", {"node_id": node_id}, unique_id=displaced_uid), "node_event(lost_node_id)")
                prev_nid = self.identity_map.get_nid(uid_hex)
                old_node_id = self.identity_map.register(node_id, uid_hex)
                if prev_nid != node_id:
                    self._emit_node_event(node_id, "got_node_id", {"node_id": node_id})
                if old_node_id is not None:
                    self._snapshot_node_for_identity(old_node_id)
                    self._migrate_node_state(old_node_id, node_id, uid_hex)
                self._snapshot_node_for_identity(node_id)
                name = response[0].name
                if hasattr(name, 'tobytes'):
                    name = name.tobytes().decode('utf-8', errors='replace')
                if name:
                    self.identity_map.set_node_name(uid_hex, str(name))
        except Exception as e:
            logging.warning(f"Node {node_id} could not provide GetInfo service. Threw {e}")
        finally:
            info_client.close()

    def close(self) -> None:
        self._node.close()

    def node_mode(self, node_id: int) -> Optional[str]:
        """The mode in the node's latest heartbeat (e.g. SOFTWARE_UPDATE), or None if none was seen."""
        mode = self._prev_mode.get(node_id)
        return None if mode is None else self.MODE_NAMES.get(mode, str(mode))

    @property
    def node(self):
        return self._node

    @property
    def nodes(self):
        return self.all_nodes