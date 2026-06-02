#!/usr/bin/env python3

import asyncio
import datetime
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, Any
import json

logger = logging.getLogger(__name__)


class FilterMatcher:
    """In-memory matcher for a recording's filter spec.

    Same OR-across-dimensions semantics as _filter_clause but applied to a
    live event dict. Used by EventLogger when routing telemetry to active
    recordings, and at quick-save creation time when copying matching events
    from the global buffer.
    """

    __slots__ = ("subject_ids", "node_ids", "service_ids", "message_types", "is_empty")

    def __init__(self, filt: Optional[dict[str, Any]] = None) -> None:
        filt = filt or {}
        self.subject_ids = frozenset(filt.get("subject_ids") or ())
        self.node_ids = frozenset(filt.get("node_ids") or ())
        self.service_ids = frozenset(filt.get("service_ids") or ())
        self.message_types = frozenset(filt.get("message_types") or ())
        self.is_empty = not (
            self.subject_ids or self.node_ids or self.service_ids or self.message_types
        )

    def matches_subject(self, event: dict) -> bool:
        if self.is_empty:
            return True
        if event.get("subject_id") in self.subject_ids:
            return True
        if event.get("publisher_node_id") in self.node_ids:
            return True
        if event.get("message_type") in self.message_types:
            return True
        return False

    def matches_service(self, ev: dict) -> bool:
        if self.is_empty:
            return True
        if ev.get("service_id") in self.service_ids:
            return True
        if ev.get("node_id") in self.node_ids:
            return True
        return False


