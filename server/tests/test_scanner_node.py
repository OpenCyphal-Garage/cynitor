"""Tests for ScannerNode port-register resolution.

Publishers and services are advertised as "uavcan.<pub|srv>.<port_name>.id",
so the numeric port-ID comes from the register's natural16 value and the DSDL
type name from the sibling ".type" register.
"""

from unittest.mock import MagicMock

import pytest

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
    async def test_returns_none_when_node_does_not_answer(self):
        class SilentClient:
            async def call(self, _request):
                return None

        node = ScannerNode.__new__(ScannerNode)
        result = await node._read_register(SilentClient(), "uavcan.pub.x.id", 42)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_parsed_union_field(self):
        class Client:
            async def call(self, _request):
                return (MagicMock(), MagicMock())

        node = ScannerNode.__new__(ScannerNode)
        node._find_non_none_field = lambda _value: ("natural16", [1620])
        result = await node._read_register(Client(), "uavcan.pub.x.id", 42)
        assert result == ("natural16", [1620])
