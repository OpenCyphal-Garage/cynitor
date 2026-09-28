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
    frames python-can subscribes to.
    """

    def __init__(self, device: str, fd: bool, on_frame: Callable[[can.Message], None]) -> None:
        self._bus = can.Bus(interface="socketcan", channel=device, fd=fd)
        self._on_frame = on_frame
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"raw-log-{device}", daemon=True)
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
