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
import time
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


@pytest.fixture
def frozen(monkeypatch):
    """Run as the PyInstaller binary does: the only case the parent watch is armed in."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)


class TestExitWhenParentDies:
    def test_not_armed_from_source(self):
        # A source server left running with nohup or & died with its shell (F-017).
        with patch.object(main, "IS_LINUX", True), patch("ctypes.CDLL") as cdll:
            main._exit_when_parent_dies()
        cdll.assert_not_called()

    def test_noop_off_linux(self):
        with patch.object(main, "IS_LINUX", False), \
             patch.object(main, "IS_WINDOWS", False), \
             patch("ctypes.CDLL") as cdll:
            main._exit_when_parent_dies()
        cdll.assert_not_called()

    def test_windows_watches_the_parent(self, frozen):
        with patch.object(main, "IS_LINUX", False), \
             patch.object(main, "IS_WINDOWS", True), \
             patch.object(main, "_exit_when_windows_parent_dies") as watch:
            main._exit_when_parent_dies()
        watch.assert_called_once()

    def test_requests_sigterm_on_parent_death(self, frozen):
        libc = MagicMock()
        libc.prctl.return_value = 0
        with patch.object(main, "IS_LINUX", True), \
             patch("ctypes.CDLL", return_value=libc), \
             patch("os.getppid", return_value=4321):
            main._exit_when_parent_dies()
        # PR_SET_PDEATHSIG is 1.
        libc.prctl.assert_called_once_with(1, signal.SIGTERM, 0, 0, 0)

    def test_exits_when_parent_died_during_startup(self, frozen):
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


class TestConsoleClose:
    """Closing the console window shuts down in order (Windows audit: it used to end the process at once)."""

    @pytest.mark.parametrize("event", [2, 5, 6])  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
    def test_close_logoff_and_shutdown_run_the_orderly_shutdown(self, monkeypatch, event):
        done = main.threading.Event()
        done.set()
        monkeypatch.setattr(main, "_shutdown_complete", done)
        with patch("_thread.interrupt_main") as interrupt:
            assert main._on_console_event(event) is True
        interrupt.assert_called_once_with(signal.SIGTERM)

    @pytest.mark.parametrize("event", [0, 1])  # CTRL_C_EVENT, CTRL_BREAK_EVENT
    def test_ctrl_c_and_ctrl_break_are_left_to_python(self, event):
        with patch("_thread.interrupt_main") as interrupt:
            assert main._on_console_event(event) is False
        interrupt.assert_not_called()

    def test_waits_for_the_shutdown_no_longer_than_windows_allows(self, monkeypatch):
        monkeypatch.setattr(main, "_shutdown_complete", main.threading.Event())  # never set
        monkeypatch.setattr(main, "_CONSOLE_CLOSE_WAIT_S", 0.1)
        with patch("_thread.interrupt_main"):
            assert main._on_console_event(2) is True

    def test_noop_off_windows(self, monkeypatch):
        monkeypatch.setattr(main, "IS_WINDOWS", False)
        with patch("ctypes.WinDLL", create=True) as windll:
            main._shut_down_when_console_closes()
        windll.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows console")
class TestWindowsConsoleCloseEndToEnd:
    def test_closing_the_window_runs_the_cleanup(self, tmp_path):
        server_dir = Path(__file__).resolve().parent.parent
        out = tmp_path / "out.txt"
        child = tmp_path / "child.py"
        child.write_text(
            "import signal, sys, time\n"
            f"sys.path.insert(0, {str(server_dir)!r})\n"
            "import main\n"
            "signal.signal(signal.SIGTERM, main._shutdown_on_sigterm)\n"
            "main._shut_down_when_console_closes()\n"
            f"out = open({str(out)!r}, 'w', buffering=1)\n"
            "out.write('ready\\n')\n"
            "try:\n"
            "    for _ in range(600):\n"
            "        time.sleep(0.05)\n"
            "except KeyboardInterrupt:\n"
            "    time.sleep(0.5)  # a cleanup that takes a while, as the server's does\n"
            "    out.write('cleaned up\\n')\n"
            "    main._shutdown_complete.set()\n"
        )
        # The real interpreter, not a venv's launcher, which would sit in the console too.
        python = getattr(sys, "_base_executable", sys.executable)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        hidden = subprocess.STARTUPINFO()
        hidden.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # its own console, not shown
        proc = subprocess.Popen([python, str(child)], env=env, startupinfo=hidden,
                                creationflags=subprocess.CREATE_NEW_CONSOLE)
        try:
            deadline = time.monotonic() + 30
            while not (out.exists() and "ready" in out.read_text()) and time.monotonic() < deadline:
                time.sleep(0.1)
            # What closing the window does: WM_CLOSE to the console's window.
            closer = (
                "import ctypes, sys\n"
                "k = ctypes.WinDLL('kernel32')\n"
                "k.FreeConsole()\n"
                "k.AttachConsole(int(sys.argv[1]))\n"
                "k.GetConsoleWindow.restype = ctypes.c_void_p\n"
                "ctypes.WinDLL('user32').PostMessageW(ctypes.c_void_p(k.GetConsoleWindow()), 0x0010, 0, 0)\n"
            )
            subprocess.run([python, "-c", closer, str(proc.pid)], timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            proc.wait(timeout=15)
            assert "cleaned up" in out.read_text()
        finally:
            proc.kill()


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
            "sys.frozen = True  # as the PyInstaller binary, for which it is armed\n"
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


class TestFailedStart:
    def test_a_port_taken_exits_with_failure(self, tmp_path):
        # It exited 0, which systemd's Restart=on-failure takes for success (F-013).
        import socket
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            result = subprocess.run(
                [sys.executable, "main.py", "--port", str(port), "--data-dir", str(tmp_path), "--no-update-check"],
                cwd=Path(main.__file__).parent, capture_output=True, text=True, timeout=60)
        assert result.returncode == 1, result.stderr[-500:]

    def test_a_network_bind_without_a_token_is_refused(self, tmp_path):
        # It started open to the network with one warning, and none for :: or a LAN address (F-031).
        env = {k: v for k, v in os.environ.items() if k != "CYNITOR_AUTH_TOKEN"}
        result = subprocess.run(
            [sys.executable, "main.py", "--bind", "::", "--port", "0", "--data-dir", str(tmp_path)],
            cwd=Path(main.__file__).parent, capture_output=True, text=True, timeout=60, env=env)
        assert result.returncode == 2 and "--insecure-no-auth" in result.stderr


class TestIsLoopback:
    @pytest.mark.parametrize("bind, loopback", [
        ("127.0.0.1", True), ("127.0.0.2", True), ("::1", True), ("[::1]", True), ("localhost", True),
        ("0.0.0.0", False), ("::", False), ("192.168.1.20", False), ("cynitor.lan", False),
    ])
    def test_binds(self, bind, loopback):
        assert main.is_loopback(bind) is loopback
