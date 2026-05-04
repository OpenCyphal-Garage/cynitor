#!/usr/bin/env python3

import asyncio
import logging
import sqlite3
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
                        attributes TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Create indexes separately
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_subject ON events(subject_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_node ON events(publisher_node_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON events(timestamp_unix)")
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
                     publisher_node_id, attributes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    event.get("subject_id"),
                    event.get("timestamp"),
                    event.get("timestamp_unix"),
                    event.get("rate"),
                    event.get("message_type"),
                    event.get("publisher_node_id"),
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

            if node_id is not None:
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
                "attributes": json.loads(row["attributes"]) if row["attributes"] else [],
            }
            for row in rows
        ]

    async def get_events(
        self,
        subject_id: Optional[int] = None,
        node_id: Optional[int] = None,
        message_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query logged events without blocking the event loop."""
        try:
            return await asyncio.to_thread(
                self._get_events_sync, subject_id, node_id, message_type, limit, offset
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
