"""Tests for ScannerNode port-register resolution.

Publishers and services are advertised as "uavcan.<pub|srv>.<port_name>.id",
so the numeric port-ID comes from the register's natural16 value and the DSDL
type name from the sibling ".type" register.
"""

import asyncio
import datetime
import inspect
import json
import socket
import struct
import sys
import time
import types
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import numpy as np

import pytest

from node_info import NodeInfo
import scanner_node
import scanner_services
from scanner_node import ScannerNode  # uavcan.* is stubbed in conftest.py


class FakeRegisters:
    """Serves canned (field_name, value) pairs keyed by register name."""

    def __init__(self, responses):
        self.responses = responses
        self.reads = []

    async def read(self, _client, reg_name, _node_id):
        self.reads.append(reg_name)
        return self.responses.get(reg_name)


def make_node(responses):
    """A ScannerNode with register I/O stubbed out (no __init__, no CAN stack)."""
    node = ScannerNode.__new__(ScannerNode)
    fake = FakeRegisters(responses)
    node._read_register = fake.read
    return node, fake


async def resolve(node, reg_name, max_port_id=ScannerNode.MAX_SUBJECT_ID, label="subject"):
    return await node._resolve_port_register(None, reg_name, 42, max_port_id, label)


class TestResolvePortRegister:
    @pytest.mark.asyncio
    async def test_resolves_subject_id_and_type(self):
        node, fake = make_node({
            "uavcan.pub.temperature.id": ("natural16", [1620]),
            "uavcan.pub.temperature.type": ("string", "uavcan.si.sample.temperature.Scalar.1.0"),
        })
        result = await resolve(node, "uavcan.pub.temperature.id")
        # Version dots become underscores so the name maps to a Python module.
        assert result == (1620, "uavcan.si.sample.temperature.Scalar_1_0")
        # The type register is derived from the id register's own name.
        assert fake.reads == ["uavcan.pub.temperature.id", "uavcan.pub.temperature.type"]

    @pytest.mark.asyncio
    async def test_resolves_named_service_port(self):
        node, _ = make_node({
            "uavcan.srv.calibrate_pwm_0.id": ("natural16", [430]),
            "uavcan.srv.calibrate_pwm_0.type": ("string", "dontpanic.CalibratePWM.1.0"),
        })
        result = await resolve(node, "uavcan.srv.calibrate_pwm_0.id",
                               ScannerNode.MAX_SERVICE_ID, "service")
        assert result == (430, "dontpanic.CalibratePWM_1_0")

    @pytest.mark.asyncio
    async def test_resolves_legacy_numeric_service_register(self):
        # The old "uavcan.srv.<digits>.id" form is a subset of the named shape.
        node, _ = make_node({
            "uavcan.srv.430.id": ("natural16", [430]),
            "uavcan.srv.430.type": ("string", "dontpanic.SumService.2.0"),
        })
        result = await resolve(node, "uavcan.srv.430.id",
                               ScannerNode.MAX_SERVICE_ID, "service")
        assert result == (430, "dontpanic.SumService_2_0")

    @pytest.mark.asyncio
    async def test_rejects_out_of_range_id(self):
        node, fake = make_node({
            "uavcan.pub.bad.id": ("natural16", [9000]),
            "uavcan.pub.bad.type": ("string", "uavcan.primitive.Empty.1.0"),
        })
        assert await resolve(node, "uavcan.pub.bad.id") is None
        # Bails out before spending a round-trip on the type register.
        assert fake.reads == ["uavcan.pub.bad.id"]

    @pytest.mark.asyncio
    async def test_unset_id_is_an_inactive_port_not_a_warning(self, caplog):
        # 65535 is the standard's "unset": a port the node has but does not use.
        node, fake = make_node({"uavcan.pub.uavcan.node.heartbeat.id": ("natural16", [65535])})
        with caplog.at_level("WARNING"):
            assert await resolve(node, "uavcan.pub.uavcan.node.heartbeat.id") is None
        assert caplog.text == ""
        assert fake.reads == ["uavcan.pub.uavcan.node.heartbeat.id"]

    @pytest.mark.asyncio
    async def test_rejects_service_id_above_service_range(self):
        # 1000 is a valid subject-ID but not a valid service-ID.
        node, _ = make_node({
            "uavcan.srv.x.id": ("natural16", [1000]),
            "uavcan.srv.x.type": ("string", "dontpanic.StartMotor.1.0"),
        })
        assert await resolve(node, "uavcan.srv.x.id",
                             ScannerNode.MAX_SERVICE_ID, "service") is None

    @pytest.mark.asyncio
    async def test_rejects_non_natural16_id_register(self):
        node, _ = make_node({"uavcan.pub.x.id": ("string", "1620")})
        assert await resolve(node, "uavcan.pub.x.id") is None

    @pytest.mark.asyncio
    async def test_rejects_empty_id_value(self):
        node, _ = make_node({"uavcan.pub.x.id": ("natural16", [])})
        assert await resolve(node, "uavcan.pub.x.id") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_id_register_unreadable(self):
        node, _ = make_node({})
        assert await resolve(node, "uavcan.pub.missing.id") is None

    @pytest.mark.asyncio
    async def test_returns_none_when_type_register_unreadable(self):
        node, _ = make_node({"uavcan.pub.x.id": ("natural16", [1620])})
        assert await resolve(node, "uavcan.pub.x.id") is None

    @pytest.mark.asyncio
    async def test_rejects_non_string_type_register(self):
        node, _ = make_node({
            "uavcan.pub.x.id": ("natural16", [1620]),
            "uavcan.pub.x.type": ("natural16", [7]),
        })
        assert await resolve(node, "uavcan.pub.x.id") is None


