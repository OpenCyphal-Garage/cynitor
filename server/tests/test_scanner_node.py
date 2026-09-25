"""Tests for ScannerNode port-register resolution.

Publishers and services are advertised as "uavcan.<pub|srv>.<port_name>.id",
so the numeric port-ID comes from the register's natural16 value and the DSDL
type name from the sibling ".type" register.
"""

import asyncio
import datetime
import json
import time
import types
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from node_info import NodeInfo
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
    @pytest.mark.asyncio
    async def test_unanswered_list_request_fails_instead_of_truncating(self):
        """No answer to register.List must not read as "end of list"."""
        list_client = MagicMock()
        list_client.call = AsyncMock(return_value=None)
        node = ScannerNode.__new__(ScannerNode)
        node._node = MagicMock()
        node._node.make_client.return_value = list_client
        with patch("scanner_node.asyncio.sleep", AsyncMock()):
            with pytest.raises(TimeoutError):
                await node.update_reg_list(42)
        assert list_client.call.await_count == 2  # one retry before giving up


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
        with patch("scanner_node.importlib.import_module", return_value=module), \
             patch("scanner_node.to_builtin", return_value={"status": 0}):
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