class EventLogger:
    """
    Optional SQLite-based event logging for persistence and replay.
    
    Stores events in a local database for historical analysis and replay.
    Can be enabled or disabled independently of live streaming.
    """
    
    def __init__(
        self,
        db_path: str = "telemetry.db",
        retention_seconds: float = 86400.0,
        max_events: int = 5_000_000,
    ) -> None:
        """
        Initialize the event logger.

        Args:
            db_path: Path to SQLite database file (default: telemetry.db).
            retention_seconds: Keep events from the last N seconds (default 24h).
                Primary retention policy. 0 = unlimited.
            max_events: Hard safety cap on event count regardless of time.
                Only triggers if rate × retention exceeds this. 0 = unlimited.
        """
        self.db_path = Path(db_path)
        self.retention_seconds = float(retention_seconds)
        self.max_events = max_events
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._autostop_task: Optional[asyncio.Task] = None
        self._write_count = 0
        self._write_lock = threading.Lock()
        self._db_initialized = False
        # Registry of live 'dedicated' recordings. Read/written from both the
        # worker thread (during _write_events_sync) and async code (create/
        # stop/delete) — protect with the same threading.Lock.
        self._active_recordings: dict[int, dict[str, Any]] = {}
        self._active_lock = threading.Lock()
    
    def _init_db(self) -> None:
        """Initialize database schema."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Create events table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        subject_id INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        timestamp_unix REAL NOT NULL,
                        rate INTEGER,
                        message_type TEXT NOT NULL,
                        publisher_node_id INTEGER,
                        unique_id TEXT,
                        attributes TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Node lifecycle history table (30-day retention)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS node_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        node_id INTEGER NOT NULL,
                        unique_id TEXT,
                        timestamp_unix REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        detail TEXT
                    )
                """)

                # Migrate existing tables: add unique_id column if missing
                # (must run before index creation on unique_id)
                for table in ("events", "node_history"):
                    cols = [row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()]
                    if "unique_id" not in cols:
                        cursor.execute(f"ALTER TABLE {table} ADD COLUMN unique_id TEXT")
                        logger.info(f"Migrated {table}: added unique_id column")

                # Identity map persistence table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS identity_map (
                        unique_id TEXT PRIMARY KEY,
                        current_node_id INTEGER,
                        last_seen_unix REAL,
                        node_name TEXT,
                        previous_node_ids TEXT
                    )
                """)

                # Node data persistence (unique_id as primary key)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS node_data (
                        unique_id TEXT PRIMARY KEY,
                        node_name TEXT,
                        software_version_major INTEGER,
                        software_version_minor INTEGER,
                        publishers TEXT,
                        subscribers TEXT,
                        servers TEXT,
                        clients TEXT,
                        unique_id_bytes TEXT,
                        last_uptime REAL,
                        last_seen TEXT,
                        updated_at REAL NOT NULL
                    )
                """)

                # Recordings: named captures. Two storage modes (events_source):
                #   'global'    — Phase 1 bookmarks; exports read from the
                #                 global events table within the recording's
                #                 time-range × filter.
                #   'dedicated' — live recordings stream into recording_events
                #                 from start, surviving global retention.
                #                 Quick-save copies matching events at create
                #                 time into the same table.
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS recordings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        start_unix REAL NOT NULL,
                        end_unix REAL,
                        filter_json TEXT,
                        notes TEXT,
                        created_at REAL NOT NULL,
                        max_length_seconds REAL,
                        max_events INTEGER,
                        stop_on_limit INTEGER DEFAULT 0,
                        auto_stopped INTEGER DEFAULT 0,
                        event_count INTEGER DEFAULT 0,
                        events_source TEXT DEFAULT 'global'
                    )
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_rec_start ON recordings(start_unix)")

                # Migrate existing Phase 1 recordings: add new columns.
                rec_cols = {row[1] for row in cursor.execute("PRAGMA table_info(recordings)").fetchall()}
                for col, ddl in (
                    ("max_length_seconds", "REAL"),
                    ("max_events",         "INTEGER"),
                    ("stop_on_limit",      "INTEGER DEFAULT 0"),
                    ("auto_stopped",       "INTEGER DEFAULT 0"),
                    ("event_count",        "INTEGER DEFAULT 0"),
                    ("events_source",      "TEXT DEFAULT 'global'"),
                ):
                    if col not in rec_cols:
                        cursor.execute(f"ALTER TABLE recordings ADD COLUMN {col} {ddl}")
                        logger.info(f"Migrated recordings: added {col} column")

                # Per-recording event store. Used when recordings.events_source =
                # 'dedicated'. Survives global events-table retention.
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS recording_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recording_id INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        timestamp_unix REAL NOT NULL,
                        timestamp_iso TEXT,
                        subject_or_service_id INTEGER,
                        publisher_node_id INTEGER,
                        unique_id TEXT,
                        message_type TEXT,
                        rate INTEGER,
                        attributes_json TEXT
                    )
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_re_rec_ts ON recording_events(recording_id, timestamp_unix)")

                # Create indexes (after migration so unique_id column exists)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_subject ON events(subject_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_node ON events(publisher_node_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp_unix)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_unique_id ON events(unique_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_nh_node ON node_history(node_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_nh_uid ON node_history(unique_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_nh_timestamp ON node_history(timestamp_unix)")

                conn.execute("PRAGMA journal_mode=WAL")
            self._db_initialized = True
            logger.info(f"Database initialized at {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}", exc_info=True)
    
    def init_db_sync(self) -> None:
        """Initialize database synchronously. Use for tests or non-async contexts."""
        self._init_db()

    async def start(self) -> None:
        """Start the event logger background task."""
        if self._running:
            logger.debug("EventLogger already running")
            return

        if not self._db_initialized:
            await asyncio.to_thread(self._init_db)

        # Rehydrate the registry of in-flight 'dedicated' recordings from the
        # database (e.g. after a backend restart while a recording was live).
        await asyncio.to_thread(self._load_active_recordings_sync)

        self._running = True
        self._task = asyncio.create_task(self._log_loop())
        self._autostop_task = asyncio.create_task(self._autostop_loop())
        logger.info("EventLogger started")

    async def stop(self) -> None:
        """Stop the event logger and flush remaining events."""
        self._running = False

        if self._autostop_task:
            self._autostop_task.cancel()
            try:
                await self._autostop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._autostop_task = None

        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("EventLogger shutdown timeout, cancelling task")
                self._task.cancel()

        logger.info("EventLogger stopped")
    
    async def log_event(self, event: dict[str, Any]) -> None:
        """
        Queue an event for logging (non-blocking).
        
        Args:
            event: Event dict to log.
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Logger queue full, dropping oldest event")
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except asyncio.QueueEmpty:
                logger.debug("Logger queue was empty while attempting to drop the oldest event")
    
    async def _log_loop(self) -> None:
        """Main logging loop."""
        while self._running or not self._queue.empty():
            try:
                # Batch events for efficiency
                events: list[dict[str, Any]] = []
                try:
                    # Get first event
                    event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    events.append(event)
                    
                    # Try to get more events without blocking
                    while len(events) < 10:
                        try:
                            event = self._queue.get_nowait()
                            events.append(event)
                        except asyncio.QueueEmpty:
                            break
                except asyncio.TimeoutError:
                    # No events, continue
                    continue
                
                await self._write_events(events)
                    
            except asyncio.CancelledError:
                logger.debug("Log loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in log loop: {e}", exc_info=True)
    
    def _write_events_sync(self, events: list[dict[str, Any]]) -> list[tuple[int, str]]:
        """Write events to database (blocking, run via to_thread).

        Returns a list of (recording_id, reason) tuples for recordings that
        crossed a limit and should be auto-stopped by the async caller.
        """
        auto_stop: list[tuple[int, str]] = []
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            for event in events:
                cursor.execute("""
                    INSERT INTO events
                    (subject_id, timestamp, timestamp_unix, rate, message_type,
                     publisher_node_id, unique_id, attributes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event.get("subject_id"),
                    event.get("timestamp"),
                    event.get("timestamp_unix"),
                    event.get("rate"),
                    event.get("message_type"),
                    event.get("publisher_node_id"),
                    event.get("unique_id"),
                    json.dumps(event.get("attributes", []))
                ))

                # Route to active dedicated recordings.
                with self._active_lock:
                    targets = [
                        (rid, info) for rid, info in self._active_recordings.items()
                        if info["matcher"].matches_subject(event)
                    ]
                for rid, info in targets:
                    self._insert_subject_event_sync(cursor, rid, event)
                    info["event_count"] += 1
                    cursor.execute(
                        "UPDATE recordings SET event_count = ? WHERE id = ?",
                        (info["event_count"], rid),
                    )
                    if info.get("stop_on_limit"):
                        reason = self._limit_breached(info, now)
                        if reason and rid not in {r for r, _ in auto_stop}:
                            auto_stop.append((rid, reason))
                            # Drop from the registry immediately so the rest
                            # of this batch doesn't keep routing past the cap.
                            with self._active_lock:
                                self._active_recordings.pop(rid, None)

            with self._write_lock:
                self._write_count += len(events)
                should_prune = self._write_count >= 1000
                if should_prune:
                    self._write_count = 0
            if should_prune:
                # Primary: time-based retention.
                if self.retention_seconds > 0:
                    cutoff = time.time() - self.retention_seconds
                    cursor.execute("DELETE FROM events WHERE timestamp_unix < ?", (cutoff,))
                # Safety net: hard event count cap. Only meaningful if rate ×
                # retention exceeds this — keeps disk bounded under bad config.
                if self.max_events > 0:
                    cursor.execute(f"""
                        DELETE FROM events
                        WHERE id NOT IN (
                            SELECT id FROM events
                            ORDER BY id DESC
                            LIMIT {self.max_events}
                        )
                    """)

        logger.debug(f"Logged {len(events)} events to database")
        return auto_stop

    async def _write_events(self, events: list[dict[str, Any]]) -> None:
        """Write events to database without blocking the event loop."""
        try:
            auto_stop = await asyncio.to_thread(self._write_events_sync, events)
        except Exception as e:
            logger.error(f"Failed to write events: {e}", exc_info=True)
            return
        for rid, reason in auto_stop:
            await self._auto_stop_recording(rid, reason)
    
    def _get_events_sync(
        self,
        subject_id: Optional[int] = None,
        node_id: Optional[int] = None,
        unique_id: Optional[str] = None,
        message_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query logged events (blocking, run via to_thread)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = "SELECT * FROM events WHERE 1=1"
            params: list[Any] = []

            if subject_id is not None:
                query += " AND subject_id = ?"
                params.append(subject_id)

            if unique_id:
                query += " AND unique_id = ?"
                params.append(unique_id)
            elif node_id is not None:
                query += " AND publisher_node_id = ?"
                params.append(node_id)

            if message_type:
                query += " AND message_type = ?"
                params.append(message_type)

            query += " ORDER BY timestamp_unix DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor.execute(query, params)
            rows = cursor.fetchall()

        return [
            {
                "id": row["id"],
                "subject_id": row["subject_id"],
                "timestamp": row["timestamp"],
                "timestamp_unix": row["timestamp_unix"],
                "rate": row["rate"],
                "message_type": row["message_type"],
                "publisher_node_id": row["publisher_node_id"],
                "unique_id": row["unique_id"],
                "attributes": json.loads(row["attributes"]) if row["attributes"] else [],
            }
            for row in rows
        ]

    async def get_events(
        self,
        subject_id: Optional[int] = None,
        node_id: Optional[int] = None,
        unique_id: Optional[str] = None,
        message_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query logged events without blocking the event loop."""
        try:
            return await asyncio.to_thread(
                self._get_events_sync, subject_id, node_id, unique_id, message_type, limit, offset
            )
        except Exception as e:
            logger.error(f"Failed to query events: {e}", exc_info=True)
            return []

    def _get_event_count_sync(self) -> int:
        """Get total number of logged events (blocking, run via to_thread)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM events")
            return cursor.fetchone()[0]

    async def get_event_count(self) -> int:
        """Get total number of logged events without blocking the event loop."""
        try:
            return await asyncio.to_thread(self._get_event_count_sync)
        except Exception as e:
            logger.error(f"Failed to get event count: {e}")
            return 0

    def _get_buffer_stats_sync(self) -> dict[str, Any]:
        """Stats about the global events buffer: extent, count, file size."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*), MIN(timestamp_unix), MAX(timestamp_unix) FROM events"
            )
            count, oldest, newest = cursor.fetchone()
        try:
            db_size_bytes = self.db_path.stat().st_size
        except OSError:
            db_size_bytes = 0
        return {
            "retention_seconds": self.retention_seconds,
            "max_events": self.max_events,
            "event_count": int(count or 0),
            "oldest_event_unix": float(oldest) if oldest is not None else None,
            "newest_event_unix": float(newest) if newest is not None else None,
            "db_size_bytes": db_size_bytes,
        }

    async def get_buffer_stats(self) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self._get_buffer_stats_sync)
        except Exception as e:
            logger.error(f"Failed to get buffer stats: {e}", exc_info=True)
            return {
                "retention_seconds": self.retention_seconds,
                "max_events": self.max_events,
                "event_count": 0,
                "oldest_event_unix": None,
                "newest_event_unix": None,
                "db_size_bytes": 0,
            }

    def _clear_events_sync(self) -> int:
        """Clear all logged events (blocking, run via to_thread)."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM events")
            count = cursor.rowcount
        logger.info(f"Cleared {count} events from database")
        return count

    async def clear_events(self) -> int:
        """Clear all logged events without blocking the event loop."""
        try:
            return await asyncio.to_thread(self._clear_events_sync)
        except Exception as e:
            logger.error(f"Failed to clear events: {e}", exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # Node history (lifecycle events, 30-day retention)
    # ------------------------------------------------------------------

    NODE_HISTORY_RETENTION_DAYS = 30

    def _log_node_event_sync(
        self,
        node_id: int,
        event_type: str,
        detail: Optional[dict] = None,
        unique_id: Optional[str] = None,
    ) -> list[tuple[int, str]]:
        """Write a node-history row. For 'service_call', also route to matching
        active recordings. Returns auto-stop list (mirrors _write_events_sync)."""
        auto_stop: list[tuple[int, str]] = []
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO node_history (node_id, unique_id, timestamp_unix, event_type, detail) VALUES (?, ?, ?, ?, ?)",
                (node_id, unique_id, now, event_type, json.dumps(detail) if detail else None),
            )
            with self._write_lock:
                self._write_count += 1
                should_prune = self._write_count % 100 == 0
            if should_prune:
                cutoff = now - self.NODE_HISTORY_RETENTION_DAYS * 86400
                cursor.execute("DELETE FROM node_history WHERE timestamp_unix < ?", (cutoff,))

            if event_type == "service_call" and detail:
                service_id = detail.get("service_id")
                probe = {"service_id": service_id, "node_id": node_id}
                with self._active_lock:
                    targets = [
                        (rid, info) for rid, info in self._active_recordings.items()
                        if info["matcher"].matches_service(probe)
                    ]
                for rid, info in targets:
                    self._insert_service_event_sync(cursor, rid, node_id, unique_id, detail, now)
                    info["event_count"] += 1
                    cursor.execute(
                        "UPDATE recordings SET event_count = ? WHERE id = ?",
                        (info["event_count"], rid),
                    )
                    if info.get("stop_on_limit"):
                        reason = self._limit_breached(info, now)
                        if reason and rid not in {r for r, _ in auto_stop}:
                            auto_stop.append((rid, reason))
                            with self._active_lock:
                                self._active_recordings.pop(rid, None)
        return auto_stop

    async def log_node_event(self, node_id: int, event_type: str, detail: Optional[dict] = None, unique_id: Optional[str] = None) -> None:
        try:
            auto_stop = await asyncio.to_thread(self._log_node_event_sync, node_id, event_type, detail, unique_id)
            logger.debug(f"Node history: node={node_id} uid={unique_id} type={event_type}")
        except Exception as e:
            logger.error(f"Failed to log node event: {e}", exc_info=True)
            return
        for rid, reason in auto_stop:
            await self._auto_stop_recording(rid, reason)

    def _get_node_history_sync(
        self,
        node_id: int,
        since_unix: Optional[float] = None,
        event_types: Optional[list[str]] = None,
        limit: int = 200,
        unique_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if unique_id:
                query = "SELECT * FROM node_history WHERE unique_id = ?"
                params: list[Any] = [unique_id]
            else:
                query = "SELECT * FROM node_history WHERE node_id = ?"
                params = [node_id]
            if since_unix is not None:
                query += " AND timestamp_unix >= ?"
                params.append(since_unix)
            if event_types:
                placeholders = ",".join("?" for _ in event_types)
                query += f" AND event_type IN ({placeholders})"
                params.extend(event_types)
            query += " ORDER BY timestamp_unix DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": row["id"],
                "node_id": row["node_id"],
                "unique_id": row["unique_id"],
                "timestamp_unix": row["timestamp_unix"],
                "event_type": row["event_type"],
                "detail": json.loads(row["detail"]) if row["detail"] else None,
            }
            for row in rows
        ]

    async def get_node_history(
        self,
        node_id: int,
        since_unix: Optional[float] = None,
        event_types: Optional[list[str]] = None,
        limit: int = 200,
        unique_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(
                self._get_node_history_sync, node_id, since_unix, event_types, limit, unique_id
            )
        except Exception as e:
            logger.error(f"Failed to query node history: {e}", exc_info=True)
            return []

    def _get_subject_summary_sync(self, node_id: int, unique_id: Optional[str] = None) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if unique_id:
                where_clause = "unique_id = ?"
                param = unique_id
            else:
                where_clause = "publisher_node_id = ?"
                param = node_id
            rows = conn.execute(f"""
                SELECT subject_id, message_type,
                       COUNT(*) as total_events,
                       AVG(rate) as avg_rate,
                       MIN(timestamp_unix) as first_seen_unix,
                       MAX(timestamp_unix) as last_seen_unix
                FROM events
                WHERE {where_clause}
                  AND subject_id NOT IN (7509, 7510)
                GROUP BY subject_id
                ORDER BY subject_id
            """, (param,)).fetchall()
        return [
            {
                "subject_id": row["subject_id"],
                "message_type": row["message_type"],
                "total_events": row["total_events"],
                "avg_rate": round(row["avg_rate"], 1) if row["avg_rate"] else 0,
                "first_seen_unix": row["first_seen_unix"],
                "last_seen_unix": row["last_seen_unix"],
            }
            for row in rows
        ]

    async def get_subject_summary(self, node_id: int, unique_id: Optional[str] = None) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._get_subject_summary_sync, node_id, unique_id)
        except Exception as e:
            logger.error(f"Failed to query subject summary: {e}", exc_info=True)
            return []

    def _get_service_call_history_sync(
        self,
        service_id: int,
        since_unix: Optional[float] = None,
        limit: int = 50,
        node_id: Optional[int] = None,
        unique_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = (
                "SELECT * FROM node_history"
                " WHERE event_type = 'service_call'"
                " AND json_extract(detail, '$.service_id') = ?"
            )
            params: list[Any] = [service_id]
            if unique_id:
                query += " AND unique_id = ?"
                params.append(unique_id)
            elif node_id is not None:
                query += " AND node_id = ?"
                params.append(node_id)
            if since_unix is not None:
                query += " AND timestamp_unix >= ?"
                params.append(since_unix)
            query += " ORDER BY timestamp_unix DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()

        results = []
        for row in rows:
            detail = json.loads(row["detail"]) if row["detail"] else {}
            results.append({
                "node_id": row["node_id"],
                "unique_id": row["unique_id"],
                "timestamp_unix": row["timestamp_unix"],
                "service_id": detail.get("service_id"),
                "service_type": detail.get("service_type"),
                "status": detail.get("status"),
                "latency_ms": detail.get("latency_ms"),
                "response": detail.get("response"),
            })
        return results

    async def get_service_call_history(
        self,
        service_id: int,
        since_unix: Optional[float] = None,
        limit: int = 50,
        node_id: Optional[int] = None,
        unique_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(
                self._get_service_call_history_sync, service_id, since_unix, limit, node_id, unique_id
            )
        except Exception as e:
            logger.error(f"Failed to query service call history: {e}", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Identity map persistence
    # ------------------------------------------------------------------

    def _save_identity_map_sync(self, records: list[dict[str, Any]]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            current_ids = [rec["unique_id"] for rec in records]
            if current_ids:
                placeholders = ",".join("?" * len(current_ids))
                cursor.execute(f"DELETE FROM identity_map WHERE unique_id NOT IN ({placeholders})", current_ids)
            else:
                cursor.execute("DELETE FROM identity_map")
            for rec in records:
                cursor.execute(
                    "INSERT OR REPLACE INTO identity_map (unique_id, current_node_id, last_seen_unix, node_name, previous_node_ids) VALUES (?, ?, ?, ?, ?)",
                    (rec["unique_id"], rec.get("current_node_id"), rec.get("last_seen_unix"), rec.get("node_name"), json.dumps(rec.get("previous_node_ids", []))),
                )
        logger.debug(f"Saved {len(records)} identity map entries")

    async def save_identity_map(self, records: list[dict[str, Any]]) -> None:
        try:
            await asyncio.to_thread(self._save_identity_map_sync, records)
        except Exception as e:
            logger.error(f"Failed to save identity map: {e}", exc_info=True)

    def _load_identity_map_sync(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM identity_map").fetchall()
        return [
            {
                "unique_id": row["unique_id"],
                "current_node_id": row["current_node_id"],
                "last_seen_unix": row["last_seen_unix"],
                "node_name": row["node_name"],
                "previous_node_ids": json.loads(row["previous_node_ids"]) if row["previous_node_ids"] else [],
            }
            for row in rows
        ]

    async def load_identity_map(self) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._load_identity_map_sync)
        except Exception as e:
            logger.error(f"Failed to load identity map: {e}", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Node data persistence (unique_id as primary key)
    # ------------------------------------------------------------------

    def _save_node_data_sync(self, unique_id: str, snapshot: dict) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO node_data
                   (unique_id, node_name, software_version_major, software_version_minor,
                    publishers, subscribers, servers, clients,
                    unique_id_bytes, last_uptime, last_seen, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    unique_id,
                    snapshot.get("name"),
                    snapshot.get("software_version", {}).get("major") if snapshot.get("software_version") else None,
                    snapshot.get("software_version", {}).get("minor") if snapshot.get("software_version") else None,
                    json.dumps(snapshot.get("publishers", [])),
                    json.dumps(snapshot.get("subscribers", [])),
                    json.dumps(snapshot.get("servers", [])),
                    json.dumps(snapshot.get("clients", [])),
                    json.dumps(snapshot.get("unique_id", [])),
                    snapshot.get("uptime"),
                    snapshot.get("last_seen"),
                    time.time(),
                ),
            )

    async def save_node_data(self, unique_id: str, snapshot: dict) -> None:
        try:
            await asyncio.to_thread(self._save_node_data_sync, unique_id, snapshot)
            logger.debug(f"Saved node data for {unique_id}")
        except Exception as e:
            logger.error(f"Failed to save node data: {e}", exc_info=True)

    def _load_all_node_data_sync(self) -> dict[str, dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM node_data").fetchall()
        result = {}
        for row in rows:
            uid = row["unique_id"]
            sv = None
            if row["software_version_major"] is not None:
                sv = {"major": row["software_version_major"], "minor": row["software_version_minor"]}
            result[uid] = {
                "unique_id": json.loads(row["unique_id_bytes"]) if row["unique_id_bytes"] else [],
                "name": row["node_name"],
                "software_version": sv,
                "publishers": json.loads(row["publishers"]) if row["publishers"] else [],
                "subscribers": json.loads(row["subscribers"]) if row["subscribers"] else [],
                "servers": json.loads(row["servers"]) if row["servers"] else [],
                "clients": json.loads(row["clients"]) if row["clients"] else [],
                "uptime": row["last_uptime"],
                "last_seen": row["last_seen"],
            }
        return result

    async def load_all_node_data(self) -> dict[str, dict]:
        try:
            return await asyncio.to_thread(self._load_all_node_data_sync)
        except Exception as e:
            logger.error(f"Failed to load node data: {e}", exc_info=True)
            return {}

    def _delete_node_data_sync(self, unique_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM node_data WHERE unique_id = ?", (unique_id,))
            conn.execute("DELETE FROM identity_map WHERE unique_id = ?", (unique_id,))

    async def delete_node_data(self, unique_id: str) -> None:
        try:
            await asyncio.to_thread(self._delete_node_data_sync, unique_id)
            logger.info(f"Deleted node data for {unique_id}")
        except Exception as e:
            logger.error(f"Failed to delete node data: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Live ingest routing helpers (shared between subjects & service calls)
    # ------------------------------------------------------------------

    def _insert_subject_event_sync(self, cursor: sqlite3.Cursor, recording_id: int, event: dict) -> None:
        cursor.execute(
            "INSERT INTO recording_events"
            " (recording_id, kind, timestamp_unix, timestamp_iso,"
            "  subject_or_service_id, publisher_node_id, unique_id,"
            "  message_type, rate, attributes_json)"
            " VALUES (?, 'subject', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                recording_id,
                event.get("timestamp_unix"),
                event.get("timestamp"),
                event.get("subject_id"),
                event.get("publisher_node_id"),
                event.get("unique_id"),
                event.get("message_type"),
                event.get("rate"),
                json.dumps(event.get("attributes", [])),
            ),
        )

    def _insert_service_event_sync(
        self,
        cursor: sqlite3.Cursor,
        recording_id: int,
        node_id: Optional[int],
        unique_id: Optional[str],
        detail: dict,
        timestamp_unix: float,
    ) -> None:
        cursor.execute(
            "INSERT INTO recording_events"
            " (recording_id, kind, timestamp_unix, timestamp_iso,"
            "  subject_or_service_id, publisher_node_id, unique_id,"
            "  message_type, rate, attributes_json)"
            " VALUES (?, 'service_call', ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                recording_id,
                timestamp_unix,
                datetime.datetime.fromtimestamp(timestamp_unix, tz=datetime.timezone.utc).isoformat(),
                detail.get("service_id"),
                node_id,
                unique_id,
                detail.get("service_type"),
                json.dumps({
                    "status": detail.get("status"),
                    "latency_ms": detail.get("latency_ms"),
                    "response": detail.get("response"),
                }),
            ),
        )

    @staticmethod
    def _limit_breached(info: dict, now: float) -> Optional[str]:
        max_n = info.get("max_events")
        if max_n and info.get("event_count", 0) >= max_n:
            return "max_events"
        max_t = info.get("max_length_seconds")
        if max_t and (now - info.get("start_unix", now)) >= max_t:
            return "max_length_seconds"
        return None

    def _make_active_entry(self, rec: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": rec["id"],
            "start_unix": rec["start_unix"],
            "max_length_seconds": rec.get("max_length_seconds"),
            "max_events": rec.get("max_events"),
            "stop_on_limit": rec.get("stop_on_limit"),
            "event_count": int(rec.get("event_count") or 0),
            "matcher": FilterMatcher(rec.get("filter")),
        }

    def _load_active_recordings_sync(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM recordings"
                " WHERE end_unix IS NULL AND events_source = 'dedicated'"
            ).fetchall()
        with self._active_lock:
            self._active_recordings = {}
            for row in rows:
                rec = self._row_to_recording(row)
                self._active_recordings[rec["id"]] = self._make_active_entry(rec)
        if rows:
            logger.info(f"Rehydrated {len(rows)} active recording(s) from DB")

    async def _autostop_loop(self) -> None:
        """Periodically check active recordings for time-based limit breaches.

        Live routing checks limits on each matching event; this loop catches
        recordings that are silent (no events arriving) but whose max_length
        has elapsed.
        """
        try:
            while self._running:
                await asyncio.sleep(5.0)
                now = time.time()
                with self._active_lock:
                    expired = [
                        rid for rid, info in self._active_recordings.items()
                        if info.get("stop_on_limit") and self._limit_breached(info, now)
                    ]
                for rid in expired:
                    await self._auto_stop_recording(rid, "max_length_seconds")
        except asyncio.CancelledError:
            pass

    def _auto_stop_recording_sync(self, recording_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE recordings SET end_unix = ?, auto_stopped = 1"
                " WHERE id = ? AND end_unix IS NULL",
                (time.time(), recording_id),
            )
            return cursor.rowcount > 0

    async def _auto_stop_recording(self, recording_id: int, reason: str) -> None:
        with self._active_lock:
            self._active_recordings.pop(recording_id, None)
        ok = await asyncio.to_thread(self._auto_stop_recording_sync, recording_id)
        if ok:
            logger.info(f"Auto-stopped recording {recording_id} (reason={reason})")

    def _copy_global_to_dedicated_sync(
        self, recording_id: int, start: float, end: float, filt: dict[str, list]
    ) -> int:
        """Snapshot matching events from the global buffer into the recording's
        dedicated store. Used by quick-save."""
        clause, params = self._filter_clause(filt)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM events"
                f" WHERE timestamp_unix >= ? AND timestamp_unix <= ?{clause}"
                f" ORDER BY timestamp_unix ASC",
                [start, end, *params],
            ).fetchall()
            cursor = conn.cursor()
            for row in rows:
                cursor.execute(
                    "INSERT INTO recording_events"
                    " (recording_id, kind, timestamp_unix, timestamp_iso,"
                    "  subject_or_service_id, publisher_node_id, unique_id,"
                    "  message_type, rate, attributes_json)"
                    " VALUES (?, 'subject', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (recording_id, row["timestamp_unix"], row["timestamp"],
                     row["subject_id"], row["publisher_node_id"], row["unique_id"],
                     row["message_type"], row["rate"], row["attributes"]),
                )
            cursor.execute(
                "UPDATE recordings SET event_count = ? WHERE id = ?",
                (len(rows), recording_id),
            )
        return len(rows)

    # ------------------------------------------------------------------
    # Recordings (named time-range bookmarks over the events log)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_filter(filt: Optional[dict[str, Any]]) -> dict[str, list]:
        """Return a filter dict with only the keys we recognize and validate types."""
        if not isinstance(filt, dict):
            return {}
        out: dict[str, list] = {}
        for key in ("subject_ids", "node_ids", "service_ids"):
            vals = filt.get(key)
            if isinstance(vals, list):
                cleaned = [int(v) for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
                if cleaned:
                    out[key] = cleaned
        msg_types = filt.get("message_types")
        if isinstance(msg_types, list):
            cleaned_msg = [str(v) for v in msg_types if isinstance(v, str) and v]
            if cleaned_msg:
                out["message_types"] = cleaned_msg
        return out

    @staticmethod
    def _filter_clause(filt: dict[str, list]) -> tuple[str, list]:
        """Build a WHERE-suffix clause and params from a normalized filter.

        OR semantics across dimensions: an event matches if it's in ANY of
        the supplied lists. Empty filter → no clause (matches everything).
        Used against the global `events` table — service_ids is ignored here
        because that table only holds subject events.
        """
        clauses: list[str] = []
        params: list[Any] = []
        sids = filt.get("subject_ids")
        if sids:
            placeholders = ",".join("?" * len(sids))
            clauses.append(f"subject_id IN ({placeholders})")
            params.extend(sids)
        nids = filt.get("node_ids")
        if nids:
            placeholders = ",".join("?" * len(nids))
            clauses.append(f"publisher_node_id IN ({placeholders})")
            params.extend(nids)
        msgs = filt.get("message_types")
        if msgs:
            placeholders = ",".join("?" * len(msgs))
            clauses.append(f"message_type IN ({placeholders})")
            params.extend(msgs)
        if not clauses:
            return "", params
        return " AND (" + " OR ".join(clauses) + ")", params

    def _create_recording_sync(
        self,
        name: str,
        start_unix: float,
        end_unix: Optional[float],
        filt: dict[str, list],
        notes: Optional[str],
        max_length_seconds: Optional[float],
        max_events: Optional[int],
        stop_on_limit: bool,
        events_source: str,
    ) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO recordings (name, start_unix, end_unix, filter_json, notes, created_at,"
                " max_length_seconds, max_events, stop_on_limit, auto_stopped, event_count, events_source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)",
                (name, start_unix, end_unix, json.dumps(filt) if filt else None, notes, time.time(),
                 max_length_seconds, max_events, 1 if stop_on_limit else 0, events_source),
            )
            return int(cursor.lastrowid)

    async def create_recording(
        self,
        name: str,
        start_unix: Optional[float] = None,
        end_unix: Optional[float] = None,
        filter_spec: Optional[dict[str, Any]] = None,
        notes: Optional[str] = None,
        max_length_seconds: Optional[float] = None,
        max_events: Optional[int] = None,
        stop_on_limit: bool = False,
        events_source: str = "dedicated",
    ) -> int:
        """Create a recording. Returns the new recording id.

        Phase 2 default is events_source='dedicated': matching events stream
        into the recording_events table from start (when end_unix is None) or
        get snapshot-copied from the global buffer at creation time (when
        end_unix is set — i.e. quick-save). Pass events_source='global' to
        create a Phase 1-style bookmark.
        """
        filt = self._normalize_filter(filter_spec)
        start = start_unix if start_unix is not None else time.time()
        rec_id = await asyncio.to_thread(
            self._create_recording_sync,
            name, start, end_unix, filt, notes,
            max_length_seconds, max_events, stop_on_limit, events_source,
        )
        if events_source == "dedicated":
            if end_unix is None:
                # Live: register so the writer thread starts routing events.
                rec = await self.get_recording(rec_id)
                if rec is not None:
                    with self._active_lock:
                        self._active_recordings[rec_id] = self._make_active_entry(rec)
            else:
                # Quick-save: snapshot matching events from the global buffer.
                await asyncio.to_thread(
                    self._copy_global_to_dedicated_sync, rec_id, start, end_unix, filt
                )
        return rec_id

    def _stop_recording_sync(self, recording_id: int, end_unix: float) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE recordings SET end_unix = ? WHERE id = ? AND end_unix IS NULL",
                (end_unix, recording_id),
            )
            return cursor.rowcount > 0

    async def stop_recording(self, recording_id: int) -> bool:
        ok = await asyncio.to_thread(self._stop_recording_sync, recording_id, time.time())
        if ok:
            with self._active_lock:
                self._active_recordings.pop(recording_id, None)
        return ok

    def _update_recording_sync(
        self,
        recording_id: int,
        name: Optional[str],
        notes: Optional[str],
        max_length_seconds: Optional[float],
        max_events: Optional[int],
        stop_on_limit: Optional[bool],
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes)
        if max_length_seconds is not None:
            sets.append("max_length_seconds = ?")
            params.append(float(max_length_seconds))
        if max_events is not None:
            sets.append("max_events = ?")
            params.append(int(max_events))
        if stop_on_limit is not None:
            sets.append("stop_on_limit = ?")
            params.append(1 if stop_on_limit else 0)
        if not sets:
            return False
        params.append(recording_id)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE recordings SET {', '.join(sets)} WHERE id = ?", params)
            return cursor.rowcount > 0

    async def update_recording(
        self,
        recording_id: int,
        name: Optional[str] = None,
        notes: Optional[str] = None,
        max_length_seconds: Optional[float] = None,
        max_events: Optional[int] = None,
        stop_on_limit: Optional[bool] = None,
    ) -> bool:
        ok = await asyncio.to_thread(
            self._update_recording_sync,
            recording_id, name, notes,
            max_length_seconds, max_events, stop_on_limit,
        )
        if not ok:
            return False
        # If the recording is live (dedicated + still running) and limits
        # changed, refresh the in-memory registry so live ingest sees the
        # new caps immediately. Otherwise auto-stop would use stale values.
        if max_length_seconds is not None or max_events is not None or stop_on_limit is not None:
            with self._active_lock:
                if recording_id in self._active_recordings:
                    info = self._active_recordings[recording_id]
                    if max_length_seconds is not None:
                        info["max_length_seconds"] = float(max_length_seconds)
                    if max_events is not None:
                        info["max_events"] = int(max_events)
                    if stop_on_limit is not None:
                        info["stop_on_limit"] = bool(stop_on_limit)
        return True

    def _row_to_recording(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "start_unix": row["start_unix"],
            "end_unix": row["end_unix"],
            "filter": json.loads(row["filter_json"]) if row["filter_json"] else {},
            "notes": row["notes"],
            "created_at": row["created_at"],
            "max_length_seconds": row["max_length_seconds"],
            "max_events": row["max_events"],
            "stop_on_limit": bool(row["stop_on_limit"]),
            "auto_stopped": bool(row["auto_stopped"]),
            "event_count": int(row["event_count"] or 0),
            "events_source": row["events_source"] or "global",
        }

    def _list_recordings_sync(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM recordings ORDER BY start_unix DESC"
            ).fetchall()
        return [self._row_to_recording(row) for row in rows]

    async def list_recordings(self) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._list_recordings_sync)
        except Exception as e:
            logger.error(f"Failed to list recordings: {e}", exc_info=True)
            return []

    def _get_recording_sync(self, recording_id: int) -> Optional[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
        return self._row_to_recording(row) if row else None

    async def get_recording(self, recording_id: int) -> Optional[dict[str, Any]]:
        return await asyncio.to_thread(self._get_recording_sync, recording_id)

    def _recording_stats_sync(self, recording: dict[str, Any]) -> dict[str, Any]:
        """Return event count + subject set for the recording.

        For events_source='dedicated', counts come from recording_events.
        For events_source='global', falls back to the time-range × filter
        scan over the global events table.
        """
        start = recording["start_unix"]
        end = recording["end_unix"] if recording["end_unix"] is not None else time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            if recording.get("events_source") == "dedicated":
                cursor.execute(
                    "SELECT COUNT(*) FROM recording_events WHERE recording_id = ?",
                    (recording["id"],),
                )
                count = int(cursor.fetchone()[0])
                cursor.execute(
                    "SELECT DISTINCT subject_or_service_id FROM recording_events"
                    " WHERE recording_id = ? AND kind = 'subject'",
                    (recording["id"],),
                )
                subjects = sorted(int(row[0]) for row in cursor.fetchall() if row[0] is not None)
            else:
                filt = recording.get("filter") or {}
                clause, params = self._filter_clause(filt)
                cursor.execute(
                    f"SELECT COUNT(*) FROM events WHERE timestamp_unix >= ? AND timestamp_unix <= ?{clause}",
                    [start, end, *params],
                )
                count = int(cursor.fetchone()[0])
                cursor.execute(
                    f"SELECT DISTINCT subject_id FROM events WHERE timestamp_unix >= ? AND timestamp_unix <= ?{clause}",
                    [start, end, *params],
                )
                subjects = sorted(int(row[0]) for row in cursor.fetchall())
        return {
            "event_count": count,
            "subjects": subjects,
            "duration_seconds": max(0.0, end - start),
        }

    async def get_recording_stats(self, recording_id: int) -> Optional[dict[str, Any]]:
        rec = await self.get_recording(recording_id)
        if not rec:
            return None
        stats = await asyncio.to_thread(self._recording_stats_sync, rec)
        return {**rec, **stats}

    def _delete_recording_sync(self, recording_id: int, purge_events: bool) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
            if not row:
                return False
            rec = self._row_to_recording(row)
            # Always clean up the per-recording event store (cheap; bounded by index).
            conn.execute("DELETE FROM recording_events WHERE recording_id = ?", (recording_id,))
            if purge_events and rec.get("events_source") == "global":
                # Legacy bookmark: also delete in-range events from the shared
                # events table. Dedicated recordings don't touch the global
                # table; their events live only in recording_events (already
                # deleted above).
                start = rec["start_unix"]
                end = rec["end_unix"] if rec["end_unix"] is not None else time.time()
                clause, params = self._filter_clause(rec.get("filter") or {})
                conn.execute(
                    f"DELETE FROM events WHERE timestamp_unix >= ? AND timestamp_unix <= ?{clause}",
                    [start, end, *params],
                )
            conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        return True

    async def delete_recording(self, recording_id: int, purge_events: bool = False) -> bool:
        with self._active_lock:
            self._active_recordings.pop(recording_id, None)
        return await asyncio.to_thread(self._delete_recording_sync, recording_id, purge_events)

    def _iter_recording_events_sync(
        self,
        recording: dict[str, Any],
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if recording.get("events_source") == "dedicated":
            return self._iter_dedicated_events_sync(recording["id"], limit, offset)
        return self._iter_global_events_sync(recording, limit, offset)

    def _iter_global_events_sync(
        self, recording: dict[str, Any], limit: Optional[int], offset: int
    ) -> list[dict[str, Any]]:
        start = recording["start_unix"]
        end = recording["end_unix"] if recording["end_unix"] is not None else time.time()
        filt = recording.get("filter") or {}
        clause, params = self._filter_clause(filt)
        query = (
            f"SELECT * FROM events WHERE timestamp_unix >= ? AND timestamp_unix <= ?{clause}"
            " ORDER BY timestamp_unix ASC"
        )
        all_params: list[Any] = [start, end, *params]
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            all_params.extend([limit, offset])
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, all_params).fetchall()
        return [
            {
                "id": row["id"],
                "kind": "subject",
                "subject_id": row["subject_id"],
                "service_id": None,
                "timestamp": row["timestamp"],
                "timestamp_unix": row["timestamp_unix"],
                "rate": row["rate"],
                "message_type": row["message_type"],
                "publisher_node_id": row["publisher_node_id"],
                "unique_id": row["unique_id"],
                "attributes": json.loads(row["attributes"]) if row["attributes"] else [],
            }
            for row in rows
        ]

    def _iter_dedicated_events_sync(
        self, recording_id: int, limit: Optional[int], offset: int
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM recording_events WHERE recording_id = ?"
            " ORDER BY timestamp_unix ASC, id ASC"
        )
        params: list[Any] = [recording_id]
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            kind = row["kind"]
            subject_id = row["subject_or_service_id"] if kind == "subject" else None
            service_id = row["subject_or_service_id"] if kind == "service_call" else None
            out.append({
                "id": row["id"],
                "kind": kind,
                "subject_id": subject_id,
                "service_id": service_id,
                "timestamp": row["timestamp_iso"],
                "timestamp_unix": row["timestamp_unix"],
                "rate": row["rate"],
                "message_type": row["message_type"],
                "publisher_node_id": row["publisher_node_id"],
                "unique_id": row["unique_id"],
                "attributes": json.loads(row["attributes_json"]) if row["attributes_json"] else [],
            })
        return out

    async def get_recording_events(
        self, recording_id: int, limit: Optional[int] = None, offset: int = 0
    ) -> list[dict[str, Any]]:
        rec = await self.get_recording(recording_id)
        if not rec:
            return []
        return await asyncio.to_thread(self._iter_recording_events_sync, rec, limit, offset)
