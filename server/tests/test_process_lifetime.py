"""Tests for the backend's shutdown wiring.

The packaged app runs this backend as a PyInstaller single-file binary, which
means a bootloader parent with the interpreter as its child. The desktop shell
kills the bootloader with SIGKILL, so the interpreter has to notice its parent
is gone and exit, or it outlives the closing window still holding port 8080.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import main


class TestShutdownOnSigterm:
    def test_raises_keyboard_interrupt(self):
        # The entry point already handles KeyboardInterrupt and runs cleanup;
        # SIGTERM is routed into that path rather than killing outright.
        with pytest.raises(KeyboardInterrupt):
            main._shutdown_on_sigterm(signal.SIGTERM, None)


class TestExitWhenParentDies:
    def test_noop_off_linux(self):
        with patch.object(main, "IS_LINUX", False), \
             patch.object(main, "IS_WINDOWS", False), \
             patch("ctypes.CDLL") as cdll:
            main._exit_when_parent_dies()
        cdll.assert_not_called()

    def test_windows_watches_the_parent(self):
        with patch.object(main, "IS_LINUX", False), \
             patch.object(main, "IS_WINDOWS", True), \
             patch.object(main, "_exit_when_windows_parent_dies") as watch:
            main._exit_when_parent_dies()
        watch.assert_called_once()

    def test_requests_sigterm_on_parent_death(self):
        libc = MagicMock()
        libc.prctl.return_value = 0
        with patch.object(main, "IS_LINUX", True), \
             patch("ctypes.CDLL", return_value=libc), \
             patch("os.getppid", return_value=4321):
            main._exit_when_parent_dies()
        # PR_SET_PDEATHSIG is 1.
        libc.prctl.assert_called_once_with(1, signal.SIGTERM, 0, 0, 0)

    def test_exits_when_parent_died_during_startup(self):
        # The parent changing between the two reads means it exited before
        # prctl landed, so the signal will never arrive. Note the new parent
        # is not pid 1: orphans go to the nearest subreaper, so comparing
        # against the original ppid is what actually detects this.
        libc = MagicMock()
        libc.prctl.return_value = 0
        with patch.object(main, "IS_LINUX", True), \
             patch("ctypes.CDLL", return_value=libc), \
             patch("os.getppid", side_effect=[4321, 2990]):
            with pytest.raises(SystemExit):
                main._exit_when_parent_dies()

    def test_survives_prctl_failure(self):
        libc = MagicMock()
        libc.prctl.return_value = -1
        with patch.object(main, "IS_LINUX", True), \
             patch("ctypes.CDLL", return_value=libc), \
             patch("os.getppid", return_value=4321):
            # A kernel that refuses the call must not take the server down.
            main._exit_when_parent_dies()

    def test_survives_missing_libc(self):
        with patch.object(main, "IS_LINUX", True), \
             patch("ctypes.CDLL", side_effect=OSError("no libc")):
            main._exit_when_parent_dies()


class TestWindowsParentWatch:
    def test_only_in_frozen_builds(self, monkeypatch):
        # Run from a shell the parent could be anything; only the frozen
        # bootloader is known to be the process that started us.
        monkeypatch.delattr(sys, "frozen", raising=False)
        with patch("ctypes.WinDLL", create=True) as windll:
            main._exit_when_windows_parent_dies()
        windll.assert_not_called()

    def test_survives_an_unopenable_parent(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        kernel32 = MagicMock()
        kernel32.OpenProcess.return_value = 0
        with patch("ctypes.WinDLL", return_value=kernel32, create=True), \
             patch("ctypes.get_last_error", return_value=5, create=True), \
             patch("threading.Thread") as thread:
            main._exit_when_windows_parent_dies()
        thread.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows parent watch")
class TestWindowsParentDeathEndToEnd:
    def test_child_exits_when_parent_is_killed(self, tmp_path):
        """Kill a parent outright and confirm the "frozen" child follows it down.

        As with the PyInstaller bootloader: TerminateProcess on the parent
        cannot be forwarded, and the child used to keep running and holding
        its port.
        """
        server_dir = Path(__file__).resolve().parent.parent
        child = tmp_path / "child.py"
        child.write_text(
            "import signal, sys, time\n"
            f"sys.path.insert(0, {str(server_dir)!r})\n"
            "sys.frozen = True\n"
            "import main\n"
            "signal.signal(signal.SIGTERM, main._shutdown_on_sigterm)\n"
            "main._exit_when_parent_dies()\n"
            "print('armed', flush=True)\n"
            # Short sleeps, like the server's event loop: a signal simulated
            # by interrupt_main is handled between bytecodes.
            "for _ in range(600):\n"
            "    time.sleep(0.1)\n"
        )
        # A venv's python.exe is a launcher that runs the real interpreter as
        # its own child, which would put an extra process between parent and
        # child. Run the real one, with this interpreter's module path.
        python = getattr(sys, "_base_executable", sys.executable)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        parent = tmp_path / "parent.py"
        parent.write_text(
            "import subprocess, sys, time\n"
            f"c = subprocess.Popen([{python!r}, {str(child)!r}], stdout=subprocess.PIPE, text=True)\n"
            "c.stdout.readline()\n"
            "print(c.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen([python, str(parent)], stdout=subprocess.PIPE, text=True, env=env)
        child_pid = None
        try:
            child_pid = int(proc.stdout.readline().strip())
            proc.kill()  # TerminateProcess, like ending the bootloader
            proc.wait(timeout=10)
            _wait_for_exit(child_pid, timeout=15)
        finally:
            proc.kill()
            if child_pid is not None:
                _kill(child_pid)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux-only")
class TestParentDeathEndToEnd:
    def test_child_exits_when_parent_is_killed(self, tmp_path):
        """Kill a parent abruptly and confirm the child follows it down.

        Mirrors what the desktop shell does to the PyInstaller bootloader, and
        is the behaviour that keeps port 8080 from being stranded. The child
        reports readiness only after arming the signal, so the test cannot race
        the parent's death against startup.
        """
        server_dir = Path(__file__).resolve().parent.parent
        child = tmp_path / "child.py"
        child.write_text(
            "import sys, time\n"
            f"sys.path.insert(0, {str(server_dir)!r})\n"
            "import main\n"
            "main._exit_when_parent_dies()\n"
            "print('armed', flush=True)\n"
            "time.sleep(60)\n"
        )
        parent = tmp_path / "parent.py"
        parent.write_text(
            "import subprocess, sys, time\n"
            f"c = subprocess.Popen([sys.executable, {str(child)!r}], stdout=subprocess.PIPE, text=True)\n"
            "c.stdout.readline()\n"  # wait until the child has armed the signal
            "print(c.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen([sys.executable, str(parent)], stdout=subprocess.PIPE, text=True)
        child_pid = None
        try:
            child_pid = int(proc.stdout.readline().strip())
            proc.kill()  # SIGKILL: cannot be forwarded, exactly like the shell
            proc.wait(timeout=10)
            _wait_for_exit(child_pid, timeout=15)
        finally:
            proc.kill()
            if child_pid is not None:
                _kill(child_pid)


def _kill(pid):
    import os
    try:
        # SIGKILL does not exist on Windows, where any signal terminates.
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


def _alive(pid):
    if sys.platform == "win32":
        # os.kill(pid, 0) would terminate the process on Windows, not probe it.
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    import os
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for_exit(pid, timeout):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.1)
    raise AssertionError(f"server pid {pid} outlived its parent")