class TestReadRegister:
    @pytest.mark.asyncio
    async def test_raises_when_node_does_not_answer(self):
        # A lost response is not a missing register: registration must fail
        # and be retried, not carry on without the port.
        class SilentClient:
            async def call(self, _request):
                return None

        node = ScannerNode.__new__(ScannerNode)
        with pytest.raises(TimeoutError):
            await node._read_register(SilentClient(), "uavcan.pub.x.id", 42)

    @pytest.mark.asyncio
    async def test_returns_parsed_union_field(self):
        class Client:
            async def call(self, _request):
                return (MagicMock(), MagicMock())

        node = ScannerNode.__new__(ScannerNode)
        node._find_non_none_field = lambda _value: ("natural16", [1620])
        result = await node._read_register(Client(), "uavcan.pub.x.id", 42)
        assert result == ("natural16", [1620])


class TestTypeNameCase:
    """Firmware that advertises type names in the wrong case still resolves."""

    @pytest.fixture
    def compiled(self):
        # Stands in for compiled DSDL: uavcan.register with its real spellings.
        module = types.SimpleNamespace(Access_1_0=object(), List_1_0=object())
        modules = {"uavcan.register": module}

        def import_module(name):
            if name not in modules:
                raise ImportError(name)
            return modules[name]

        with patch("scanner_node.importlib.import_module", side_effect=import_module):
            yield

    @pytest.mark.asyncio
    async def test_lowercase_service_type_is_corrected(self, compiled, caplog):
        node, _ = make_node({
            "uavcan.srv.register_access.id": ("natural16", [384]),
            "uavcan.srv.register_access.type": ("string", "uavcan.register.access.1.0"),
        })
        result = await resolve(node, "uavcan.srv.register_access.id",
                               ScannerNode.MAX_SERVICE_ID, "service")
        assert result == (384, "uavcan.register.Access_1_0")
        # The firmware is still wrong; the log says where.
        assert "uavcan.srv.register_access.type" in caplog.text
        assert "case-sensitive" in caplog.text

    def test_exact_name_is_kept(self, compiled):
        assert ScannerNode._canonical_type_name("uavcan.register.List_1_0") == "uavcan.register.List_1_0"

    def test_unknown_type_is_left_for_the_caller_to_report(self, compiled):
        assert ScannerNode._canonical_type_name("uavcan.register.nope_1_0") == "uavcan.register.nope_1_0"

    def test_unknown_namespace_is_left_alone(self, compiled):
        assert ScannerNode._canonical_type_name("vendor.thing.Foo_1_0") == "vendor.thing.Foo_1_0"

    def test_ambiguous_match_is_not_guessed(self):
        module = types.SimpleNamespace(Foo_1_0=object(), FOO_1_0=object())
        with patch("scanner_node.importlib.import_module", return_value=module):
            assert ScannerNode._canonical_type_name("ns.foo_1_0") == "ns.foo_1_0"


class TestUpdateRegListLostResponse:
    """No answer to register.List must not read as "end of list", nor as "no registers"
    unless the node's port.List says it serves no register.List."""

    @staticmethod
    def unanswering(port_list_servers=None):
        """A node that answers no register.List request; with ``port_list_servers``, the
        services its port.List names, else no port.List of it has come."""
        list_client = MagicMock()
        list_client.call = AsyncMock(return_value=None)
        node = ScannerNode.__new__(ScannerNode)
        node._node = MagicMock()
        node._node.make_client.return_value = list_client
        info = NodeInfo(node_id=42)
        if port_list_servers is not None:
            info.has_published_port_list = True
            info.server_ServiceIDs = list(port_list_servers)
        node.all_nodes = {42: info}
        return node, list_client

    @pytest.mark.asyncio
    async def test_unanswered_list_request_fails_instead_of_truncating(self):
        node, list_client = self.unanswering(port_list_servers=[384, 385, 430])
        with patch("scanner_node.asyncio.sleep", AsyncMock()):
            with pytest.raises(TimeoutError):
                await node.update_reg_list(42)
        assert list_client.call.await_count == 2  # one retry before giving up

    @pytest.mark.asyncio
    async def test_without_a_port_list_no_answer_is_a_lost_response(self):
        node, _ = self.unanswering()
        with patch("scanner_node.asyncio.sleep", AsyncMock()):
            with pytest.raises(TimeoutError):
                await node.update_reg_list(42)

    @pytest.mark.asyncio
    async def test_a_node_that_serves_no_register_list_gets_the_standard_services(self, caplog):
        # A lib_cordicor_cpp node serves GetInfo and ExecuteCommand but no
        # registers: it was retried for ever, and its services had no client.
        node, list_client = self.unanswering(port_list_servers=[430, 435])
        with patch("scanner_node.asyncio.sleep", AsyncMock()), caplog.at_level("INFO"):
            pubs, srvs = await node.update_reg_list(42)
        assert pubs == {}
        assert set(srvs) == set(ScannerNode.STANDARD_SERVICES)
        assert list_client.call.await_count == 2  # asked, as a node may leave a server out of its port.List
        assert "Node 42 has no registers" in caplog.text


