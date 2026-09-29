"""Subject types the user sets, and guessing them from payloads."""

import importlib
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

import main
import type_guess
from scanner_node import ScannerNode


class Service:
    Request = object


MODULE = types.SimpleNamespace(T_1_0=object(), U_1_0=object(), S_1_0=Service)


@pytest.fixture
def scanner():
    node = ScannerNode.__new__(ScannerNode)
    node.user_subject_types, node._sampling = {}, set()
    node._uptime_before_drop, node._prev_uptime = {}, {}
    node._emit_node_event = lambda *args, **kwargs: None
    node.active_publishers, node.subject_types, node.publishers_subscribers = {}, {}, {}

    def subscribe(subject_id, data_type_class, message_type):
        node.subject_types[subject_id] = message_type
        node.publishers_subscribers[subject_id] = MagicMock()

    node._subscribe = subscribe
    with patch("scanner_node.importlib.import_module", return_value=MODULE):
        yield node


class TestScannerSetType:
    def test_set_change_clear(self, scanner):
        scanner.set_subject_type(1620, "ns.T.1.0")  # DSDL spelling
        assert scanner.subject_types[1620] == "ns.T_1_0" and scanner.user_subject_types == {1620: "ns.T_1_0"}
        first = scanner.publishers_subscribers[1620]
        scanner.set_subject_type(1620, "ns.U_1_0")
        first.close.assert_called_once()
        assert scanner.subject_types[1620] == "ns.U_1_0"
        scanner.set_subject_type(1620, None)
        assert 1620 not in scanner.subject_types and 1620 not in scanner.publishers_subscribers
        assert scanner.user_subject_types == {}

    def test_refusals(self, scanner):
        with pytest.raises(ValueError, match="service type"):
            scanner.set_subject_type(1620, "ns.S_1_0")
        with patch.object(importlib, "import_module", side_effect=ImportError):  # not by name: 3.11+ resolves it with the patched import_module
            with pytest.raises(ValueError, match="not a compiled"):
                scanner.set_subject_type(1620, "other.T_1_0")
        scanner.subject_types[1621] = "ns.T_1_0"  # named by registers
        with pytest.raises(RuntimeError, match="registers"):
            scanner.set_subject_type(1621, "ns.U_1_0")
        scanner._sampling.add(1622)
        with pytest.raises(RuntimeError, match="listened"):
            scanner.set_subject_type(1622, "ns.T_1_0")

    async def test_registers_take_over(self, scanner):
        scanner.set_subject_type(1620, "ns.T_1_0")
        await scanner.add_subscriptions(10, {1620: "ns.U_1_0"})
        assert scanner.subject_types[1620] == "ns.U_1_0" and scanner.user_subject_types == {}

    def test_kept_when_publishers_go(self, scanner):
        scanner.set_subject_type(1620, "ns.T_1_0")
        scanner.active_publishers[1620] = {42}
        scanner._snapshot_node_for_identity = lambda node_id: None
        scanner._prev_ports, scanner._prev_health, scanner._prev_mode = {}, {}, {}
        scanner.service_clients, scanner.service_metadata, scanner.node_service_types = {}, {}, {}
        scanner.cleanup_subscriptions(42)
        assert scanner.subject_types[1620] == "ns.T_1_0"

    async def test_sampling_refused_for_a_decoded_subject(self, scanner):
        scanner.set_subject_type(1620, "ns.T_1_0")
        with pytest.raises(RuntimeError, match="already decoded"):
            await scanner.sample_subject(1620, 0.1)


class TestRanking:
    def test_implausible_values(self):
        assert type_guess.implausible_values({"a": 1.5, "b": [0.0, 20.0], "c": "x", "d": 7}) == 0
        assert type_guess.implausible_values({"a": float("nan"), "b": [1e-40, 3e20], "c": {"d": float("inf")}}) == 4

    @pytest.mark.parametrize("encoded, received, fits", [
        (12, 15, True),    # 12 + tail = 13 bytes: a 16-byte frame, 3 zeros
        (12, 12, False),   # no padding where a frame needs some is no CAN FD frame
        (4, 7, False),     # 4 + tail fits a 5-byte frame exactly: zeros are data
        (11, 11, True),    # 11 + tail = 12: no padding
        (60, 63, True),    # a 64-byte frame
        (100, 110, True),  # several frames
        (100, 170, False),
    ])
    def test_fd_padding(self, encoded, received, fits):
        assert type_guess.fd_padding_fits(encoded, received) is fits

    def test_json_safe(self):
        assert type_guess.json_safe({"a": [float("nan"), 1.0]}) == {"a": ["nan", 1.0]}

    def test_order_and_fit(self):
        decoded = {  # what each type makes of the payloads; None: does not fit
            "std.Plain": {"v": 300.0}, "std.Odd": {"v": 1e-40}, "own.Custom": {"v": 2.0},
            "std.Fixed": {"v": 1.0}, "std.Wrong": None,
        }
        candidates = [type_guess.Candidate(name, name, name.startswith("own."), name == "std.Fixed")
                      for name in decoded]
        with patch.object(type_guess, "exact_decode", lambda data_type, payload, fd: decoded[data_type]), \
                patch.object(sys.modules["pycyphal.dsdl"], "to_builtin", lambda d: d, create=True):
            result = type_guess.rank_types([b"\x00" * 4, b"\x01" * 4], candidates)
        assert result["matches"] == 4
        assert [c["type"] for c in result["candidates"]] == ["own.Custom", "std.Plain", "std.Fixed", "std.Odd"]
        assert result["candidates"][0] == {"type": "own.Custom", "custom": True, "preview": {"v": 2.0}}

    def test_decoder_log_is_quiet_meanwhile_and_restored(self):
        import logging
        log = logging.getLogger("nunavut_support")
        log.setLevel(logging.INFO)
        seen = []

        def decode(data_type, payload, fd):
            seen.append(log.level)
            return None
        with patch.object(type_guess, "exact_decode", decode), \
                patch.object(sys.modules["pycyphal.dsdl"], "to_builtin", lambda d: d, create=True):
            type_guess.rank_types([b"x"], [type_guess.Candidate("a", "a", False, False)])
        assert seen == [logging.WARNING] and log.level == logging.INFO


