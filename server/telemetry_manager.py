import asyncio
import datetime
import logging
from collections import defaultdict
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TelemetryManager:
    """
    Consume scanner events and distribute them to subscribers.

    The manager also maintains a latest-state cache keyed by subject ID and node ID.
    Events are normalized into JSON-compatible dictionaries before caching.
    """

    def __init__(self, scanner: Any) -> None:
        """
        Initialize the TelemetryManager.
        
        Args:
            scanner: ScannerNode instance to consume messages from.
        """
        self.scanner = scanner

        # Latest telemetry by subject_id
        self.latest_by_subject: dict[int, dict[str, Any]] = {}

        # Latest telemetry by node_id then subject_id
        self.latest_by_node: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)

        # Async subscribers: queue -> label ("logger", "client", ...)
        self.subscribers: dict[asyncio.Queue, str] = {}

        # Events discarded because a subscriber's queue was full, by label
        self.dropped: dict[str, int] = defaultdict(int)

        # Manager control
        self._running = False
        self._loop_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the telemetry consumption loop."""
        if self._running:
            logger.debug("TelemetryManager already running")
            return

        self._running = True
        self._loop_task = asyncio.create_task(self._telemetry_loop())
        logger.info("TelemetryManager started")

    async def stop(self) -> None:
        """Stop the telemetry consumption loop and cleanup."""
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        logger.info("TelemetryManager stopped")

    def subscribe(self, max_queue: int = 100, label: str = "client") -> asyncio.Queue:
        """
        Create a queue that receives telemetry events.
        
        Args:
            max_queue: Maximum size of the event queue.
            label: Name under which events this queue misses are counted in ``dropped``.
        
        Returns:
            asyncio.Queue subscribed to telemetry events.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self.subscribers[queue] = label
        logger.debug(f"New subscriber added (total: {len(self.subscribers)})")
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """
        Remove a subscriber queue.
        
        Args:
            queue: The queue to unsubscribe.
        """
        self.subscribers.pop(queue, None)
        logger.debug(f"Subscriber removed (total: {len(self.subscribers)})")

    def get_latest_subject(self, subject_id: int) -> Optional[dict[str, Any]]:
        """
        Get the latest telemetry for a subject.
        
        Args:
            subject_id: The subject ID to query.
        
        Returns:
            Latest event dict or None if not found.
        """
        return self.latest_by_subject.get(subject_id)

    def get_latest_node(self, node_id: int) -> dict[int, dict[str, Any]]:
        """
        Get the latest telemetry for all subjects published by a node.
        
        Args:
            node_id: The node ID to query.
        
        Returns:
            Dictionary mapping subject_id to latest event dict.
        """
        return self.latest_by_node.get(node_id, {})
    
    def get_all_nodes_info(self) -> dict[str, Any]:
        """
        Get information about all discovered nodes on the CAN network.
        
        Returns:
            Dictionary with node_count and detailed info for each node.
        """
        nodes_info = {}
        
        for node_id, node in self.scanner.all_nodes.items():
            if node.has_appeared:
                info_response = getattr(node, "info_response", None)

                # Convert unique_id array to a plain list of Python ints.
                unique_id = self._as_int_list(getattr(node, "unique_id", []))

                # Convert publisher/subscriber/client/server IDs by extracting plain ints.
                publishers = self._as_int_list(getattr(node, "publisher_SubjectIDs", []))
                subscribers = self._as_int_list(getattr(node, "subscriber_SubjectIDs", []))
                clients = self._as_int_list(getattr(node, "client_ServiceIDs", []))
                servers = self._as_int_list(getattr(node, "server_ServiceIDs", []))
                
                unique_id_hex = self.scanner.identity_map.get_uid(node_id)

                nodes_info[node_id] = {
                    "node_id": node_id,
                    "unique_id": unique_id,
                    "unique_id_hex": unique_id_hex,
                    "uptime": self._to_builtin_int(getattr(node, "uptime", None)),
                    "has_disappeared": node.has_disappeared,
                    "has_responded_to_getinfo": node.has_responded_to_getInfo,
                    "name": self._decode_name_field(info_response.name) if info_response else None,
                    "software_version": {
                        "major": self._to_builtin_int(info_response.software_version.major),
                        "minor": self._to_builtin_int(info_response.software_version.minor)
                    } if info_response else None,
                    "publishers": publishers,
                    "subscribers": subscribers,
                    "clients": clients,
                    "servers": servers,
                    "last_seen": self._serialize_timestamps(getattr(node, "last_seen", None))
                }
        
        live_node_ids = set(nodes_info.keys())
        for ghost in self.scanner.identity_map.detached_identities(live_node_ids):
            uid = ghost["unique_id_hex"]
            key = f"uid:{uid}"
            snap = ghost.get("snapshot") or {}
            prev_ids = ghost.get("previous_node_ids", [])
            nodes_info[key] = {
                "node_id": None,
                "last_node_id": prev_ids[-1] if prev_ids else None,
                "unique_id": snap.get("unique_id"),
                "unique_id_hex": uid,
                "uptime": snap.get("uptime"),
                "has_disappeared": True,
                "has_responded_to_getinfo": bool(snap),
                "name": snap.get("name") or ghost["name"],
                "software_version": snap.get("software_version"),
                "publishers": snap.get("publishers", []),
                "subscribers": snap.get("subscribers", []),
                "clients": snap.get("clients", []),
                "servers": snap.get("servers", []),
                "last_seen": [snap["last_seen"]] if snap.get("last_seen") else None,
                "_ghost": True,
            }

        return {
            "node_count": len(nodes_info),
            "nodes": nodes_info
        }

    def get_service_schema(self, node_id: int) -> Optional[dict[str, Any]]:
        """Return service metadata and request field schema for a node."""
        services = self.scanner.get_service_schema(node_id)
        if not services and not self.scanner.all_nodes.get(node_id, None):
            return None
        return {"node_id": node_id, "services": services}

    def get_client_info(self, node_id: int) -> Optional[dict[str, Any]]:
        """Return enriched client port info: type names and possible server nodes."""
        node = self.scanner.all_nodes.get(node_id)
        if not node or not node.has_appeared:
            return None
        clients = self.scanner.get_client_info(node_id)
        return {"node_id": node_id, "clients": clients}

    @staticmethod
    def _to_builtin_int(value: Any) -> Optional[int]:
        """Convert NumPy/PyCyphal integer-like values into plain Python ints."""
        if value is None:
            return None
        return int(value)

    @classmethod
    def _as_int_list(cls, values: Any) -> list[int]:
        """Convert array-like values into a plain list of Python ints."""
        if values is None:
            return []
        if hasattr(values, "tolist"):
            values = values.tolist()
        return [cls._to_builtin_int(getattr(item, "value", item)) for item in values]

    @staticmethod
    def _decode_name_field(value: Any) -> Optional[str]:
        """Decode DSDL string-like byte arrays into UTF-8 strings."""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if hasattr(value, "tobytes"):
            value = value.tobytes()
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _serialize_timestamps(values: Any) -> Optional[list[str]]:
        """Serialize datetime collections into ISO 8601 strings."""
        if values is None:
            return None
        result: list[str] = []
        for item in values:
            if isinstance(item, datetime.datetime):
                result.append(item.isoformat())
            else:
                result.append(str(item))
        return result

    # ------------------------------------------------------------------
    # INTERNAL LOOP
    # ------------------------------------------------------------------

    async def _telemetry_loop(self) -> None:
        """Main loop that consumes events from scanner and broadcasts them."""
        while True:
            try:
                event = await self.scanner.message_queue.get()
                self._update_state(event)
                await self._broadcast(event)
            except asyncio.CancelledError:
                logger.info("Telemetry loop cancelled")
                return
            except Exception as e:
                logger.error(f"Error in telemetry loop: {e}", exc_info=True)
                await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # STATE MANAGEMENT
    # ------------------------------------------------------------------

    def _update_state(self, event: dict[str, Any]) -> None:
        """
        Update the latest-state cache with the new event.
        
        Args:
            event: Event dict with keys: subject_id, timestamp, rate, message_type, 
                   attributes, publisher_node_id, timestamp_unix
        """
        try:
            event = self._to_json_compatible(event)
            subject_id = event.get("subject_id")
            publisher_node_id = event.get("publisher_node_id")
            
            if subject_id is None:
                logging.error(f"Event missing subject_id: {event}")
                return
            
            self.latest_by_subject[subject_id] = event
            
            if publisher_node_id is not None:
                self.latest_by_node[publisher_node_id][subject_id] = event
                logging.debug(f"Updated subject {subject_id} from node {publisher_node_id}, "
                             f"rate: {event.get('rate')}Hz")
            else:
                logging.debug(f"Updated subject {subject_id}, rate: {event.get('rate')}Hz")
        except Exception as e:
            logging.error(f"Invalid event structure: {event}, error: {e}")

    # ------------------------------------------------------------------
    # BROADCAST
    # ------------------------------------------------------------------

    async def _broadcast(self, event: dict[str, Any]) -> None:
        """
        Distribute event dict to all subscribers (already JSON-serializable from _update_state).

        Args:
            event: The event dict to broadcast.
        """
        dead_subscribers = []

        for queue, label in self.subscribers.items():
            try:
                if queue.full():
                    queue.get_nowait()
                    self.dropped[label] += 1
                queue.put_nowait(event)
            except Exception as e:
                logger.error(f"Error broadcasting to queue: {e}")
                dead_subscribers.append(queue)

        for queue in dead_subscribers:
            self.subscribers.pop(queue, None)
            logger.debug("Removed dead subscriber")

    @classmethod
    def _to_json_compatible(cls, value: Any) -> Any:
        """Recursively normalize values for aiohttp JSON responses and WebSocket sends."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value

        if isinstance(value, datetime.datetime):
            return value.isoformat()

        if isinstance(value, datetime.date):
            return value.isoformat()

        if isinstance(value, datetime.timedelta):
            return value.total_seconds()

        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", errors="replace")

        if isinstance(value, dict):
            return {key: cls._to_json_compatible(item) for key, item in value.items()}

        if isinstance(value, (list, tuple, set)):
            return [cls._to_json_compatible(item) for item in value]

        if hasattr(value, "item") and callable(value.item):
            try:
                return cls._to_json_compatible(value.item())
            except Exception:
                pass

        if hasattr(value, "tolist") and callable(value.tolist):
            try:
                return cls._to_json_compatible(value.tolist())
            except Exception:
                pass

        if hasattr(value, "tobytes") and callable(value.tobytes):
            try:
                return bytes(value.tobytes()).decode("utf-8", errors="replace")
            except Exception:
                pass

        if hasattr(value, "value"):
            try:
                return cls._to_json_compatible(value.value)
            except Exception:
                pass

        return str(value)