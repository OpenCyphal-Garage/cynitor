"""Tests for BusLoadMonitor — subprocess management and utilization parsing."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from main import BusLoadMonitor, _get_can_bitrate


class TestGetCanBitrate:
    def test_default_when_command_fails(self):
        with patch("main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert _get_can_bitrate("vcan0") == 500000

    def test_parses_bitrate_from_output(self):
        with patch("main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="3: can0: <NOARP,UP,LOWER_UP> mtu 16 qdisc fq_codel state UP\n    link/can  promiscuity 0\n    can state ERROR-ACTIVE restart-ms 0\n          bitrate 1000000 sample-point 0.750",
            )
            assert _get_can_bitrate("can0") == 1000000

    def test_default_when_no_match(self):
        with patch("main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="no bitrate info here")
            assert _get_can_bitrate("can0") == 500000

    def test_default_on_exception(self):
        with patch("main.subprocess.run", side_effect=FileNotFoundError):
            assert _get_can_bitrate("can0") == 500000


class TestBusLoadMonitor:
    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        with patch("main._get_can_bitrate", return_value=500000):
            monitor = BusLoadMonitor("vcan0")

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
        with patch("main._get_can_bitrate", return_value=500000):
            monitor = BusLoadMonitor("vcan0")

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

    def test_is_alive_false_when_not_started(self):
        with patch("main._get_can_bitrate", return_value=500000):
            monitor = BusLoadMonitor("vcan0")
        assert not monitor.is_alive

    def test_is_alive_false_when_process_exited(self):
        with patch("main._get_can_bitrate", return_value=500000):
            monitor = BusLoadMonitor("vcan0")
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        monitor._proc = mock_proc
        assert not monitor.is_alive

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self):
        with patch("main._get_can_bitrate", return_value=500000):
            monitor = BusLoadMonitor("vcan0")
        await monitor.stop()
        assert monitor.utilization == 0.0