@pytest.fixture
def session(tmp_path):
    s = main.CANSession(data_dir=tmp_path)
    s.scanner = MagicMock()  # is_running
    return s


class TestSession:
    def test_set_and_clear_are_saved(self, session):
        session.set_subject_type(1620, "ns.T.1.0")
        session.scanner.set_subject_type.assert_called_with(1620, "ns.T.1.0")
        assert json.loads(session.subject_types_file.read_text()) == {"1620": "ns.T.1.0"}
        session.set_subject_type(1620, None)
        assert session.saved_subject_types() == {}

    def test_refused_types_are_not_saved(self, session):
        session.scanner.set_subject_type.side_effect = ValueError("no")
        with pytest.raises(ValueError):
            session.set_subject_type(1620, "ns.X.1.0")
        assert session.saved_subject_types() == {}

    def test_needs_a_connection(self, tmp_path):
        with pytest.raises(RuntimeError, match="not connected"):
            main.CANSession(data_dir=tmp_path).set_subject_type(1620, "ns.T.1.0")

    def test_saved_types_are_applied_and_bad_ones_skipped(self, session):
        session.subject_types_file.write_text(json.dumps({"1620": "ns.T.1.0", "1621": "ns.Gone.1.0"}))
        session.scanner.set_subject_type.side_effect = [None, ValueError("not compiled")]
        session._apply_saved_subject_types()
        assert session.scanner.set_subject_type.call_count == 2

    def test_unreadable_file_is_empty(self, session):
        session.subject_types_file.write_text("not json")
        assert session.saved_subject_types() == {}

    async def test_guess_without_traffic(self, session):
        session.scanner.sample_subject = AsyncMock(return_value=[])
        assert await session.guess_subject_type(1620, []) == {"samples": 0, "matches": 0, "candidates": []}

    async def test_guess_ranks_the_samples(self, session):
        session.scanner.sample_subject = AsyncMock(return_value=[b"\x01"])
        with patch.object(main, "load_candidates", return_value=["c"]) as load, \
                patch.object(main, "rank_types", return_value={"matches": 1, "candidates": ["x"]}) as rank:
            result = await session.guess_subject_type(1620, [{"full_name": "ns.T.1.0"}])
        load.assert_called_once_with([{"full_name": "ns.T.1.0"}])
        rank.assert_called_once_with([b"\x01"], ["c"], False)
        assert result == {"samples": 1, "matches": 1, "candidates": ["x"]}


@pytest.fixture
async def api(session):
    from websocket_server import WebSocketServer
    server = WebSocketServer(session=session, host="127.0.0.1", port=0,
                             dsdl_manager=MagicMock(message_types=MagicMock(return_value=[{"full_name": "a"}])))
    async with TestClient(TestServer(server.app)) as client:
        yield client, session


class TestApi:
    async def test_set_and_clear(self, api):
        client, session = api
        session.set_subject_type = MagicMock()
        resp = await client.put("/api/subjects/1620/type", json={"type": " ns.T.1.0 "})
        assert resp.status == 200 and (await resp.json())["type"] == "ns.T.1.0"
        session.set_subject_type.assert_called_with(1620, "ns.T.1.0")
        assert (await client.delete("/api/subjects/1620/type")).status == 200
        session.set_subject_type.assert_called_with(1620, None)

    @pytest.mark.parametrize("error, status", [(ValueError("x"), 400), (RuntimeError("x"), 409)])
    async def test_set_errors(self, api, error, status):
        client, session = api
        session.set_subject_type = MagicMock(side_effect=error)
        assert (await client.put("/api/subjects/1620/type", json={"type": "ns.T.1.0"})).status == status

    async def test_bad_requests(self, api):
        client, _ = api
        assert (await client.put("/api/subjects/1620/type", json={})).status == 400
        assert (await client.put("/api/subjects/9000/type", json={"type": "ns.T.1.0"})).status == 400

    async def test_guess(self, api):
        client, session = api
        session.guess_subject_type = AsyncMock(return_value={"samples": 2, "matches": 0, "candidates": []})
        resp = await client.get("/api/subjects/1620/type-guesses")
        assert resp.status == 200 and (await resp.json())["samples"] == 2
        session.guess_subject_type.assert_awaited_once_with(1620, [{"full_name": "a"}])
        session.guess_subject_type = AsyncMock(side_effect=RuntimeError("already decoded"))
        assert (await client.get("/api/subjects/1620/type-guesses")).status == 409


class TestNodesPayload:
    def test_says_who_set_each_type(self):
        from telemetry_manager import TelemetryManager
        telemetry = TelemetryManager.__new__(TelemetryManager)
        telemetry.scanner = types.SimpleNamespace(subject_types={1620: "ns.T_1_0", 1621: "ns.U_1_0"},
                                                  user_subject_types={1621: "ns.U_1_0"})
        assert telemetry._subject_types() == {"1620": {"type": "ns.T_1_0", "set_by": "registers"},
                                              "1621": {"type": "ns.U_1_0", "set_by": "user"}}
