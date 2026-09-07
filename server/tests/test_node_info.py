"""Tests for NodeInfo class — lifecycle, state transitions, and timing."""

import datetime
import time
import collections
from unittest.mock import MagicMock, patch

import numpy
import pytest

from node_info import NodeInfo, NodeTime  # uavcan.* is stubbed in conftest.py


class TestNodeInfoInit:
    def test_defaults(self):
        node = NodeInfo(node_id=42)
        assert node.node_id == 42
        assert node.has_appeared is False
        assert node.has_disappeared is False
        assert node.has_responded_to_getInfo is False
        assert node.uptime is None
        assert node.unique_id.size == 0
        assert node.publisher_SubjectIDs == []
        assert node.subscriber_SubjectIDs == []
        assert node.client_ServiceIDs == []
        assert node.server_ServiceIDs == []
        assert node.publishers_info == {}


class TestMarkAppeared:
    def test_mark_appeared(self):
        node = NodeInfo(node_id=1)
        now = datetime.datetime.now()
        node.mark_appeared(now)
        assert node.has_appeared is True
        assert node.has_disappeared is False
        assert node.first_seen == now
        assert len(node.last_seen) == 2
        assert node.last_seen[0] == now
        assert node.last_seen[1] == now

    def test_mark_appeared_sets_mono_time(self):
        node = NodeInfo(node_id=1)
        before = time.monotonic()
        node.mark_appeared(datetime.datetime.now())
        after = time.monotonic()
        assert before <= node._last_seen_mono <= after


class TestMarkSeen:
    def test_mark_seen_updates_last_seen(self):
        node = NodeInfo(node_id=1)
        t0 = datetime.datetime.now()
        node.mark_appeared(t0)
        t1 = t0 + datetime.timedelta(seconds=1)
        node.mark_seen(t1)
        assert node.last_seen[-1] == t1

    def test_mark_seen_reappear(self):
        node = NodeInfo(node_id=1)
        t0 = datetime.datetime.now()
        node.mark_appeared(t0)
        node.has_disappeared = True
        t1 = t0 + datetime.timedelta(seconds=10)
        node.mark_seen(t1)
        assert node.has_disappeared is False
        assert node.first_seen == t1

    def test_mark_seen_deque_maxlen(self):
        node = NodeInfo(node_id=1)
        t0 = datetime.datetime.now()
        node.mark_appeared(t0)
        for i in range(10):
            node.mark_seen(t0 + datetime.timedelta(seconds=i + 1))
        assert len(node.last_seen) == 2


class TestCheckDisappeared:
    def test_not_appeared_returns_false(self):
        node = NodeInfo(node_id=1)
        assert node.check_disappeared() is False

    def test_recently_seen_returns_false(self):
        node = NodeInfo(node_id=1)
        node.mark_appeared(datetime.datetime.now())
        assert node.check_disappeared() is False

    def test_stale_returns_true(self):
        node = NodeInfo(node_id=1)
        node.mark_appeared(datetime.datetime.now())
        node._last_seen_mono = time.monotonic() - 5.0
        assert node.check_disappeared() is True
        assert node.has_disappeared is True

    def test_threshold_boundary(self):
        node = NodeInfo(node_id=1)
        node.mark_appeared(datetime.datetime.now())
        node._last_seen_mono = time.monotonic() - (NodeInfo.OFFLINE_THRESHOLD_S - 0.1)
        assert node.check_disappeared() is False


class TestOnlineOfflineTime:
    def test_online_time_not_appeared(self):
        node = NodeInfo(node_id=1)
        assert node.get_online_time() is None

    def test_online_time_disappeared(self):
        node = NodeInfo(node_id=1)
        node.mark_appeared(datetime.datetime.now())
        node._last_seen_mono = time.monotonic() - 10.0
        assert node.get_online_time() is None

    def test_online_time_active(self):
        node = NodeInfo(node_id=1)
        t0 = datetime.datetime.now()
        node.mark_appeared(t0)
        t1 = t0 + datetime.timedelta(hours=1, minutes=23, seconds=45)
        node.mark_seen(t1)
        result = node.get_online_time()
        assert result is not None
        assert result.hours == 1
        assert result.minutes == 23
        assert result.seconds == 45

    def test_offline_time_not_disappeared(self):
        node = NodeInfo(node_id=1)
        node.mark_appeared(datetime.datetime.now())
        assert node.get_offline_time() is None

    def test_offline_time_disappeared(self):
        node = NodeInfo(node_id=1)
        t0 = datetime.datetime.now() - datetime.timedelta(seconds=30)
        node.mark_appeared(t0)
        node.mark_seen(t0)
        node._last_seen_mono = time.monotonic() - 10.0
        result = node.get_offline_time()
        assert result is not None
        assert result.time.total_seconds() >= 10


