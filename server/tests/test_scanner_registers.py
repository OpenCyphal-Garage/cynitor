"""Tests for the scanner's register reads and writes (scanner_registers.py): get_registers and set_register.

The port registers a node is registered by (update_reg_list) are tested in
test_scanner_node.py.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

import scanner_node
from scanner_registers import RegisterNotApplied
from scanner_node import ScannerNode  # uavcan.* is stubbed in conftest.py


def name_bytes(name: str):
    return np.frombuffer(name.encode("utf-8"), dtype=np.uint8)


@pytest.fixture
def uavcan_types(monkeypatch):
    """Requests and values the fakes can read: the DSDL types are mocks here (see conftest)."""
    register, array = scanner_node.uavcan.register, scanner_node.uavcan.primitive.array

    def made_as(kind):
        return lambda **fields: SimpleNamespace(kind=kind, **fields)

    monkeypatch.setattr(register.List_1_0, "Request", lambda index: SimpleNamespace(index=index))
    monkeypatch.setattr(register.Access_1_0, "Request", lambda name, value=None: SimpleNamespace(name=name, value=value))
    monkeypatch.setattr(register, "Name_1_0", lambda name: SimpleNamespace(name=name))
    monkeypatch.setattr(register, "Value_1_0", lambda **fields: SimpleNamespace(**fields))
    for kind in ("Bit", "Integer64", "Integer32", "Integer16", "Integer8", "Natural64", "Natural32", "Natural16",
                 "Natural8", "Real64", "Real32", "Real16"):
        monkeypatch.setattr(array, f"{kind}_1_0", made_as(kind))
    monkeypatch.setattr(scanner_node.uavcan.primitive, "String_1_0", made_as("String"))
    monkeypatch.setattr(scanner_node.uavcan.primitive, "Unstructured_1_0", made_as("Unstructured"))


class Node:
    """A remote node's registers, as its List and Access servers answer for them.

    ``registers``: {name: (type, value, mutable)}. ``silent``: what a request
    is not answered for, a list index or a register name, and how many times.
    """

    def __init__(self, registers, silent=None):
        self.registers = dict(registers)
        self.silent = dict(silent or {})
        self.written = []   # (name, the value sent) of each write
        self.list = MagicMock(call=AsyncMock(side_effect=self._list))
        self.access = MagicMock(call=AsyncMock(side_effect=self._access))

    def _unanswered(self, key) -> bool:
        if self.silent.get(key, 0) > 0:
            self.silent[key] -= 1
            return True
        return False

    async def _list(self, request):
        if self._unanswered(request.index):
            return None
        names = list(self.registers)
        name = names[request.index] if request.index < len(names) else ""
        return SimpleNamespace(name=SimpleNamespace(name=name_bytes(name))), None

    async def _access(self, request):
        name = request.name.name.decode("utf-8")
        if self._unanswered(name):
            return None
        kind, value, mutable = self.registers[name]
        if request.value is not None and getattr(request.value, "empty", None) is None:
            self.written.append((name, request.value))
            (kind, sent), = vars(request.value).items()
            value = (np.array(sent.value, dtype=getattr(self, "precision", None)) if kind != "string"
                     else sent.value.decode("utf-8"))
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(value=(kind, value), mutable=mutable), None


class KeepsItsValues(Node):
    """A node that answers a write with the value it had: Cyphal lets a node refuse one."""

    async def _access(self, request):
        kept = dict(self.registers)
        await super()._access(request)  # records the write
        self.registers = kept
        name = request.name.name.decode("utf-8")
        kind, value, mutable = kept[name]
        return SimpleNamespace(value=(kind, value), mutable=mutable), None


def scanner_for(node: Node) -> ScannerNode:
    scanner = ScannerNode.__new__(ScannerNode)
    scanner._node = MagicMock()
    scanner._node.make_client.side_effect = lambda dtype, node_id, port: (
        node.list if dtype is scanner_node.uavcan.register.List_1_0 else node.access)
    scanner._find_non_none_field = lambda value: value   # the fake answers with (type, value) as it is
    return scanner


class TestGetRegisters:
    async def test_every_register_with_its_value_type_and_access(self, uavcan_types):
        node = Node({
            "uavcan.node.id": ("natural16", np.array([42]), True),
            "limits": ("real32", np.array([1.5, 2.5]), False),
            "uavcan.node.description": ("string", "left wheel", True),
            "spare": ("empty", None, True),
        })
        assert await scanner_for(node).get_registers(7) == [
            {"register_name": "uavcan.node.id", "value": "42", "type": "natural16", "access": "read-write"},
            {"register_name": "limits", "value": "1.5, 2.5", "type": "real32", "access": "read-only"},
            {"register_name": "uavcan.node.description", "value": "left wheel", "type": "string", "access": "read-write"},
            {"register_name": "spare", "value": "N/A", "type": "empty", "access": "read-write"},
        ]
        node.list.close.assert_called_once_with()
        node.access.close.assert_called_once_with()

    async def test_bits_read_true_and_false_as_dsdl_writes_them(self, uavcan_types):
        # Python's True/False showed (audit F-046); writes take either spelling.
        node = Node({"one": ("bit", np.array([True]), True), "flags": ("bit", np.array([True, False, True]), True)})
        assert [r["value"] for r in await scanner_for(node).get_registers(7)] == ["true", "true, false, true"]

    async def test_a_name_asked_for_twice_in_vain_is_passed_over(self, uavcan_types):
        node = Node({"a": ("bit", np.array([True]), True), "b": ("bit", np.array([False]), True),
                     "c": ("bit", np.array([True]), True)}, silent={1: 2})
        names = [r["register_name"] for r in await scanner_for(node).get_registers(7)]
        assert names == ["a", "c"]

    async def test_a_name_that_comes_at_the_second_asking_is_kept(self, uavcan_types):
        node = Node({"a": ("bit", np.array([True]), True), "b": ("bit", np.array([False]), True)}, silent={1: 1})
        assert [r["register_name"] for r in await scanner_for(node).get_registers(7)] == ["a", "b"]

    async def test_a_register_not_read_in_two_tries_is_left_out(self, uavcan_types):
        node = Node({"a": ("bit", np.array([True]), True), "b": ("bit", np.array([False]), True),
                     "c": ("bit", np.array([True]), True)}, silent={"a": 1, "b": 2})
        assert [r["register_name"] for r in await scanner_for(node).get_registers(7)] == ["a", "c"]

    async def test_a_register_that_fails_to_read_does_not_stop_the_rest(self, uavcan_types):
        node = Node({"a": ("bit", ValueError("Malformed union"), True), "b": ("bit", np.array([False]), True)})
        assert [r["register_name"] for r in await scanner_for(node).get_registers(7)] == ["b"]

    async def test_no_more_are_asked_for_than_a_node_may_have(self, uavcan_types):
        node = Node({f"r{i}": ("bit", np.array([True]), True) for i in range(ScannerNode.MAX_REGISTERS + 5)})
        assert len(await scanner_for(node).get_registers(7)) == ScannerNode.MAX_REGISTERS

    async def test_a_node_with_no_registers_gives_an_empty_list(self, uavcan_types):
        assert await scanner_for(Node({})).get_registers(7) == []

    async def test_a_node_that_does_not_answer_is_given_up_on_at_once(self, uavcan_types):
        # A node that cannot hear Cynitor used to be asked for all 256 indexes,
        # two tries each: 8.5 minutes, then an empty list.
        node = Node({"a": ("bit", np.array([True]), True)}, silent={i: 2 for i in range(ScannerNode.MAX_REGISTERS)})
        with pytest.raises(TimeoutError, match="Node 7 does not answer register requests"):
            await scanner_for(node).get_registers(7)
        assert node.list.call.await_count == 4
        node.list.close.assert_called_once_with()

    async def test_the_list_ends_where_the_node_stops_answering(self, uavcan_types):
        node = Node({f"r{i}": ("bit", np.array([True]), True) for i in range(5)}, silent={2: 2, 3: 2})
        assert [r["register_name"] for r in await scanner_for(node).get_registers(7)] == ["r0", "r1"]
        assert node.list.call.await_count == 2 + 4


class TestSetRegister:
    async def write(self, node, name, value, reg_type):
        return await scanner_for(node).set_register(7, name, value, reg_type)

    async def test_the_value_goes_out_in_the_registers_type_and_what_the_node_holds_comes_back(self, uavcan_types):
        node = Node({"gain": ("natural16", np.array([1]), True)})
        assert await self.write(node, "gain", "1200", "natural16") == 1200
        (name, sent), = node.written
        assert name == "gain" and sent.natural16.kind == "Natural16" and sent.natural16.value == [1200]
        node.access.close.assert_called_once_with()

    @pytest.mark.parametrize("text, values", [("[1, 2 3]", [1, 2, 3]), ("  7 ", [7]), ("[1000000 4000000]", [1000000, 4000000])])
    async def test_an_array_is_written_as_listed(self, uavcan_types, text, values):
        node = Node({"rates": ("natural32", np.array([0, 0]), True)})
        updated = await self.write(node, "rates", text, "natural32")
        assert node.written[0][1].natural32.value == values
        assert updated == (values if len(values) > 1 else values[0])

    async def test_reals_strings_and_bits(self, uavcan_types):
        node = Node({"k": ("real32", np.array([0.0]), True), "name": ("string", "", True),
                     "flags": ("bit", np.array([False]), True)})
        await self.write(node, "k", "0.25", "real32")
        await self.write(node, "name", "left wheel", "string")
        await self.write(node, "flags", "true 0 False 1", "bit")
        sent = dict(node.written)
        assert sent["k"].real32.value == [0.25]
        assert sent["name"].string.value == b"left wheel"
        assert sent["flags"].bit.value == [True, False, False, True]

    async def test_bytes_are_written_from_hex(self, uavcan_types):
        # They went out wrapped in a type that does not exist, so no such register could be written.
        node = Node({"blob": ("unstructured", "", True)})
        answers = [(SimpleNamespace(value=("unstructured", ""), mutable=True), None),
                   (SimpleNamespace(value=("unstructured", "01ab"), mutable=True), None)]
        sent = []

        async def access(request):
            sent.append(request.value)
            return answers[len(sent) - 1]
        node.access.call = AsyncMock(side_effect=access)
        assert await self.write(node, "blob", "01ab", "unstructured") == "01ab"
        assert sent[1].unstructured.kind == "Unstructured" and sent[1].unstructured.value == bytearray(b"\x01\xab")

    async def test_bytes_that_are_no_hex_are_refused(self, uavcan_types):
        node = Node({"blob": ("unstructured", "", True)})
        with pytest.raises(ValueError, match="Invalid value 'xyz' for type 'unstructured'"):
            await self.write(node, "blob", "xyz", "unstructured")
        assert node.written == []

    async def test_a_read_only_register_is_refused(self, uavcan_types):
        node = Node({"serial": ("natural16", np.array([1]), False)})
        with pytest.raises(ValueError, match="Register 'serial' is read-only"):
            await self.write(node, "serial", "2", "natural16")
        assert node.written == []

    async def test_a_register_the_node_does_not_have_is_named(self, uavcan_types):
        # A node answers an unknown name with an empty value: that is no type mismatch.
        node = Node({"gian": ("empty", None, False)})
        with pytest.raises(ValueError, match="Node 7 has no register 'gian'"):
            await self.write(node, "gian", "5", "natural16")
        assert node.written == []

    async def test_another_type_is_refused_unless_both_are_numbers(self, uavcan_types):
        node = Node({"name": ("string", "x", True), "gain": ("integer32", np.array([1]), True)})
        with pytest.raises(ValueError, match="Type mismatch: expected 'string', got 'natural16'"):
            await self.write(node, "name", "5", "natural16")
        # Numbers for numbers: converted to the register's own type, which the
        # node takes; as given, the node may keep its value (F-044).
        await self.write(node, "gain", "5", "natural16")
        assert node.written[0][1].integer32.value == [5]

    async def test_a_value_the_node_does_not_take_is_said_not_applied(self, uavcan_types):
        node = KeepsItsValues({"gain": ("natural16", np.array([1234]), True)})
        with pytest.raises(RegisterNotApplied, match="Node 7 did not take the value: 'gain' holds 1234"):
            await self.write(node, "gain", "5", "natural16")

    @pytest.mark.parametrize("reg_type, text", [("real32", "0.1"), ("real16", "0.3"), ("bit", "1 0"),
                                                 ("natural8", "[1, 2]"),
                                                 ("real32", "[nan, 1, 2]"), ("real64", "nan")])
    async def test_a_value_read_back_in_the_registers_precision_was_taken(self, uavcan_types, reg_type, text):
        # 0.1 as real32 reads back 0.100000001: the same value, in the register's precision. A NaN
        # read back is the NaN written, though NaN equals nothing (Windows audit, FW-04).
        dtype = {"real32": np.float32, "real16": np.float16, "real64": np.float64, "bit": np.bool_,
                 "natural8": np.uint8}[reg_type]
        node = Node({"r": (reg_type, np.array([0], dtype=dtype), True)})
        node.precision = dtype
        await self.write(node, "r", text, reg_type)

    @pytest.mark.parametrize("reg_type, value, message", [
        ("natural16", "70000", r"Value 70000 out of range for natural16 \(0–65535\)"),
        ("integer8", "[1, -129]", r"Value -129 out of range for integer8 \(-128–127\)"),
        ("real32", "1e39", "Value 1e\\+39 out of range for real32"),
        ("natural8", "abc", "invalid literal for int"),
        ("bit", "maybe", "Invalid value 'maybe' for type 'bit'"),
    ])
    async def test_a_value_that_does_not_fit_is_refused_before_anything_is_sent(self, uavcan_types, reg_type, value,
                                                                              message):
        node = Node({"r": (reg_type, np.array([0]), True)})
        with pytest.raises(ValueError, match=message):
            await self.write(node, "r", value, reg_type)
        assert node.written == []

    async def test_a_type_there_is_no_writing_of_is_refused(self, uavcan_types):
        node = Node({"r": ("natural16", np.array([1]), True)})
        with pytest.raises(ValueError, match="Type mismatch: expected 'natural16', got 'empty'"):
            await self.write(node, "r", "1", "empty")

    async def test_a_node_that_does_not_answer_gives_nothing(self, uavcan_types):
        assert await self.write(Node({"gain": ("natural16", np.array([1]), True)}, silent={"gain": 1}),
                                "gain", "2", "natural16") is None      # the read
        node = Node({"gain": ("natural16", np.array([1]), True)})
        answers = [(SimpleNamespace(value=("natural16", np.array([1])), mutable=True), None), None]
        node.access.call = AsyncMock(side_effect=answers)                 # read answered, write not
        assert await self.write(node, "gain", "2", "natural16") is None
        node.access.close.assert_called_once_with()