def make_rate_node():
    node = ScannerNode.__new__(ScannerNode)
    node._rate_timestamps = {}
    return node


class TestRate:
    def test_rate_is_per_publisher_and_subject_rate_is_total(self):
        node = make_rate_node()
        for second in range(5):
            for node_id in (10, 11, 12):  # three nodes, 1 Hz heartbeat each
                rate, subject_rate = node._track_rate(7509, node_id, float(second))
        assert rate == 1.0
        assert subject_rate == 3.0

    def test_rate_keeps_one_decimal(self):
        node = make_rate_node()
        for t in (0.0, 0.4, 0.8, 1.2):  # 2.5 Hz
            rate, _ = node._track_rate(1, 5, t)
        assert rate == 2.5

    def test_silent_publisher_drops_out_of_subject_rate(self):
        node = make_rate_node()
        for t in range(3):
            node._track_rate(1, 5, float(t))
        _, subject_rate = node._track_rate(1, 6, 100.0)
        assert subject_rate == 0.0

    def test_publisher_that_stops_leaves_subject_rate_within_three_periods(self):
        """Not 10 s later, when its messages leave the window: the dashboard calls it silent by then."""
        node = make_rate_node()
        for tick in range(50):  # both publishers at 10 Hz for 5 s
            node._track_rate(1, 5, tick / 10)
            node._track_rate(1, 6, tick / 10 + 0.05)
        for tick in range(50, 80):  # 5 stops; 6 goes on for 3 s
            _, subject_rate = node._track_rate(1, 6, tick / 10 + 0.05)
        assert subject_rate == 10.0

    def test_slow_publisher_still_counts_between_its_messages(self):
        node = make_rate_node()
        for t in (0.0, 5.0):  # 0.2 Hz: three periods are 15 s
            node._track_rate(1, 5, t)
        node._track_rate(1, 6, 8.0)
        _, subject_rate = node._track_rate(1, 6, 9.0)
        assert subject_rate == 1.2  # 0.2 Hz from 5, 1 Hz from 6


class TestTransferTimes:
    def test_uses_driver_timestamp(self):
        transfer = MagicMock()
        transfer.timestamp.system = Decimal("1700000000.25")
        transfer.timestamp.monotonic = Decimal("12.5")
        assert ScannerNode._transfer_times(transfer) == (1700000000.25, 12.5)

    def test_falls_back_to_now_without_transfer(self):
        before = time.time()
        system, _ = ScannerNode._transfer_times(None)
        assert system >= before


class TestInfoRefresh:
    def make(self):
        node = ScannerNode.__new__(ScannerNode)
        node._info_in_flight = set()
        node.all_nodes = {42: NodeInfo(node_id=42)}
        node._refresh_info = AsyncMock()
        return node

    @pytest.mark.asyncio
    async def test_only_one_refresh_per_node_at_a_time(self):
        node = self.make()
        assert node._schedule_info_refresh(42, was_disappeared=False) is True
        assert node._schedule_info_refresh(42, was_disappeared=False) is False
        await asyncio.sleep(0)
        node._refresh_info.assert_awaited_once_with(42, False)

    def test_unanswered_node_is_retried_sooner_than_a_known_one(self):
        node = self.make()
        info = node.all_nodes[42]
        now = datetime.datetime.now()
        assert node._info_due(info, now)  # never asked
        info.last_info_attempt = now - datetime.timedelta(seconds=ScannerNode.INFO_RETRY_S + 1)
        assert node._info_due(info, now)
        info.has_responded_to_getInfo = True
        assert not node._info_due(info, now)
        info.last_info_attempt = now - datetime.timedelta(seconds=ScannerNode.INFO_REFRESH_S + 1)
        assert node._info_due(info, now)


def subscriber_that_dropped(frames):
    """A pycyphal subscriber whose frame queue overflowed by ``frames``."""
    subscriber = MagicMock()
    subscriber.transport_session.sample_statistics.return_value = SimpleNamespace(drops=frames)
    return subscriber


