"""Tests for NodeIdentityMap."""

from node_identity_map import NodeIdentityMap


class TestNodeIdentityMap:
    def test_register_new_mapping(self):
        m = NodeIdentityMap()
        result = m.register(5, "abcdef0123456789")
        assert result is None
        assert m.get_uid(5) == "abcdef0123456789"
        assert m.get_nid("abcdef0123456789") == 5

    def test_register_same_mapping_is_noop(self):
        m = NodeIdentityMap()
        m.register(5, "abcdef0123456789")
        result = m.register(5, "abcdef0123456789")
        assert result is None

    def test_register_migration_returns_old_node_id(self):
        m = NodeIdentityMap()
        m.register(5, "abcdef0123456789")
        result = m.register(10, "abcdef0123456789")
        assert result == 5
        assert m.get_nid("abcdef0123456789") == 10
        assert m.get_uid(10) == "abcdef0123456789"
        assert m.get_uid(5) is None

    def test_register_replacement_at_same_node_id(self):
        m = NodeIdentityMap()
        m.register(5, "aaaa")
        result = m.register(5, "bbbb")
        assert result is None
        assert m.get_uid(5) == "bbbb"
        assert m.get_nid("aaaa") is None
        assert m.get_nid("bbbb") == 5

    def test_register_ignores_all_zeros(self):
        m = NodeIdentityMap()
        result = m.register(5, "00000000000000000000000000000000")
        assert result is None
        assert m.get_uid(5) is None
        assert m.get_nid("00000000000000000000000000000000") is None

    def test_register_ignores_empty_string(self):
        m = NodeIdentityMap()
        result = m.register(5, "")
        assert result is None
        assert m.get_uid(5) is None

    def test_unregister_nid(self):
        m = NodeIdentityMap()
        m.register(5, "abcdef0123456789")
        m.unregister_nid(5)
        assert m.get_uid(5) is None
        assert m.get_nid("abcdef0123456789") is None

    def test_unregister_nid_preserves_history(self):
        m = NodeIdentityMap()
        m.register(5, "abcdef0123456789")
        m.unregister_nid(5)
        mappings = m.all_mappings()
        assert "abcdef0123456789" in mappings
        assert mappings["abcdef0123456789"]["current_node_id"] is None

    def test_migration_after_unregister(self):
        """After explicit unregister, re-registration is treated as new (no migration)."""
        m = NodeIdentityMap()
        m.register(5, "abcdef0123456789")
        m.unregister_nid(5)
        result = m.register(10, "abcdef0123456789")
        assert result is None
        assert m.get_nid("abcdef0123456789") == 10

    def test_offline_node_reappears_with_new_id(self):
        """Node goes offline (no unregister), reappears at a different node_id — triggers migration."""
        m = NodeIdentityMap()
        m.register(5, "abcdef0123456789")
        result = m.register(10, "abcdef0123456789")
        assert result == 5
        assert m.get_nid("abcdef0123456789") == 10
        assert m.get_uid(5) is None

    def test_offline_node_slot_taken_by_new_device(self):
        """Offline node's slot is taken by a different device — old uid loses its node_id."""
        m = NodeIdentityMap()
        m.register(5, "aaa")
        result = m.register(5, "bbb")
        assert result is None
        assert m.get_nid("aaa") is None
        assert m.get_nid("bbb") == 5
        mappings = m.all_mappings()
        assert mappings["aaa"]["current_node_id"] is None

    def test_migration_without_unregister(self):
        """Node gets a new node_id while old slot is still live (allocator race)."""
        m = NodeIdentityMap()
        m.register(5, "abcdef0123456789")
        result = m.register(10, "abcdef0123456789")
        assert result == 5
        assert m.get_uid(5) is None
        assert m.get_uid(10) == "abcdef0123456789"

    def test_all_mappings(self):
        m = NodeIdentityMap()
        m.register(5, "aaa")
        m.register(10, "aaa")
        m.register(20, "bbb")
        mappings = m.all_mappings()
        assert mappings["aaa"]["current_node_id"] == 10
        assert 5 in mappings["aaa"]["previous_node_ids"]
        assert mappings["bbb"]["current_node_id"] == 20
        assert mappings["bbb"]["previous_node_ids"] == []

    def test_multiple_migrations(self):
        m = NodeIdentityMap()
        m.register(5, "aaa")
        m.register(10, "aaa")
        m.register(20, "aaa")
        assert m.get_nid("aaa") == 20
        assert m.get_uid(5) is None
        assert m.get_uid(10) is None
        mappings = m.all_mappings()
        assert set(mappings["aaa"]["previous_node_ids"]) == {5, 10}

    def test_get_uid_unknown(self):
        m = NodeIdentityMap()
        assert m.get_uid(99) is None

    def test_get_nid_unknown(self):
        m = NodeIdentityMap()
        assert m.get_nid("unknown") is None

    def test_unregister_unknown_nid_is_safe(self):
        m = NodeIdentityMap()
        m.unregister_nid(99)

    def test_snapshot_roundtrip(self):
        m = NodeIdentityMap()
        m.register(5, "aaa111")
        m.register(10, "aaa111")
        m.register(20, "bbb222")
        m.set_node_name("aaa111", "my_node")

        snapshot = m.snapshot()
        assert len(snapshot) == 2

        m2 = NodeIdentityMap()
        m2.load_snapshot(snapshot)
        assert m2.get_nid("aaa111") == 10
        assert m2.get_nid("bbb222") == 20
        assert m2.get_uid(10) == "aaa111"
        assert m2.get_uid(20) == "bbb222"
        mappings = m2.all_mappings()
        assert mappings["aaa111"]["name"] == "my_node"
        assert 5 in mappings["aaa111"]["previous_node_ids"]

    def test_snapshot_preserves_last_seen(self):
        m = NodeIdentityMap()
        m.register(5, "aaa111")
        snapshot = m.snapshot()
        assert snapshot[0]["last_seen_unix"] is not None
        assert snapshot[0]["last_seen_unix"] > 0

        m2 = NodeIdentityMap()
        m2.load_snapshot(snapshot)
        mappings = m2.all_mappings()
        assert mappings["aaa111"]["last_seen"] == snapshot[0]["last_seen_unix"]

    def test_snapshot_offline_node_no_current_nid(self):
        m = NodeIdentityMap()
        m.register(5, "aaa111")
        m.unregister_nid(5)
        snapshot = m.snapshot()
        assert snapshot[0]["current_node_id"] is None

        m2 = NodeIdentityMap()
        m2.load_snapshot(snapshot)
        assert m2.get_nid("aaa111") is None
        assert m2.get_uid(5) is None

    def test_set_node_name(self):
        m = NodeIdentityMap()
        m.register(5, "aaa111")
        m.set_node_name("aaa111", "test_node")
        mappings = m.all_mappings()
        assert mappings["aaa111"]["name"] == "test_node"

    def test_set_node_name_ignores_empty(self):
        m = NodeIdentityMap()
        m.register(5, "aaa111")
        m.set_node_name("aaa111", "")
        m.set_node_name("", "name")
        mappings = m.all_mappings()
        assert mappings["aaa111"]["name"] is None

    def test_load_snapshot_then_live_registration(self):
        """Snapshot-loaded entries can be migrated by live registrations."""
        m = NodeIdentityMap()
        m.register(5, "aaa111")
        snapshot = m.snapshot()

        m2 = NodeIdentityMap()
        m2.load_snapshot(snapshot)
        result = m2.register(10, "aaa111")
        assert result == 5
        assert m2.get_nid("aaa111") == 10
        assert m2.get_uid(5) is None

    def test_detached_identities_excludes_live(self):
        m = NodeIdentityMap()
        m.register(5, "aaa111")
        m.register(10, "bbb222")
        detached = m.detached_identities({5, 10})
        assert detached == []

    def test_detached_identities_returns_displaced(self):
        """When a new device takes the same node_id, the old identity becomes detached."""
        m = NodeIdentityMap()
        m.register(5, "aaa111")
        m.set_node_name("aaa111", "old_device")
        m.register(5, "bbb222")
        detached = m.detached_identities({5})
        assert len(detached) == 1
        assert detached[0]["unique_id_hex"] == "aaa111"
        assert detached[0]["name"] == "old_device"

    def test_detached_identities_after_unregister(self):
        m = NodeIdentityMap()
        m.register(5, "aaa111")
        m.unregister_nid(5)
        detached = m.detached_identities(set())
        assert len(detached) == 1
        assert detached[0]["unique_id_hex"] == "aaa111"

    def test_load_node_data_bulk(self):
        m = NodeIdentityMap()
        m.register(5, "aaa111")
        m.register(10, "bbb222")
        data = {
            "aaa111": {"name": "node_a", "publishers": [100]},
            "bbb222": {"name": "node_b", "publishers": [200]},
        }
        m.load_node_data(data)
        assert m.get_node_snapshot("aaa111")["name"] == "node_a"
        assert m.get_node_snapshot("bbb222")["publishers"] == [200]

    def test_load_node_data_detached_uses_snapshot(self):
        m = NodeIdentityMap()
        m.register(5, "aaa111")
        m.load_node_data({"aaa111": {"name": "saved_node", "uptime": 999}})
        m.unregister_nid(5)
        detached = m.detached_identities(set())
        assert len(detached) == 1
        assert detached[0]["snapshot"]["name"] == "saved_node"
        assert detached[0]["snapshot"]["uptime"] == 999
