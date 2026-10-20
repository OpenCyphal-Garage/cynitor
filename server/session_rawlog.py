"""Raw CAN logs while a session runs: every frame on the bus to a candump .log file (see raw_log).

Held by the session (main.CANSession) as ``raw_logs``. Behind the CAN hub the
frames come from its forwarding loops; on SocketCAN from a second,
listen-only socket. Stopping one writes its sidecar, what the session knew
of the bus, for playing the log back (CANSession.play_raw_log).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from can_config import socketcan_device
from raw_log import RAW_LOG_DIR, RawLog, SocketcanTap, free_space_problem, new_log_name, write_sidecar

if TYPE_CHECKING:
    from main import CANSession

logger = logging.getLogger(__name__)


class RawLogging:
    """The raw log of a session: none, or the one being written (``active``).

    start and stop touch the data folder's disk, which may be slow or busy, so
    the API calls them in worker threads (api_rawlogs); they take a lock of
    their own, as two requests may come at once.
    """

    def __init__(self, session: CANSession) -> None:
        self._session = session
        self.active: Optional[RawLog] = None
        self._tap: Optional[SocketcanTap] = None
        self._lock = threading.Lock()

    @property
    def folder(self) -> Path:
        return self._session.data_dir / RAW_LOG_DIR

    def start(self) -> RawLog:
        """Start logging every frame on the bus to a new candump .log file.

        Behind the hub the frames come from its forwarding loops; on SocketCAN
        from a second, listen-only socket. RuntimeError if CAN is not connected
        or a log is already running.
        """
        with self._lock:
            return self._start_locked()

    def _start_locked(self) -> RawLog:
        session = self._session
        if not session.is_running:
            raise RuntimeError("CAN is not connected")
        if self.active is not None:
            raise RuntimeError("A raw log is already running")
        self.folder.mkdir(parents=True, exist_ok=True)
        problem = free_space_problem(self.folder)
        if problem is not None:
            raise RuntimeError(problem)
        path = self.folder / new_log_name()
        if session.hub is not None:
            log = RawLog(path, channel="can0")
            session.hub.add_listener(log.write)
        else:
            device = socketcan_device(session.can_interface)
            log = RawLog(path, channel=device)
            try:
                self._tap = SocketcanTap(device, session.can_fd, log.write)
            except Exception:
                log.close()
                path.unlink(missing_ok=True)
                raise
        self.active = log
        logger.info("Raw CAN log started: %s", path)
        return log

    def stop(self) -> Optional[RawLog]:
        """Stop the raw log, if one runs, and return it."""
        with self._lock:
            return self._stop_locked()

    def _stop_locked(self) -> Optional[RawLog]:
        session = self._session
        log, self.active = self.active, None
        if log is None:
            return None
        if session.hub is not None:
            session.hub.remove_listener(log.write)
        if self._tap is not None:
            self._tap.stop()
            self._tap = None
        log.close()
        if session.scanner is not None:
            write_sidecar(log.path, self._bus_knowledge())
        logger.info("Raw CAN log stopped: %s (%d frames)", log.path, log.frames)
        return log

    def _bus_knowledge(self) -> dict:
        """What this session knows about the bus that a raw log cannot hold, for playing it."""
        session = self._session
        scanner = session._live_scanner
        publishers: dict[str, dict[str, str]] = {}
        for subject_id, node_ids in scanner.active_publishers.items():
            subject_type = scanner.subject_types.get(subject_id)
            if not subject_type:
                continue
            for node_id in node_ids:
                publishers.setdefault(str(node_id), {})[str(subject_id)] = subject_type
        names = {}
        for node_id, node in scanner.all_nodes.items():
            info = getattr(node, "info_response", None)
            if info is not None:
                names[str(node_id)] = info.name.tobytes().decode("utf-8", errors="replace")
        own = session.node_id
        return {
            "version": 1,
            "own_node_id": own,  # None listening only
            "bitrate": session.can_bitrate,
            "data_bitrate": session.can_data_bitrate,
            "fd": session.can_fd,
            "publishers": publishers,
            "servers": {str(n): {str(s): t for s, t in types.items()}
                        for n, types in scanner.node_service_types.items()},
            "names": names,
        }