class TestFramesDroppedBeforeDecoding:
    """A subject that outruns the decoding loses its backlog, counted, rather than fall ever further behind."""

    def make(self, lag_s: float, offline=None):
        """A scanner decoding subject 1620, and a message of it that waited ``lag_s`` for its turn."""
        node = ScannerNode.__new__(ScannerNode)
        node.offline = offline
        subscriber = MagicMock()
        subscriber.transport_session.frame_queue_capacity = None
        node.publishers_subscribers = {1620: subscriber}
        transfer = SimpleNamespace(timestamp=SimpleNamespace(monotonic=time.monotonic() - lag_s, system=time.time()))
        return node, subscriber.transport_session, transfer

    def test_a_message_that_waited_too_long_lets_go_of_what_waits_behind_it(self):
        node, session, transfer = self.make(ScannerNode.MAX_DECODING_LAG_S + 0.5)
        type(session).frame_queue_capacity = capacity = PropertyMock(return_value=None)
        node._skip_backlog_if_behind(1620, transfer)
        # pycyphal's queue shrunk to one frame, which discards and counts the rest, then unlimited again.
        assert [call.args[0] for call in capacity.call_args_list] == [1, None]

    def test_a_message_in_time_leaves_the_queue_alone(self):
        node, session, transfer = self.make(ScannerNode.MAX_DECODING_LAG_S / 2)
        type(session).frame_queue_capacity = capacity = PropertyMock(return_value=None)
        node._skip_backlog_if_behind(1620, transfer)
        capacity.assert_not_called()

    def test_a_raw_log_played_from_a_file_is_decoded_whole(self):
        node, session, transfer = self.make(60.0, offline={"version": 1})
        type(session).frame_queue_capacity = capacity = PropertyMock(return_value=None)
        node._skip_backlog_if_behind(1620, transfer)
        capacity.assert_not_called()

    def test_a_transfer_without_a_time_is_let_be(self):
        node, session, _ = self.make(60.0)
        type(session).frame_queue_capacity = capacity = PropertyMock(return_value=None)
        node._skip_backlog_if_behind(1620, SimpleNamespace())
        capacity.assert_not_called()

    def test_dropped_frames_are_summed_over_the_subjects(self):
        node = ScannerNode.__new__(ScannerNode)
        node.publishers_subscribers = {1620: subscriber_that_dropped(7), 1621: subscriber_that_dropped(0),
                                       1622: subscriber_that_dropped(5)}
        assert node.dropped_frames() == 12

    def test_the_count_outlasts_a_subject_no_longer_decoded(self):
        node = ScannerNode.__new__(ScannerNode)
        gone = subscriber_that_dropped(7)
        node.publishers_subscribers = {1620: gone, 1621: subscriber_that_dropped(5)}
        node._unsubscribe(1620)
        gone.close.assert_called_once_with()
        assert 1620 not in node.publishers_subscribers and node.dropped_frames() == 12

    def test_a_subscriber_without_statistics_counts_nothing(self):
        node = ScannerNode.__new__(ScannerNode)
        node.publishers_subscribers = {1620: SimpleNamespace()}
        assert node.dropped_frames() == 0


def scanner_on_socket(sock) -> ScannerNode:
    """A scanner whose pycyphal transport reads from ``sock``, as on SocketCAN."""
    node = ScannerNode.__new__(ScannerNode)
    node.publishers_subscribers = {}
    media = SimpleNamespace(_sock=sock)
    node._node = SimpleNamespace(presentation=SimpleNamespace(transport=SimpleNamespace(_maybe_media=media)))
    return node


def socket_that_dropped(drops: int):
    """A socket whose SO_MEMINFO says its receive queue let ``drops`` go."""
    meminfo = struct.pack("9I", 0, 212992, 0, 212992, 0, 0, 0, 0, drops)
    return SimpleNamespace(getsockopt=lambda level, option, size: meminfo[:size])


