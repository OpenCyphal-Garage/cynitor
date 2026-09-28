"""Tests for BusLoadMonitor — subprocess management and utilization parsing."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from main import BusLoadMonitor, _get_can_bitrates


class TestGetCanBitrates:
    def test_default_when_command_fails(self):
        with patch("main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _get_can_bitrates("vcan0") == (500000, None)

    def test_parses_bitrate_from_output(self):
        with patch("main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="3: can0: <NOARP,UP,LOWER_UP> mtu 16 qdisc fq_codel state UP\n    link/can  promiscuity 0\n    can state ERROR-ACTIVE restart-ms 0\n          bitrate 1000000 sample-point 0.750",
            )
            assert _get_can_bitrates("can0") == (1000000, None)

    def test_parses_can_fd_data_bitrate(self):
        with patch("main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="3: can0: <NOARP,UP,LOWER_UP> mtu 72 qdisc pfifo_fast state UP\n    link/can  promiscuity 0\n    can <FD> state ERROR-ACTIVE restart-ms 0\n          bitrate 500000 sample-point 0.875\n          dbitrate 2000000 dsample-point 0.750",
            )
            assert _get_can_bitrates("can0") == (500000, 2000000)

    def test_default_when_no_match(self):
        with patch("main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="no bitrate info here")
            assert _get_can_bitrates("can0") == (500000, None)

    def test_default_on_exception(self):
        with patch("main.subprocess.run", side_effect=FileNotFoundError):
            assert _get_can_bitrates("can0") == (500000, None)


def make_monitor(iface: str = "vcan0", canbusload: str | None = "/usr/bin/canbusload",
                 bitrates: tuple = (500000, None)):
    """Build a BusLoadMonitor with the canbusload probe pinned.

    The monitor decides once, at construction, whether it is permanently
    disabled by looking for 'canbusload' (from Linux can-utils) on PATH. Left
    unpatched, these tests pass only on machines that happen to have can-utils
    installed and invert on every machine that does not, CI runners included.
    Pinning the probe keeps them about the monitor's logic, not about the host.

    Pass canbusload=None to construct the disabled no-op variant.
    """
    with patch("main._get_can_bitrates", return_value=bitrates), \
         patch("main.shutil.which", return_value=canbusload):
        return BusLoadMonitor(iface)


class TestBusLoadMonitor:
    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        monitor = make_monitor()

        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=[b"", b""])
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("main.asyncio.create_subprocess_exec", return_value=mock_proc):
            await monitor.start()
            assert monitor._proc is not None
            assert monitor.is_alive

        await monitor.stop()
        assert monitor._proc is None
        assert monitor.utilization == 0.0

    @pytest.mark.asyncio
    async def test_read_loop_parses_utilization(self):
        monitor = make_monitor()

        lines = [b"vcan0@500000  42%\n", b"vcan0@500000  73%\n", b""]
        line_iter = iter(lines)

        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=lambda: next(line_iter))
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("main.asyncio.create_subprocess_exec", return_value=mock_proc):
            await monitor.start()
            await asyncio.sleep(0.1)

        assert monitor.utilization == 73.0
        await monitor.stop()

    @pytest.mark.asyncio
    async def test_can_fd_data_bitrate_is_passed_to_canbusload(self):
        monitor = make_monitor("can0", bitrates=(500000, 2000000))
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.stdout.readline = AsyncMock(return_value=b"")
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock()
        with patch("main.asyncio.create_subprocess_exec", return_value=mock_proc) as spawn:
            await monitor.start()
        assert spawn.call_args.args[:2] == ("canbusload", "can0@500000,2000000")
        await monitor.stop()

    def test_is_alive_false_when_not_started(self):
        monitor = make_monitor()
        assert not monitor.is_alive

    def test_is_alive_false_when_process_exited(self):
        monitor = make_monitor()
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        monitor._proc = mock_proc
        assert not monitor.is_alive

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self):
        monitor = make_monitor()
        await monitor.stop()
        assert monitor.utilization == 0.0


class TestBusLoadMonitorWithoutCanUtils:
    """Behaviour on hosts with no 'canbusload' binary, such as CI runners."""

    def test_disabled_when_canbusload_missing(self):
        monitor = make_monitor(canbusload=None)
        assert monitor._disabled

    def test_reports_alive_when_disabled(self):
        # Deliberate: the health watchdog in _register_loop treats a dead
        # monitor as a reason to drop the CAN session, so the permanently
        # disabled no-op must not look dead or it would force false
        # disconnects on every machine without can-utils.
        monitor = make_monitor(canbusload=None)
        assert monitor.is_alive

    @pytest.mark.asyncio
    async def test_start_does_not_spawn_a_process(self):
        monitor = make_monitor(canbusload=None)
        with patch("main.asyncio.create_subprocess_exec") as spawn:
            await monitor.start()
        spawn.assert_not_called()
        assert monitor._proc is None
        assert monitor.utilization == 0.0
