import logging
import time
from typing import Optional


class NodeIdentityMap:
    """Bidirectional mapping between unique_id (stable hardware identity) and node_id (ephemeral network address)."""

    def __init__(self) -> None:
        self._uid_to_nid: dict[str, int] = {}
        self._nid_to_uid: dict[int, str] = {}
        self._uid_history: dict[str, list[int]] = {}
        self._last_seen: dict[str, float] = {}
        self._node_names: dict[str, str] = {}
        self._node_snapshots: dict[str, dict] = {}

    def register(self, node_id: int, unique_id_hex: str) -> Optional[int]:
        """Register a node_id <-> unique_id mapping.

        Returns the previous node_id if this unique_id was already known
        under a different node_id (signals migration needed), or None otherwise.
        """
        if not unique_id_hex or all(c == '0' for c in unique_id_hex):
            return None

        prev_uid = self._nid_to_uid.get(node_id)
        if prev_uid == unique_id_hex:
            return None

        old_node_id = self._uid_to_nid.get(unique_id_hex)

        # Clear stale reverse entry if this node_id previously held a different uid
        if prev_uid is not None and prev_uid != unique_id_hex:
            if self._uid_to_nid.get(prev_uid) == node_id:
                del self._uid_to_nid[prev_uid]

        # Clear stale forward entry if this uid previously pointed to a different node_id
        if old_node_id is not None and old_node_id != node_id:
            if self._nid_to_uid.get(old_node_id) == unique_id_hex:
                del self._nid_to_uid[old_node_id]

        self._uid_to_nid[unique_id_hex] = node_id
        self._nid_to_uid[node_id] = unique_id_hex
        self._last_seen[unique_id_hex] = time.time()

        if unique_id_hex not in self._uid_history:
            self._uid_history[unique_id_hex] = []
        if node_id not in self._uid_history[unique_id_hex]:
            self._uid_history[unique_id_hex].append(node_id)

        if old_node_id is not None and old_node_id != node_id:
            logging.info(f"Identity migration: unique_id={unique_id_hex} moved from node_id={old_node_id} to node_id={node_id}")
            return old_node_id

        return None

    def get_uid(self, node_id: int) -> Optional[str]:
        return self._nid_to_uid.get(node_id)

    def get_nid(self, unique_id_hex: str) -> Optional[int]:
        return self._uid_to_nid.get(unique_id_hex)

    def unregister_nid(self, node_id: int) -> None:
        """Remove node_id side of the mapping, but keep the uid entry for future recognition."""
        uid = self._nid_to_uid.pop(node_id, None)
        if uid and self._uid_to_nid.get(uid) == node_id:
            del self._uid_to_nid[uid]

    def remove_identity(self, unique_id_hex: str) -> None:
        """Completely remove a unique_id from all maps."""
        nid = self._uid_to_nid.pop(unique_id_hex, None)
        if nid is not None and self._nid_to_uid.get(nid) == unique_id_hex:
            del self._nid_to_uid[nid]
        self._uid_history.pop(unique_id_hex, None)
        self._last_seen.pop(unique_id_hex, None)
        self._node_names.pop(unique_id_hex, None)
        self._node_snapshots.pop(unique_id_hex, None)

    def set_node_name(self, unique_id_hex: str, name: str) -> None:
        if unique_id_hex and name:
            self._node_names[unique_id_hex] = name

    def save_node_snapshot(self, unique_id_hex: str, snapshot: dict) -> None:
        if unique_id_hex:
            self._node_snapshots[unique_id_hex] = snapshot

    def get_node_snapshot(self, unique_id_hex: str) -> Optional[dict]:
        return self._node_snapshots.get(unique_id_hex)

    def all_mappings(self) -> dict[str, dict]:
        """Return full mapping state for API exposure."""
        result = {}
        for uid, history in self._uid_history.items():
            current_nid = self._uid_to_nid.get(uid)
            result[uid] = {
                "current_node_id": current_nid,
                "previous_node_ids": [nid for nid in history if nid != current_nid],
                "last_seen": self._last_seen.get(uid),
                "name": self._node_names.get(uid),
            }
        return result

    def detached_identities(self, live_node_ids: set[int]) -> list[dict]:
        """Return identities not currently represented in the live node set."""
        result = []
        for uid, history in self._uid_history.items():
            current_nid = self._uid_to_nid.get(uid)
            if current_nid is not None and current_nid in live_node_ids:
                continue
            entry = {
                "unique_id_hex": uid,
                "name": self._node_names.get(uid),
                "last_seen": self._last_seen.get(uid),
                "previous_node_ids": history,
            }
            snap = self._node_snapshots.get(uid)
            if snap:
                entry["snapshot"] = snap
            result.append(entry)
        return result

    def snapshot(self) -> list[dict]:
        """Return a list of records suitable for persistence."""
        records = []
        for uid, history in self._uid_history.items():
            records.append({
                "unique_id": uid,
                "current_node_id": self._uid_to_nid.get(uid),
                "last_seen_unix": self._last_seen.get(uid),
                "node_name": self._node_names.get(uid),
                "previous_node_ids": history,
            })
        return records

    def load_snapshot(self, records: list[dict]) -> None:
        """Restore state from persisted records. Called before any live registrations."""
        for rec in records:
            uid = rec["unique_id"]
            nid = rec.get("current_node_id")
            self._uid_history[uid] = rec.get("previous_node_ids", [])
            if rec.get("last_seen_unix"):
                self._last_seen[uid] = rec["last_seen_unix"]
            if rec.get("node_name"):
                self._node_names[uid] = rec["node_name"]
            if nid is not None:
                self._uid_to_nid[uid] = nid
                self._nid_to_uid[nid] = uid
        logging.info(f"Identity map loaded {len(records)} entries from persistence")

    def load_node_data(self, data: dict[str, dict]) -> None:
        """Bulk-load persisted node snapshots (unique_id -> snapshot dict)."""
        for uid, snap in data.items():
            self._node_snapshots[uid] = snap
        if data:
            logging.info(f"Loaded {len(data)} node data snapshots from database")