class TestKernelDroppedFrames:
    """Past what can be decoded, the kernel's queue for the scanner's socket overflows (F-042)."""

    def test_counted_with_the_frames_dropped_before_decoding(self):
        node = scanner_on_socket(socket_that_dropped(1000))
        node.publishers_subscribers = {1620: subscriber_that_dropped(7)}
        assert node.kernel_dropped_frames() == 1000
        assert node.dropped_frames() == 1007

    def test_nothing_without_a_socketcan_socket(self):
        node = ScannerNode.__new__(ScannerNode)
        node.publishers_subscribers = {}
        node._node = SimpleNamespace(presentation=SimpleNamespace(transport=SimpleNamespace(_maybe_media=object())))
        assert node.kernel_dropped_frames() == 0

    def test_nothing_when_the_socket_cannot_say(self):
        def refuse(*_):
            raise OSError(92, "Protocol not available")
        assert scanner_on_socket(SimpleNamespace(getsockopt=refuse)).kernel_dropped_frames() == 0

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SO_MEMINFO is Linux's")
    def test_a_real_socket_is_read(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            assert scanner_on_socket(sock).kernel_dropped_frames() == 0

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pycyphal's SocketCAN media is Linux's")
    def test_pycyphal_still_keeps_the_socket_where_it_is_read(self):
        # kernel_dropped_frames goes through two private attributes of
        # pycyphal's: a release that renames them must fail here, not count 0.
        from pycyphal.transport.can import CANTransport
        from pycyphal.transport.can.media.socketcan import SocketCANMedia
        assert "self._maybe_media" in inspect.getsource(CANTransport.__init__)
        assert "self._sock =" in inspect.getsource(SocketCANMedia.__init__)

    def test_no_node_is_called_offline_while_the_kernel_drops_frames(self):
        node = scanner_on_socket(socket_that_dropped(0))
        node.all_nodes = {n: NodeInfo(node_id=n) for n in (42, 43)}
        node.hold_offline_while_losing_frames()
        assert node.all_nodes[42].silence_unreliable_until == 0.0
        node._node.presentation.transport._maybe_media._sock = socket_that_dropped(500)
        node.hold_offline_while_losing_frames()
        held = node.all_nodes[42].silence_unreliable_until
        assert held >= time.monotonic() + NodeInfo.OFFLINE_THRESHOLD_S - 0.5
        node.hold_offline_while_losing_frames()  # no more drops: the hold runs out, not renewed
        assert node.all_nodes[43].silence_unreliable_until == held

    def test_a_warning_once_a_while_while_the_count_grows(self, caplog):
        node = scanner_on_socket(socket_that_dropped(0))
        node.report_kernel_drops()
        assert "kernel's receive queue" not in caplog.text
        node._node.presentation.transport._maybe_media._sock = socket_that_dropped(1234)
        node.report_kernel_drops()
        node._node.presentation.transport._maybe_media._sock = socket_that_dropped(2000)
        node.report_kernel_drops()
        warnings = [r.getMessage() for r in caplog.records if "kernel's receive queue" in r.getMessage()]
        assert warnings == ["1234 CAN frames lost in the kernel's receive queue: the bus carries more "
                            "than Cynitor decodes. Decode on demand handles such a bus."]
        node._kernel_drops_reported_at -= ScannerNode.KERNEL_DROPS_REPORT_S
        node.report_kernel_drops()
        assert "766 CAN frames lost" in caplog.text


class TestDecodingOnDemand:
    """Only the subjects asked for go through pycyphal; the others keep their type and are counted."""

    TYPES = SimpleNamespace(T_1_0=object(), U_1_0=object(), Record_1_1=object())

    @pytest.fixture
    def node(self):
        node = ScannerNode.__new__(ScannerNode)
        node.subject_attributes, node.subject_types, node.publishers_subscribers = {}, {}, {}
        node._node = MagicMock(id=10)
        node._node.make_subscriber.side_effect = lambda dtype, subject_id: MagicMock()
        node.message_queue = asyncio.Queue()
        node.dropped_events = 0
        node.identity_map = MagicMock(get_uid=MagicMock(return_value=None))
        node._get_node_unique_id_hex = lambda node_id: "ab" * 16 if node_id == 42 else None
        with patch("scanner_node.get_model", return_value=SimpleNamespace(attributes=[])), \
                patch("scanner_node.importlib.import_module", return_value=self.TYPES):
            node._subscribe(1620, self.TYPES.T_1_0, "ns.T_1_0")
            node._subscribe(1621, self.TYPES.U_1_0, "ns.U_1_0")
            node._subscribe(8184, self.TYPES.Record_1_1, "uavcan.diagnostic.Record_1_1")
            yield node

    def test_every_subject_is_decoded_unless_told_otherwise(self, node):
        assert set(node.publishers_subscribers) == {1620, 1621, 8184}

    def test_only_those_asked_for_and_the_diagnostics(self, node):
        other = node.publishers_subscribers[1621]
        node.set_decoding({1620})
        assert set(node.publishers_subscribers) == {1620, 8184}
        other.close.assert_called_once_with()
        assert node.subject_types[1621] == "ns.U_1_0"   # its type is kept: it can be decoded again at once

    def test_one_asked_for_later_is_listened_to_again(self, node):
        node.set_decoding(set())
        kept = node.publishers_subscribers[8184]
        node.set_decoding({1621})
        assert set(node.publishers_subscribers) == {1621, 8184}
        assert node.publishers_subscribers[8184] is kept   # not listened to twice
        node._node.make_subscriber.assert_called_with(self.TYPES.U_1_0, 1621)

    def test_a_subject_found_meanwhile_waits_to_be_asked_for(self, node):
        node.set_decoding({1620})
        node._subscribe(1700, self.TYPES.T_1_0, "ns.T_1_0")
        assert 1700 not in node.publishers_subscribers and node.subject_types[1700] == "ns.T_1_0"
        node.set_decoding({1700})
        assert set(node.publishers_subscribers) == {1700, 8184}

    def test_none_decodes_them_all_again(self, node):
        node.set_decoding(set())
        node.set_decoding(None)
        assert set(node.publishers_subscribers) == {1620, 1621, 8184}

    def test_a_subject_with_a_type_is_not_one_to_guess_the_type_of(self, node):
        node.set_decoding(set())   # typed, though not listened to
        node._sampling = set()
        with pytest.raises(RuntimeError, match="already decoded"):
            asyncio.run(node.sample_subject(1620, 0.1))

    async def test_the_rates_counted_are_reported_for_what_is_not_decoded(self, node):
        node.set_decoding({1620})
        await node.queue_rates({
            (1620, 42): (100.0, 1000.0),   # decoded: its own messages carry its rate
            (1621, 42): (250.04, 1000.5),
            (1621, 43): (50.0, 1000.25),
            (1900, 42): (5.0, 1000.0),     # no type known: counted all the same
            (7509, 42): (1.0, 1000.0),     # heartbeats are decoded whatever happens
            (1621, 10): (9.0, 1000.0),     # Cynitor's own node
        })
        events = [node.message_queue.get_nowait() for _ in range(node.message_queue.qsize())]
        assert [(e["subject_id"], e["publisher_node_id"], e["rate"], e["subject_rate"]) for e in events] == [
            (1621, 42, 250.0, 300.0), (1621, 43, 50.0, 300.0), (1900, 42, 5.0, 5.0)]
        first = events[0]
        assert first["undecoded"] is True and first["attributes"] == [] and first["payload_bytes"] is None
        assert first["message_type"] == "U_1_0" and first["timestamp_unix"] == 1000.5
        assert first["unique_id"] == "ab" * 16
        assert events[2]["message_type"] is None


class TestUniqueIdHex:
    def make(self, unique_id, answered=True):
        node = ScannerNode.__new__(ScannerNode)
        node.all_nodes = {42: SimpleNamespace(has_responded_to_getInfo=answered,
                                              unique_id=np.array(unique_id, dtype=np.uint8))}
        return node

    def test_two_hex_digits_a_byte(self):
        assert self.make(list(range(16)))._get_node_unique_id_hex(42) == "000102030405060708090a0b0c0d0e0f"
        assert self.make([0] * 15 + [0xA0])._get_node_unique_id_hex(42) == "0" * 30 + "a0"

    def test_none_for_a_node_with_no_unique_id(self):
        assert self.make([0] * 16)._get_node_unique_id_hex(42) is None   # all zeros: not set
        assert self.make([])._get_node_unique_id_hex(42) is None
        assert self.make(list(range(16)), answered=False)._get_node_unique_id_hex(42) is None
        assert self.make(list(range(16)))._get_node_unique_id_hex(7) is None  # no such node


class TestQueueDrops:
    @pytest.mark.asyncio
    async def test_full_message_queue_counts_dropped_events(self):
        node = make_rate_node()
        node.message_queue = asyncio.Queue(maxsize=2)
        node.dropped_events = 0
        node.identity_map = MagicMock(get_uid=MagicMock(return_value=None))
        node._get_node_unique_id_hex = lambda _nid: None
        for i in range(5):
            await node._queue_event(i, "T_1_0", [], publisher_node_id=1)
        assert node.dropped_events == 3
        assert [node.message_queue.get_nowait()["subject_id"] for _ in range(2)] == [3, 4]


def make_event_node():
    node = ScannerNode.__new__(ScannerNode)
    node._prev_uptime = {}
    node._uptime_before_drop = {}
    node._node_id_conflict_reported = {}
    node.user_subject_types, node._sampling = {}, set()
    node.events = []
    node._emit_node_event = lambda node_id, event_type, detail=None: node.events.append((event_type, detail))
    return node


def heartbeats(node, *uptimes, node_id=42):
    for uptime in uptimes:  # what heartbeat_callback does with each one
        node._check_uptime(node_id, uptime)
        node._prev_uptime[node_id] = uptime


class TestRestartOrNodeIdConflict:
    def test_restart_is_reported_once_the_count_goes_on(self):
        node = make_event_node()
        heartbeats(node, 1000, 0)
        assert node.events == []  # judged on the next heartbeat
        heartbeats(node, 1)
        assert node.events == [("restart_suspected", {"old_uptime": 1000, "new_uptime": 0})]

    def test_two_nodes_on_one_node_id_are_a_conflict_not_restarts(self):
        node = make_event_node()
        heartbeats(node, 1000, 50, 1001, 51, 1002, 52, 1003)
        assert [e[0] for e in node.events] == ["node_id_conflict"]  # reported once a minute
        assert node.events[0][1] == {"uptimes": [50, 1001]}

    def test_conflict_is_reported_again_after_a_while(self):
        node = make_event_node()
        heartbeats(node, 1000, 50, 1001)
        node._node_id_conflict_reported[42] -= ScannerNode.NODE_ID_CONFLICT_REPORT_S
        heartbeats(node, 51, 1002)
        assert [e[0] for e in node.events] == ["node_id_conflict", "node_id_conflict"]

    def test_early_uptime_drop_is_not_judged(self):
        node = make_event_node()
        heartbeats(node, 3, 0, 1)  # uptime too small to tell anything
        assert node.events == []


class TestSubjectTypes:
    @pytest.fixture
    def node(self):
        node = make_event_node()
        node.active_publishers = {}
        node.subject_types = {}
        node.subscribed = []

        def subscribe(subject_id, data_type_class, message_type):
            node.subscribed.append((subject_id, message_type))
            node.subject_types[subject_id] = message_type

        node._subscribe = subscribe
        module = types.SimpleNamespace(T_1_0=object(), T_1_1=object(), U_1_0=object(), T_2_0=object())
        with patch("scanner_node.importlib.import_module", return_value=module):
            yield node

    async def test_first_publisher_sets_the_type(self, node):
        await node.add_subscriptions(10, {1620: "ns.T_1_0"})
        assert node.subscribed == [(1620, "ns.T_1_0")] and node.events == []

    async def test_another_minor_version_is_no_conflict(self, node):
        await node.add_subscriptions(10, {1620: "ns.T_1_0"})
        await node.add_subscriptions(11, {1620: "ns.T_1_1"})
        assert node.subscribed == [(1620, "ns.T_1_0")] and node.events == []
        assert node.active_publishers[1620] == {10, 11}

    @pytest.mark.parametrize("other", ["ns.U_1_0", "ns.T_2_0"])
    async def test_another_type_or_major_version_is_reported(self, node, other):
        await node.add_subscriptions(10, {1620: "ns.T_1_0"})
        await node.add_subscriptions(11, {1620: other})
        assert node.subscribed == [(1620, "ns.T_1_0")]  # still decoded as the first
        assert node.events == [("type_conflict", {"subject_id": 1620, "type": other, "decoded_as": "ns.T_1_0"})]


class TestDiagnosticSubscriptionIsPermanent:
    def test_cleanup_keeps_it_but_drops_others(self):
        node = make_event_node()
        diagnostic, other = MagicMock(), MagicMock()
        node.publishers_subscribers = {8184: diagnostic, 1620: other}
        node.subject_types = {8184: "uavcan.diagnostic.Record_1_1", 1620: "ns.T_1_0"}
        node.active_publishers = {8184: {42}, 1620: {42}}
        node._snapshot_node_for_identity = lambda node_id: None
        node._prev_ports, node._prev_health, node._prev_mode = {}, {}, {}
        node.service_clients, node.service_metadata, node.node_service_types = {}, {}, {}
        node.cleanup_subscriptions(42)
        diagnostic.close.assert_not_called()
        other.close.assert_called_once()
        assert 8184 in node.publishers_subscribers and 1620 not in node.subject_types


class TestServiceCallFields:
    """Composite request fields: every sub-field given is set, by name."""

    @pytest.fixture
    def service(self):
        from pydsdl import CompositeType

        class Point:
            _MODEL_ = types.SimpleNamespace(fields=[types.SimpleNamespace(name="x"), types.SimpleNamespace(name="y")])

        class Request:
            _MODEL_ = types.SimpleNamespace(attributes=[
                types.SimpleNamespace(name="point", data_type=MagicMock(spec=CompositeType)),
            ])

        client = MagicMock()
        client.dtype = types.SimpleNamespace(Request=Request)
        client.call = AsyncMock(return_value=(object(), None))
        node = ScannerNode.__new__(ScannerNode)
        node.service_clients = {(42, 100): client}
        module = types.SimpleNamespace(Point_1_0=Point)
        # patch.object: from Python 3.11, patch("scanner_node.x") finds the module
        # through importlib.import_module, which the first patch replaces.
        with patch.object(scanner_node.importlib, "import_module", return_value=module), \
             patch.object(scanner_services, "to_builtin", return_value={"status": 0}):
            yield node, client

    async def call(self, node, value):
        return await node.make_service_call(42, 100, "Svc_1_0", {"point": {"type": "ns.Point.1.0", "value": value}})

    async def test_all_named_fields_are_set(self, service):
        node, client = service
        await self.call(node, {"x": 1, "y": 2})
        sent = client.call.await_args.args[0]
        assert (sent.point.x, sent.point.y) == (1, 2)

    async def test_single_value_sets_the_first_field(self, service):
        node, client = service
        await self.call(node, 5)
        sent = client.call.await_args.args[0]
        assert sent.point.x == 5 and not hasattr(sent.point, "y")

    async def test_unknown_field_is_refused(self, service):
        node, client = service
        with pytest.raises(ValueError, match="'z'"):
            await self.call(node, {"z": 1})
        client.call.assert_not_awaited()

    async def test_response_is_json(self, service):
        node, _ = service
        assert json.loads(await self.call(node, {"x": 1})) == {"status": 0}


class TestExecuteCommand:
    def test_callable_on_a_node_that_serves_it_without_registers(self):
        from node_info import NodeInfo
        node = ScannerNode.__new__(ScannerNode)
        info = NodeInfo(node_id=42)
        info.has_appeared = True
        info.server_ServiceIDs = [435]
        node.all_nodes = {42: info}
        node.node_service_types, node.service_metadata = {}, {}
        request = types.SimpleNamespace(_MODEL_=types.SimpleNamespace(attributes=[
            types.SimpleNamespace(name="command", data_type="saturated uint16"),
            types.SimpleNamespace(name="parameter", data_type="saturated uint8[<=255]"),
        ]))
        module = types.SimpleNamespace(ExecuteCommand_1_3=types.SimpleNamespace(Request=request))
        with patch("scanner_node.importlib.import_module", return_value=module):
            [service] = node.get_service_schema(42)
        assert service["callable"] and service["full_type"] == "uavcan.node.ExecuteCommand_1_3"
        assert [f["name"] for f in service["request_fields"]] == ["command", "parameter"]



class TestGetInfoNoAnswer:
    @pytest.fixture
    def node(self):
        node = ScannerNode.__new__(ScannerNode)
        client = MagicMock()
        client.call = AsyncMock(return_value=None)  # pycyphal: no answer in time
        node._node = MagicMock()
        node._node.make_client.return_value = client
        return node

    @pytest.mark.parametrize("fd", [True, False])
    async def test_says_so_and_hints_at_can_fd(self, node, caplog, fd):
        node.get_transport_info = lambda: {"protocol": {"is_fd": fd}}
        await node.getInfo(50)
        assert "Node 50 does not answer GetInfo, though its heartbeats arrive" in caplog.text
        assert ("Run it in CAN FD too" in caplog.text) == fd
        assert "NoneType" not in caplog.text


class TestOwnNodeIdTaken:
    """Cynitor never hears its own heartbeats: one from its node-ID comes from another node."""

    def make(self, own_id=42):
        node = make_event_node()
        node._node = types.SimpleNamespace(id=own_id)
        node.all_nodes = {n: NodeInfo(node_id=n) for n in (42, 43)}
        node._prev_health, node._prev_mode, node._prev_ports = {}, {}, {}
        node._schedule_info_refresh = MagicMock(return_value=True)
        node._queue_event = AsyncMock()
        node.said = []
        node.on_own_node_id_conflict = node.said.append
        return node

    async def heartbeat(self, node, source, uptime=10):
        msg = types.SimpleNamespace(uptime=uptime, health=types.SimpleNamespace(value=0),
                                    mode=types.SimpleNamespace(value=0), vendor_specific_status_code=0)
        await node.heartbeat_callback(msg, types.SimpleNamespace(source_node_id=source))

    async def test_is_reported_once_a_while(self, caplog):
        node = self.make()
        for uptime in (10, 11, 12):
            await self.heartbeat(node, 42, uptime)
        assert node.events.count(("node_id_conflict", {"own_node_id": True, "uptime": 10})) == 1
        assert len(node.said) == 1 and "Cynitor's own node-ID 42" in node.said[0]
        assert "Cynitor's own node-ID 42" in caplog.text

    async def test_other_node_ids_are_not_a_conflict(self):
        node = self.make()
        await self.heartbeat(node, 43)
        assert node.said == [] and "node_id_conflict" not in [e[0] for e in node.events]

    async def test_anonymous_scanner_has_no_node_id_to_share(self):
        node = self.make(own_id=None)
        await self.heartbeat(node, 42)
        assert node.said == []

    async def test_anonymous_heartbeat_and_port_list_are_ignored(self):
        # An anonymous node may send neither; one that does raised KeyError,
        # logged with a traceback for each frame.
        node = self.make()
        await self.heartbeat(node, None)
        await node.port_callback(MagicMock(), types.SimpleNamespace(source_node_id=None))
        assert node.events == [] and set(node.all_nodes) == {42, 43}
        node._queue_event.assert_not_awaited()


class TestLeavingSoftwareUpdate:
    async def test_new_firmware_is_asked_for_its_info_at_once(self):
        node = make_event_node()
        info = NodeInfo(node_id=42)
        info.mark_appeared(first_seen=datetime.datetime.now())
        node.all_nodes = {42: info}
        node._prev_health, node._prev_mode, node._prev_ports = {}, {}, {}
        node._info_due = lambda _node, _now: False
        node._schedule_info_refresh = MagicMock(return_value=True)
        node._queue_event = AsyncMock()

        async def heartbeat(mode):
            msg = types.SimpleNamespace(uptime=10, health=types.SimpleNamespace(value=0),
                                        mode=types.SimpleNamespace(value=mode), vendor_specific_status_code=0)
            await node.heartbeat_callback(msg, types.SimpleNamespace(source_node_id=42))

        for mode in (0, 3, 3):  # operational, then in its bootloader
            await heartbeat(mode)
        node._schedule_info_refresh.assert_not_called()
        await heartbeat(0)  # the new firmware runs
        node._schedule_info_refresh.assert_called_once_with(42, was_disappeared=False)


class TestArrayAttributes:
    def test_numbers_stay_numbers_and_bytes_are_text(self):
        import numpy as np
        node = ScannerNode.__new__(ScannerNode)
        results = []
        node._extract_value("meter_per_second", np.array([1.0, 2.0, 3.5], dtype=np.float32), results)
        node._extract_value("counts", np.array([1, 300], dtype=np.uint16), results)
        node._extract_value("text", np.frombuffer(b"hello", dtype=np.uint8), results)
        assert results == [{"attribute": "meter_per_second", "value": [1.0, 2.0, 3.5]},
                           {"attribute": "counts", "value": [1, 300]},
                           {"attribute": "text", "value": "hello"}]

    def test_array_of_composites_is_json_an_id_as_its_value(self):
        """port.List's SubjectID[] holds DSDL objects, which broke the event log and the dashboard's socket."""
        import numpy as np
        node = ScannerNode.__new__(ScannerNode)
        ids = np.array([types.SimpleNamespace(value=7509), types.SimpleNamespace(value=7510)], dtype=object)
        pairs = np.array([types.SimpleNamespace(a=1, b=2)], dtype=object)
        results = []
        with patch.object(scanner_node, "to_builtin", side_effect=vars):  # a DSDL object's fields, as a dict
            node._extract_value("publishers.sparse_list", ids, results)
            node._extract_value("pairs", pairs, results)
        assert results == [{"attribute": "publishers.sparse_list", "value": [7509, 7510]},
                           {"attribute": "pairs", "value": [{"a": 1, "b": 2}]}]
        json.dumps(results)

    def test_a_service_mask_is_the_ids_it_sets(self):
        """port.List's ServiceIDList is 512 bits: a card showed them as [false, false, …] (audit F-046)."""
        import numpy as np
        node = ScannerNode.__new__(ScannerNode)
        mask = np.zeros(512, dtype=bool)
        mask[[430, 435]] = True
        service_ids = types.SimpleNamespace(mask=mask)
        model = types.SimpleNamespace(attributes=[types.SimpleNamespace(name="mask")])
        results = []
        with patch.object(scanner_node, "get_model", return_value=model), \
             patch.object(scanner_node, "Field", types.SimpleNamespace):
            node._extract_value("servers", service_ids, results)
        assert results == [{"attribute": "servers", "value": [430, 435]}]

    def test_a_subject_mask_is_the_ids_it_sets(self):
        import numpy as np
        node = ScannerNode.__new__(ScannerNode)
        mask = np.zeros(8192, dtype=bool)
        mask[[7509, 7510]] = True
        results = []
        node._extract_value("publishers.mask", mask, results)
        node._extract_value("flags", np.array([True, False]), results)  # other bools stay as they are
        assert results == [{"attribute": "publishers.mask", "value": [7509, 7510]},
                           {"attribute": "flags", "value": [True, False]}]