class TestSetInfo:
    def test_set_info_marks_responded(self):
        node = NodeInfo(node_id=1)
        mock_response = MagicMock()
        mock_response.unique_id = numpy.array([1, 2, 3], dtype=numpy.uint8)
        mock_transfer = MagicMock()
        node.set_info(mock_response, mock_transfer)
        assert node.has_responded_to_getInfo is True
        assert node.info_response == mock_response
        assert node.transfer_from == mock_transfer
        assert numpy.array_equal(node.unique_id, numpy.array([1, 2, 3], dtype=numpy.uint8))


class TestSetPortList:
    def _make_port_list(self, pub_ids=None, sub_ids=None, server_ids=None, client_ids=None):
        port_list = MagicMock()
        pubs = MagicMock()
        if pub_ids:
            pubs.sparse_list = numpy.array(pub_ids)
            pubs.sparse_list.size = len(pub_ids)
            items = []
            for pid in pub_ids:
                item = MagicMock()
                item.value = pid
                item.__int__ = lambda self, v=pid: v
                items.append(item)
            pubs.sparse_list = items
            pubs.sparse_list = MagicMock()
            pubs.sparse_list.__iter__ = lambda self: iter(items)
            pubs.sparse_list.size = len(items)
        else:
            pubs.sparse_list = MagicMock()
            pubs.sparse_list.size = 0
        port_list.publishers = pubs

        subs = MagicMock()
        if sub_ids:
            items = []
            for sid in sub_ids:
                item = MagicMock()
                item.value = sid
                items.append(item)
            subs.sparse_list = MagicMock()
            subs.sparse_list.__iter__ = lambda self: iter(items)
            subs.sparse_list.size = len(items)
        else:
            subs.sparse_list = MagicMock()
            subs.sparse_list.size = 0
        port_list.subscribers = subs

        servers_mask = numpy.zeros(512, dtype=bool)
        if server_ids:
            for sid in server_ids:
                servers_mask[sid] = True
        port_list.servers.mask = servers_mask

        clients_mask = numpy.zeros(512, dtype=bool)
        if client_ids:
            for cid in client_ids:
                clients_mask[cid] = True
        port_list.clients.mask = clients_mask

        return port_list

    def test_set_port_list_registers_ids(self):
        node = NodeInfo(node_id=1)
        port_list = self._make_port_list(server_ids=[100, 200])
        node.set_port_list(port_list)
        assert node.has_published_port_list is True
        assert node.has_registered_ports is True
        assert node.has_servers is True
        assert 100 in node.server_ServiceIDs
        assert 200 in node.server_ServiceIDs

    def test_set_port_list_clears_previous(self):
        node = NodeInfo(node_id=1)
        port_list1 = self._make_port_list(server_ids=[100])
        node.set_port_list(port_list1)
        assert len(node.server_ServiceIDs) == 1
        port_list2 = self._make_port_list(server_ids=[200, 300])
        node.set_port_list(port_list2)
        assert 100 not in node.server_ServiceIDs
        assert 200 in node.server_ServiceIDs
        assert 300 in node.server_ServiceIDs

    def test_empty_ports(self):
        node = NodeInfo(node_id=1)
        port_list = self._make_port_list()
        node.set_port_list(port_list)
        assert node.has_publishers is False
        assert node.has_subscribers is False
        assert node.has_servers is False
        assert node.has_clients is False


class TestNodeTime:
    def test_str(self):
        td = datetime.timedelta(days=1, hours=2, minutes=3, seconds=4)
        nt = NodeTime(td, 1, 2, 3, 4)
        s = str(nt)
        assert "1d" in s
        assert "2h" in s
        assert "3min" in s
        assert "4s" in s
