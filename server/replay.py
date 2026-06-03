"""Replay engine for stored recordings.

ReplayManager reads `recording_events` rows from the event-logger SQLite
database and broadcasts them through its own subscriber queues — structurally
a sibling of TelemetryManager, on purpose: the WS handler can subscribe to
either, no per-event source check needed.

Invariant: only one ReplayManager runs at a time per backend session, and it
runs only while no CAN session is active (so live and replay never overlap on
the broadcast channel).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


_BATCH_SIZE = 500


class ReplayManager:
    """Streams a recording back through WS subscriber queues at controlled speed."""

    def __init__(self, db_path: Path | str, recording_id: int, speed: float = 1.0) -> None:
        self.db_path = Path(db_path)
        self.recording_id = int(recording_id)
        self._speed = max(0.1, min(50.0, float(speed)))

        self.subscribers: set[asyncio.Queue] = set()

        self._task: Optional[asyncio.Task] = None
        # paused event is SET when running; CLEARED when paused so awaiters block.
        self._paused = asyncio.Event()
        self._paused.set()
        # _stop set causes the run loop to terminate.
        self._stop = asyncio.Event()
        # _reanchor set tells the run loop to recompute the wall-clock anchor on
        # its next iteration (after seek or speed change).
        self._reanchor = asyncio.Event()

        # Boundaries of the recording, populated on start():
        self._first_event_unix: Optional[float] = None
        self._last_event_unix: Optional[float] = None
        self._total_events: int = 0

        # Current playback position (seconds since first event)
        self._position_s: float = 0.0
        self._events_emitted: int = 0

        # Wall-clock anchor for timing
        self._wall_anchor: float = 0.0      # monotonic time at last (re)anchor
        self._event_anchor_unix: float = 0.0  # event timestamp at last (re)anchor

        # End-of-recording flag (set when the engine has emitted everything)
        self._finished = asyncio.Event()
        # Optional callback fired once on auto-stop, so the WS layer can clear
        # session.replay without polling.
        self._on_finish: Optional[Any] = None

    # ------------------------------------------------------------------
    # Subscriber management (same shape as TelemetryManager)
    # ------------------------------------------------------------------

    def subscribe(self, max_queue: int = 100) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    async def _broadcast(self, event: dict[str, Any]) -> None:
        dead = []
        for queue in self.subscribers:
            try:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(event)
            except Exception as exc:
                logger.warning("Replay broadcast failed for a queue: %s", exc)
                dead.append(queue)
        for q in dead:
            self.subscribers.discard(q)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, start_offset_s: float = 0.0) -> dict[str, Any]:
        """Open the recording, validate it has events, and spawn the playback task."""
        if self._task is not None and not self._task.done():
            raise RuntimeError("Replay already running")

        first_unix, last_unix, total = await asyncio.to_thread(
            self._read_recording_bounds_sync,
        )
        if total == 0:
            raise ValueError(f"Recording {self.recording_id} has no replayable events")

        self._first_event_unix = first_unix
        self._last_event_unix = last_unix
        self._total_events = total
        self._events_emitted = 0
        self._position_s = max(0.0, min(start_offset_s, self.duration_s))

        self._stop.clear()
        self._finished.clear()
        self._paused.set()  # not paused
        self._anchor_now()

        self._task = asyncio.create_task(self._run())
        logger.info(
            "Replay started: recording_id=%d total_events=%d duration=%.1fs speed=%.2fx",
            self.recording_id, total, self.duration_s, self._speed,
        )
        return self.status()

    async def stop(self) -> None:
        if self._task is None:
            self._notify_session_end()
            return
        self._stop.set()
        self._paused.set()  # un-pause so the run loop can exit
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._notify_session_end()
            self.subscribers.clear()

    def _notify_session_end(self) -> None:
        """Push a sentinel event to every subscriber so consumer coroutines
        can exit cleanly. Their WS handlers will tear down and the frontend
        reconnects to pick up the post-replay state."""
        sentinel = {
            "type": "replay_ended",
            "recording_id": self.recording_id,
            "finished": self.is_finished,
        }
        for queue in list(self.subscribers):
            try:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(sentinel)
            except Exception:
                pass

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._anchor_now()
        self._paused.set()

    def seek(self, position_s: float) -> None:
        position_s = max(0.0, min(float(position_s), self.duration_s))
        self._position_s = position_s
        self._reanchor.set()

    def set_speed(self, speed: float) -> None:
        self._speed = max(0.1, min(50.0, float(speed)))
        self._reanchor.set()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def duration_s(self) -> float:
        if self._first_event_unix is None or self._last_event_unix is None:
            return 0.0
        return max(0.0, self._last_event_unix - self._first_event_unix)

    @property
    def is_paused(self) -> bool:
        return not self._paused.is_set()

    @property
    def is_finished(self) -> bool:
        return self._finished.is_set()

    def status(self) -> dict[str, Any]:
        return {
            "active": self._task is not None and not self._task.done(),
            "recording_id": self.recording_id,
            "position_s": round(self._position_s, 3),
            "duration_s": round(self.duration_s, 3),
            "speed": self._speed,
            "paused": self.is_paused,
            "events_emitted": self._events_emitted,
            "total_events": self._total_events,
            "finished": self.is_finished,
        }

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    def _anchor_now(self) -> None:
        self._wall_anchor = time.monotonic()
        if self._first_event_unix is not None:
            self._event_anchor_unix = self._first_event_unix + self._position_s
        else:
            self._event_anchor_unix = 0.0

    async def _run(self) -> None:
        try:
            await self._stream_events()
        except asyncio.CancelledError:
            logger.info("Replay task cancelled")
            raise
        except Exception as exc:
            logger.error("Replay task crashed: %s", exc, exc_info=True)
        finally:
            self._finished.set()
            self._notify_session_end()
            if self._on_finish is not None:
                try:
                    cb_result = self._on_finish()
                    if asyncio.iscoroutine(cb_result):
                        await cb_result
                except Exception as exc:
                    logger.warning("Replay finish callback failed: %s", exc)

    async def _stream_events(self) -> None:
        assert self._first_event_unix is not None
        # Track the last emitted timestamp so the next batch query knows where
        # to resume. We use timestamp_unix as the cursor, with the row id as a
        # tiebreaker for events that share a timestamp.
        cursor_unix = self._first_event_unix + self._position_s
        cursor_id = -1

        while not self._stop.is_set():
            # If paused, block until resumed (or stop)
            if not self._paused.is_set():
                stop_wait = asyncio.create_task(self._stop.wait())
                resume_wait = asyncio.create_task(self._paused.wait())
                done, pending = await asyncio.wait(
                    [stop_wait, resume_wait], return_when=asyncio.FIRST_COMPLETED,
                )
                for p in pending:
                    p.cancel()
                if self._stop.is_set():
                    break

            # Honor any pending seek / speed change before reading the next batch
            if self._reanchor.is_set():
                self._reanchor.clear()
                cursor_unix = self._first_event_unix + self._position_s
                cursor_id = -1
                self._anchor_now()

            batch = await asyncio.to_thread(
                self._read_batch_sync, cursor_unix, cursor_id,
            )
            if not batch:
                # No more events — we've played to the end.
                self._position_s = self.duration_s
                break

            for row in batch:
                if self._stop.is_set():
                    return
                if not self._paused.is_set():
                    # Paused mid-batch — break out so the outer loop blocks on resume
                    break
                if self._reanchor.is_set():
                    break  # outer loop will rebuild the cursor

                target_wall = (
                    self._wall_anchor
                    + (row["timestamp_unix"] - self._event_anchor_unix) / self._speed
                )
                delay = target_wall - time.monotonic()
                if delay > 0:
                    try:
                        # Wait, but yield early on stop/pause/reanchor so we don't
                        # sleep past a user action.
                        await asyncio.wait_for(self._stop.wait(), timeout=delay)
                        # If wait_for returned without TimeoutError, stop was set
                        return
                    except asyncio.TimeoutError:
                        pass

                event = _row_to_event(row)
                await self._broadcast(event)
                self._events_emitted += 1
                self._position_s = max(0.0, row["timestamp_unix"] - self._first_event_unix)
                cursor_unix = row["timestamp_unix"]
                cursor_id = row["id"]

    # ------------------------------------------------------------------
    # SQLite (sync, called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _read_recording_bounds_sync(self) -> tuple[Optional[float], Optional[float], int]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(timestamp_unix) AS lo, MAX(timestamp_unix) AS hi"
                " FROM recording_events WHERE recording_id = ? AND kind = 'subject'",
                (self.recording_id,),
            ).fetchone()
        count = int(row[0] or 0)
        return (row[1], row[2], count)

    def _read_batch_sync(self, after_unix: float, after_id: int) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, timestamp_unix, timestamp_iso, subject_or_service_id,"
                "       publisher_node_id, unique_id, message_type, rate, attributes_json"
                "  FROM recording_events"
                " WHERE recording_id = ? AND kind = 'subject'"
                "   AND (timestamp_unix > ? OR (timestamp_unix = ? AND id > ?))"
                " ORDER BY timestamp_unix, id"
                " LIMIT ?",
                (self.recording_id, after_unix, after_unix, after_id, _BATCH_SIZE),
            ).fetchall()
        return [dict(r) for r in rows]


def _row_to_event(row: dict[str, Any]) -> dict[str, Any]:
    """Translate a recording_events row into the live-event dict shape that
    the WebSocket handler and frontend cacheEvent expect.

    The `replay: true` field lets clients distinguish replayed events from
    live ones without having to track session state externally.
    """
    try:
        attrs = json.loads(row.get("attributes_json") or "[]")
    except Exception:
        attrs = []
    return {
        "subject_id": row.get("subject_or_service_id"),
        "timestamp": row.get("timestamp_iso") or _to_iso(row.get("timestamp_unix")),
        "timestamp_unix": row.get("timestamp_unix"),
        "rate": row.get("rate"),
        "message_type": row.get("message_type"),
        "publisher_node_id": row.get("publisher_node_id"),
        "unique_id": row.get("unique_id"),
        "payload_bytes": None,
        "attributes": attrs if isinstance(attrs, list) else [],
        "replay": True,
    }


def _to_iso(ts_unix: Optional[float]) -> Optional[str]:
    if ts_unix is None:
        return None
    return datetime.datetime.fromtimestamp(ts_unix, tz=datetime.timezone.utc).isoformat()
