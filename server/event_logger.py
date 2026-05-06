#!/usr/bin/env python3

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional, Any
import json

logger = logging.getLogger(__name__)


class EventLogger:
    """
    Optional SQLite-based event logging for persistence and replay.
    
    Stores events in a local database for historical analysis and replay.
    Can be enabled or disabled independently of live streaming.
    """
    
    def __init__(self, db_path: str = "telemetry.db", max_events: int = 100000) -> None:
        """
        Initialize the event logger.
        
        Args:
            db_path: Path to SQLite database file (default: telemetry.db).
            max_events: Maximum events to keep (older ones deleted). 0 = unlimited.
        """
        self.db_path = Path(db_path)
        self.max_events = max_events
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._write_count = 0

        self._init_db()
    
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

                # Create indexes (after migration so unique_id column exists)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_subject ON events(subject_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_node ON events(publisher_node_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp_unix)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_unique_id ON events(unique_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_nh_node ON node_history(node_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_nh_uid ON node_history(unique_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_nh_timestamp ON node_history(timestamp_unix)")

                conn.execute("PRAGMA journal_mode=WAL")
            logger.info(f"Database initialized at {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}", exc_info=True)
    
    async def start(self) -> None:
        """Start the event logger background task."""
        if self._running:
            logger.debug("EventLogger already running")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._log_loop())
        logger.info("EventLogger started")
    
    async def stop(self) -> None:
        """Stop the event logger and flush remaining events."""
        self._running = False
        
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
    
    def _write_events_sync(self, events: list[dict[str, Any]]) -> None:
        """Write events to database (blocking, run via to_thread)."""
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

            self._write_count += len(events)
            if self.max_events > 0 and self._write_count >= self.max_events:
                self._write_count = 0
                cursor.execute(f"""
                    DELETE FROM events
                    WHERE id NOT IN (
                        SELECT id FROM events
                        ORDER BY id DESC
                        LIMIT {self.max_events}
                    )
                """)

        logger.debug(f"Logged {len(events)} events to database")

    async def _write_events(self, events: list[dict[str, Any]]) -> None:
        """Write events to database without blocking the event loop."""
        try:
            await asyncio.to_thread(self._write_events_sync, events)
        except Exception as e:
            logger.error(f"Failed to write events: {e}", exc_info=True)
    
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

    def _log_node_event_sync(self, node_id: int, event_type: str, detail: Optional[dict] = None, unique_id: Optional[str] = None) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO node_history (node_id, unique_id, timestamp_unix, event_type, detail) VALUES (?, ?, ?, ?, ?)",
                (node_id, unique_id, time.time(), event_type, json.dumps(detail) if detail else None),
            )
            # Prune old entries periodically
            self._write_count += 1
            if self._write_count % 100 == 0:
                cutoff = time.time() - self.NODE_HISTORY_RETENTION_DAYS * 86400
                cursor.execute("DELETE FROM node_history WHERE timestamp_unix < ?", (cutoff,))

    async def log_node_event(self, node_id: int, event_type: str, detail: Optional[dict] = None, unique_id: Optional[str] = None) -> None:
        try:
            await asyncio.to_thread(self._log_node_event_sync, node_id, event_type, detail, unique_id)
            logger.debug(f"Node history: node={node_id} uid={unique_id} type={event_type}")
        except Exception as e:
            logger.error(f"Failed to log node event: {e}", exc_info=True)

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
