"""Raw CAN frame logs in the candump .log format.

python-can's CanutilsLogWriter writes the file, so python-can, SavvyCAN and
can-utils (canplayer, log2asc) read it: classic and CAN FD frames, error
frames, and the direction of each frame.

Frames come from one of two taps, whichever the session uses: the CAN hub's
forwarding loops for adapters Cynitor opens itself, or a second, listen-only
socket on a SocketCAN interface (SocketcanTap). Neither changes what Cynitor
sends or receives.
"""

from __future__ import annotations

import datetime
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import json

import can

logger = logging.getLogger(__name__)

RAW_LOG_DIR = "raw"  # inside the data folder
_NAME = re.compile(r"^cynitor-\d{8}-\d{6}\.log$")


def new_log_name(now: Optional[datetime.datetime] = None) -> str:
    return (now or datetime.datetime.now()).strftime("cynitor-%Y%m%d-%H%M%S.log")


def log_path(folder: Path, name: str) -> Optional[Path]:
    """The file ``name`` names in ``folder``, or None if it is not a raw log name.

    Only names Cynitor gives its logs pass, so a request cannot reach any
    other file.
    """
    return folder / name if _NAME.match(name) else None


def list_logs(folder: Path) -> list[dict]:
    """Saved raw logs, newest first."""
    if not folder.is_dir():
        return []
    logs = []
    for path in folder.iterdir():
        if _NAME.match(path.name) and path.is_file():
            stat = path.stat()
            logs.append({"name": path.name, "bytes": stat.st_size, "modified_unix": stat.st_mtime})
    return sorted(logs, key=lambda log: log["name"], reverse=True)


class RawLog:
    """One log file being written. Frames may arrive from several threads."""

    def __init__(self, path: Path, channel: str) -> None:
        self.path = path
        self.frames = 0
        self.started_unix = time.time()
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        self._writer: Optional[can.CanutilsLogWriter] = can.CanutilsLogWriter(str(path), channel=channel)

    def write(self, msg: can.Message) -> None:
        # candump logs carry wall-clock time; some adapters stamp frames with
        # their own clock since power-up instead.
        if msg.timestamp < 1e9:
            msg.timestamp = time.time()
        with self._lock:
            if self._writer is None:
                return
            try:
                self._writer.on_message_received(msg)
                self.frames += 1
            except Exception as exc:  # e.g. disk full: stop writing, say why
                self.error = str(exc)
                logger.error("Raw log %s stopped: %s", self.path.name, exc)
                self._close_locked()

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._writer is not None:
            self._writer.stop()
            self._writer = None

    def status(self) -> dict:
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"name": self.path.name, "frames": self.frames, "bytes": size,
                "started_unix": self.started_unix, "error": self.error}


class SocketcanTap:
    """A second, listen-only socket on a SocketCAN interface, feeding ``on_frame``.

    It receives every frame on the bus, Cynitor's own among them (the kernel
    hands a socket's frames to the host's other sockets too), and the error
    frames python-can subscribes to. python-can marks a frame sent when the
    kernel says it was created on this host (MSG_DONTROUTE): Cynitor's own,
    and those of any other program here, such as every node on a vcan.
    """

    def __init__(self, device: str, fd: bool, on_frame: Callable[[can.Message], None],
                 can_filters: Optional[list] = None, name: str = "raw-log") -> None:
        # can_filters are applied by the kernel: frames they reject never reach Python.
        self._bus = can.Bus(interface="socketcan", channel=device, fd=fd, can_filters=can_filters)
        self._on_frame = on_frame
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"{name}-{device}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self._bus.recv(timeout=0.2)
            except Exception as exc:
                logger.error("Raw log tap stopped: %s", exc)
                return
            if msg is not None:
                self._on_frame(msg)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._bus.shutdown()


# ── Playback ──────────────────────────────────────────────────────────────
#
# A log holds frames only. Fixed-port subjects (heartbeat, port list,
# diagnostics, ...) decode from them alone, but other subjects' types come
# from registers, which a log cannot be asked for. So when a log stops, what
# the session knew about the bus is kept beside it in a sidecar file.

def sidecar_path(log: Path) -> Path:
    """cynitor-...-120000.log -> cynitor-...-120000.types.json"""
    return log.with_suffix(".types.json")


def write_sidecar(log: Path, info: dict) -> None:
    try:
        sidecar_path(log).write_text(json.dumps(info, indent=1), encoding="utf-8")
    except OSError as exc:
        logger.warning("Raw log %s: could not save what the session knew about the bus: %s", log.name, exc)


def read_sidecar(log: Path) -> dict:
    """The sidecar of ``log``, or an empty one (logs from before sidecars, or a lost file)."""
    try:
        return json.loads(sidecar_path(log).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def log_has_fd_frames(log: Path) -> bool:
    """Whether ``log`` holds CAN FD frames (candump writes them as ``id##...``)."""
    with log.open(encoding="utf-8", errors="replace") as f:
        return any("##" in line for line in f)


class LogPlayer(can.BusABC):
    """A python-can bus that plays a candump .log file as if it were the bus.

    Frames arrive at their logged pace divided by ``speed`` (0: as fast as
    they can be read). Frames sent by the node-ID ``skip_node_id`` (the Cynitor
    that made the log) are left out, and sending is a no-op: a log cannot be
    written to. At the end of the file recv raises CanOperationError, which
    the hub reports as the end of the session.

    It stays silent until play() is called, so that the session can first
    get everything listening: frames played before would reach no one.
    """

    def __init__(self, path: Path, speed: float = 1.0, skip_node_id: Optional[int] = None, **kwargs) -> None:
        super().__init__(channel=str(path), **kwargs)
        self._reader = iter(can.LogReader(str(path)))
        self._speed = speed
        self._skip_node_id = skip_node_id
        self._pending: Optional[can.Message] = None
        self._start: Optional[tuple[float, float]] = None  # (log time, monotonic time) of the first frame
        self._playing = threading.Event()

    def play(self) -> None:
        self._playing.set()

    def _next(self) -> Optional[can.Message]:
        for msg in self._reader:
            source = msg.arbitration_id & 0x7F if msg.is_extended_id else None
            if not msg.is_error_frame and source is not None and source == self._skip_node_id:
                continue
            msg.is_rx = True  # logged on this host or not, it now comes from the "bus"
            return msg
        return None

    def _recv_internal(self, timeout: Optional[float]):
        if not self._playing.wait(timeout):
            return None, False
        msg = self._pending or self._next()
        self._pending = None
        if msg is None:
            raise can.CanOperationError("end of the raw log")
        if self._speed > 0:
            if self._start is None:
                self._start = (msg.timestamp, time.monotonic())
            due = self._start[1] + (msg.timestamp - self._start[0]) / self._speed
            wait = due - time.monotonic()
            if wait > 0:
                if timeout is not None and wait > timeout:
                    time.sleep(timeout)
                    self._pending = msg
                    return None, False
                time.sleep(wait)
        return msg, False

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        pass
