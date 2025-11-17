# This class provides all information about Cyphal node.

from dataclasses import dataclass, field
from typing import Dict, List, Deque, Optional

import datetime
import collections
import logging
import numpy
import numpy.typing

import pycyphal.transport
import uavcan
import pycyphal
import uavcan.node
import uavcan.node.port



@dataclass
class NodeTime:
    time: datetime.timedelta
    days: int
    hours: int
    minutes: int
    seconds: int
    microseconds: int

    def __init__(self, timedelta: datetime.timedelta, days: int, hours: int, minutes: int, seconds: int):
        self.time = timedelta
        self.days = days
        self.hours = hours
        self.minutes = minutes
        self.seconds = seconds
        self.microseconds = timedelta.microseconds

    def toString(self):
        return f"{self.days}d/{self.hours}h/{self.minutes}min/{self.seconds}s/{self.microseconds}µs"
    
@dataclass
class PublisherInfo:
    subject_id: int
    message_type: Optional[str] = None
    last_published_data: Optional[str] = None
    frequency_hz: Optional[float] = None
    last_published_time: Optional[datetime.datetime] = None
    publish_times: Deque[datetime.datetime] = collections.deque(maxlen=10) 


@dataclass
class NodeInfo:
    node_id: int
    unique_id: numpy.typing.NDArray[numpy.uint8]
    uptime: int
    has_appeared: bool
    has_disappeared: bool
    first_seen: datetime.datetime
    last_seen: Deque[datetime.datetime]
    has_responded_to_getInfo: bool
    info_response: uavcan.node.GetInfo_1_0.Response
    transfer_from: pycyphal.transport._transfer.TransferFrom
    has_published_port_list: bool
    has_been_registered_by_web_scanner: bool
    port_list: uavcan.node.port.List_1_0 
    has_registered_ports: bool 
    has_subscribers: bool
    subscriber_SubjectIDs: List[int]
    has_publishers: bool
    publisher_SubjectIDs: List[int]
    has_clients: bool
    client_ServiceIDs: List[int]
    has_servers: bool
    server_ServiceIDs: List[int]

    max_offline_time_microseconds: int = 1100000  # 1.1 seconds 
    _online_time: Optional[NodeTime] = None
    _offline_time: Optional[NodeTime] = None
    publishers_info: Dict[int, PublisherInfo] = field(default_factory=dict)

    def __init__(self, node_id: int):
        self.node_id = node_id
        self.unique_id = numpy.array([], dtype=numpy.uint8)
        self.uptime = None
        self.has_appeared = False
        self.last_seen = collections.deque(maxlen=2)
        self.has_responded_to_getInfo = False
        self.has_disappeared = False
        self.has_published_port_list = False
        self.has_been_registered_by_web_scanner = False
        self.has_registered_ports = False
        self.has_subscribers = False
        self.subscriber_SubjectIDs = []
        self.has_publishers = False 
        self.publisher_SubjectIDs = []
        self.has_clients = False
        self.client_ServiceIDs = []
        self.has_servers = False
        self.server_ServiceIDs = []
        self.publishers_info = {}

    def hasAppeared(self, first_seen: datetime.datetime):
        self.has_appeared = True
        self.first_seen = first_seen
        self.last_seen.append(first_seen)
        self.last_seen.append(first_seen)  # Initialize both entries of last_seen
        self.has_disappeared = False

    def wasSeen(self, last_seen: datetime.datetime):
        self.last_seen.append(last_seen)
        if self.has_disappeared:
            logging.info(f"Node {self.node_id} has reappeared.")
            self.first_seen = last_seen
            self.last_seen.append(last_seen)  # Reset the timer
        self.has_disappeared = False

    def setInfo(self, getInfo_response: uavcan.node.GetInfo_1_0.Response, transfer_from: pycyphal.transport._transfer.TransferFrom) -> None:
        self.has_responded_to_getInfo = True
        self.info_response = getInfo_response
        self.transfer_from = transfer_from
        self.registerUniqueID()

    def disappeared(self) -> bool:
        if not self.has_appeared:
            return False
        delta_time = datetime.datetime.now() - self.last_seen[1]
        delta_time_total_microseconds = delta_time.seconds * 1000000 + delta_time.microseconds
        if delta_time_total_microseconds >= self.max_offline_time_microseconds:
            self.has_disappeared = True
            return True
        return False

    def getOnlineTime(self) -> Optional[NodeTime]:
        if not self.has_appeared or self.disappeared():
            return None
        timedelta = self.last_seen[1] - self.first_seen
        days = timedelta.days
        hours = timedelta.seconds // 3600
        seconds_remainder = timedelta.seconds % 3600
        minutes = seconds_remainder // 60
        seconds = seconds_remainder % 60
        self._online_time = NodeTime(timedelta, days, hours, minutes, seconds)
        return self._online_time

    def getOfflineTime(self) -> Optional[NodeTime]:
        if self.disappeared():
            timedelta = datetime.datetime.now() - self.last_seen[1]
            days = timedelta.days
            hours = timedelta.seconds // 3600
            seconds_remainder = timedelta.seconds % 3600
            minutes = seconds_remainder // 60
            seconds = seconds_remainder % 60
            self._offline_time = NodeTime(timedelta, days, hours, minutes, seconds)
            return self._offline_time
        return None

    def setPortList(self, port_list: uavcan.node.port.List_1_0):
        self.has_published_port_list = True
        self.port_list = port_list
        self.registerIDs()

    def registerPublisherIDs(self) -> None:
        if self.has_published_port_list:
            # Fixed sparse list handling for publishers
            publishers = self.port_list.publishers.sparse_list
            if publishers is not None and publishers.size > 0:
                self.has_publishers = True
                for subject_id in publishers:
                    logging.info(f"Node {self.node_id} has a publisher with Subject-ID {subject_id}")
                    self.publisher_SubjectIDs.append(subject_id)
                
                    self.publishers_info[subject_id.value] = PublisherInfo(subject_id=subject_id.value)

            else:
                self.has_publishers = False
        else:
            self.has_publishers = False

    def registerSubscriberIDs(self) -> None:
        if self.has_published_port_list:
            # Fixed sparse list handling for subscribers
            subscribers = self.port_list.subscribers.sparse_list
            if subscribers is not None and subscribers.size > 0:
                self.has_subscribers = True
                for subject_id in subscribers:
                    logging.info(f"Node {self.node_id} has a subscriber with Subject-ID {subject_id}")
                    self.subscriber_SubjectIDs.append(subject_id)
            else:
                logging.info(f"Node {self.node_id} has no subscribers.")
                self.has_subscribers = False
        else:
            self.has_subscribers = False

    def registerClientIDs(self) -> None:
        if self.has_published_port_list:
            # Fixed sparse list handling for clients
            clients = self.port_list.clients.mask
            for service_id in range(0, clients.size):
                if clients[service_id]:
                    self.has_clients = True
                    logging.info(f"Node {self.node_id} has a client with Service-ID {service_id}")
                    self.client_ServiceIDs.append(service_id)
        else:
            self.has_clients = False

    def registerServerIDs(self) -> None:
        if self.has_published_port_list:
            # Fixed sparse list handling for servers
            servers = self.port_list.servers.mask
            for service_id in range(0, servers.size):
                if servers[service_id]:
                    self.has_servers = True
                    logging.info(f"Node {self.node_id} has a server with Service-ID {service_id}")
                    self.server_ServiceIDs.append(service_id)
        else:
            self.has_servers = False

    def registerUniqueID(self) -> None:
        if self.has_responded_to_getInfo:
            self.unique_id = self.info_response.unique_id

    def registerIDs(self) -> None:
        if self.has_registered_ports:
            return
        self.registerPublisherIDs()
        self.registerSubscriberIDs()
        self.registerClientIDs()
        self.registerServerIDs()
        self.has_registered_ports = True
        logging.info(f"Node {self.node_id} has registered all Subject/Service IDs.")
