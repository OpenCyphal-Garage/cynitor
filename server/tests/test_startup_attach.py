"""Tests for startup attach behaviour.

Passing a CAN interface that does not exist used to be fatal: the server tore
itself down and exited, taking the dashboard with it. That is the wrong trade,
because the dashboard is exactly where a user would correct the mistake.
"""

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main


@pytest.fixture
def session():
    s = MagicMock()
    s.connect = AsyncMock()
    return s


class TestSuccessfulAttach:
    @pytest.mark.asyncio
    async def test_reports_success(self, session):
        assert await main.attach_or_fall_back(session, "vcan0") is True
        session.connect.assert_awaited_once_with("vcan0", force_compile=False)

    @pytest.mark.asyncio
    async def test_passes_force_compile_through(self, session):
        await main.attach_or_fall_back(session, "vcan0", force_compile=True)
        session.connect.assert_awaited_once_with("vcan0", force_compile=True)


class TestFailedAttach:
    @pytest.mark.asyncio
    async def test_does_not_raise(self, session):
        # The caller is the server's main loop: an exception here would end it.
        session.connect.side_effect = OSError(19, "No such device")
        with patch.object(main, "discover_can_interfaces", return_value=[]):
            assert await main.attach_or_fall_back(session, "can0") is False

    @pytest.mark.asyncio
    async def test_names_the_interface_and_the_cause(self, session, caplog):
        session.connect.side_effect = OSError(19, "No such device")
        with patch.object(main, "discover_can_interfaces", return_value=[]):
            await main.attach_or_fall_back(session, "can0")
        text = caplog.text
        assert "can0" in text
        assert "No such device" in text

    @pytest.mark.asyncio
    async def test_lists_what_is_actually_available(self, session, caplog):
        session.connect.side_effect = OSError(19, "No such device")
        with patch.object(main, "discover_can_interfaces", return_value=["vcan0", "slcan0"]):
            await main.attach_or_fall_back(session, "can0")
        assert "vcan0" in caplog.text
        assert "slcan0" in caplog.text

    @pytest.mark.asyncio
    async def test_suggests_creating_one_when_none_exist(self, session, caplog):
        session.connect.side_effect = OSError(19, "No such device")
        with patch.object(main, "discover_can_interfaces", return_value=[]):
            await main.attach_or_fall_back(session, "can0")
        assert "modprobe vcan" in caplog.text

    @pytest.mark.asyncio
    async def test_says_it_is_carrying_on(self, session, caplog):
        session.connect.side_effect = OSError(19, "No such device")
        with patch.object(main, "discover_can_interfaces", return_value=["vcan0"]):
            await main.attach_or_fall_back(session, "can0")
        assert "selection mode" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_survives_discovery_also_failing(self, session, caplog):
        # Nothing on this path may take the server down, including the
        # diagnostic lookup that only exists to produce a better message.
        session.connect.side_effect = OSError(19, "No such device")
        with patch.object(main, "discover_can_interfaces", side_effect=RuntimeError("boom")):
            assert await main.attach_or_fall_back(session, "can0") is False
        assert "selection mode" in caplog.text.lower()


class TestShowTokenOnTerminal:
    """The token is printed for a human to copy, and kept out of everything else.

    It is deliberately not logged: the log buffer is served through /api/logs
    and shown in the dashboard's log panel, and a service manager captures
    stdout into the system journal.
    """

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    def test_prints_when_a_terminal_is_attached(self):
        out = self._Tty()
        assert main.show_token_on_terminal("secret-value", out) is True
        assert "secret-value" in out.getvalue()

    def test_silent_when_output_is_captured(self):
        # Piped, redirected, or captured by a service manager.
        out = io.StringIO()
        assert main.show_token_on_terminal("secret-value", out) is False
        assert out.getvalue() == ""

    def test_silent_when_no_token_is_set(self):
        out = self._Tty()
        assert main.show_token_on_terminal("", out) is False
        assert out.getvalue() == ""
        assert main.show_token_on_terminal(None, out) is False

    def test_tells_the_reader_what_to_do_with_it(self):
        out = self._Tty()
        main.show_token_on_terminal("secret-value", out)
        assert "dashboard" in out.getvalue().lower()

    def test_never_reaches_the_log_buffer(self, caplog):
        out = self._Tty()
        main.show_token_on_terminal("secret-value", out)
        assert "secret-value" not in caplog.text
