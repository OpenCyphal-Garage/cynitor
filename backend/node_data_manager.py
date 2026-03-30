#!/usr/bin/env python3
# This class defines how the data is written to the database.

import asyncio
import logging
import pycyphal.application

from typing import List, Dict
from typing import Union
from collections import deque

from database import Database
from node_info import NodeInfo

class NodeDataManager:
    def __init__(self, node: pycyphal.application.Node, all_nodes: List, message_queue: asyncio.Queue, batch_interval: float = 1.0) -> None:
        self._node          = node
        self._running       = False

        self.all_nodes      = all_nodes
        self.message_queue  = message_queue
        self.db             = Database(batch_interval=batch_interval)

    def format_uptime(self, uptime_seconds: int) -> str:
        # Formats uptime from total seconds to a more readable '0d 0h 1m 47s'.
        days = uptime_seconds // (24 * 3600)
        uptime_seconds %= (24 * 3600)
        hours = uptime_seconds // 3600
        uptime_seconds %= 3600
        minutes = uptime_seconds // 60
        seconds = uptime_seconds % 60
        return f"{days}d {hours}h {minutes}m {seconds}s"

    def update_node_uptime(self, node_info: NodeInfo) -> None:
        # Updates only the uptime field in the database for an existing node
        unique_id       = ''.join(f'{byte:02x}' for byte in node_info.unique_id) if node_info.has_responded_to_getInfo else "Unknown"

        time_offline    = node_info.getOfflineTime()
        time_online     = node_info.getOnlineTime()
        if time_offline:
            uptime = "Offline"
        else:
            uptime = node_info.uptime if time_online else "N/A"
            if uptime != "N/A":
                uptime = self.format_uptime(uptime)

        self.db.update_node(
            unique_id   = unique_id,
            uptime      = uptime
        )

    def update_node_id(self, node_info: NodeInfo, node_id: Union[int, str] = "not assigned") -> None:
        # Updates only the node id field in the database for an existing node. Takes both strings and ints as input. 
        unique_id       = ''.join(f'{byte:02x}' for byte in node_info.unique_id) if node_info.has_responded_to_getInfo else "Unknown"

        self.db.update_node(
            unique_id   = unique_id,
            node_id     = node_id
        )

    def update_node_info(self, node_info: NodeInfo) -> None:
        # Updates the full node information in the database.
        unique_id       = ''.join(f'{byte:02x}' for byte in node_info.unique_id) if node_info.has_responded_to_getInfo else "Unknown"
        node_name       = node_info.info_response.name.tobytes().decode("utf-8") if node_info.has_responded_to_getInfo else "Unknown"
        node_id         = node_info.node_id
        publishers      = [subject_id.value for subject_id in node_info.publisher_SubjectIDs]
        subscribers     = [subject_id.value for subject_id in node_info.subscriber_SubjectIDs]
        servers         = node_info.server_ServiceIDs
        clients         = node_info.client_ServiceIDs

        self.db.update_node(
            unique_id   = unique_id,
            node_id     = node_id,
            node_name   = node_name,
            publishers  = publishers,
            subscribers = subscribers,
            servers     = servers,
            clients     = clients
        )

    async def update_subject_info(self) -> None:
        while self._running:
            # Queue size is used as a performance metric
            queue_sizes = deque(maxlen=20)
            max_queue_threshold = 100

            try:
                subject_id, timestamp, rate, msg_class, attributes = await self.message_queue.get()
                message_type = f"{msg_class.__module__}.{msg_class.__name__}"

                self.db.update_subject(subject_id, timestamp, rate, message_type, attributes)
                self.message_queue.task_done()
                logging.debug(f"Updated subject {subject_id} in database with type {message_type}")

                # Track queue size with moving average
                current_size = self.message_queue.qsize()
                queue_sizes.append(current_size)

                # Calculate moving average
                if queue_sizes:  # Ensure we have at least one value
                    moving_avg = sum(queue_sizes) / len(queue_sizes)
                    logging.debug(f"Current queue size: {current_size}, Moving average: {moving_avg:.2f}")

                    # Analyze trends based on moving average
                    if moving_avg > max_queue_threshold:
                        logging.error(f"Queue moving average exceeded threshold: {moving_avg:.2f} > {max_queue_threshold}")
                    elif moving_avg > max_queue_threshold * 0.75:
                        logging.warning(f"Queue moving average approaching threshold: {moving_avg:.2f}")

                    # Optional: Check for sustained growth
                    if len(queue_sizes) == queue_sizes.maxlen:  # Window is full
                        first_half = list(queue_sizes)[:queue_sizes.maxlen // 2]
                        second_half = list(queue_sizes)[queue_sizes.maxlen // 2:]
                        avg_first = sum(first_half) / len(first_half)
                        avg_second = sum(second_half) / len(second_half)
                        if avg_second > avg_first:
                            logging.warning(f"Queue showing sustained growth: {avg_first:.2f} -> {avg_second:.2f}")

            except Exception as e:
                logging.error(f"Error in database update: {e}")
                await asyncio.sleep(1)  # Prevent tight loop on error

    def update_service_info(self, node_id: int, service_info_list: Dict) -> None:
        """
        Updates service information in the database using service_info_list.
        
        Args:
            node_id (int): The ID of the node providing the services.
            service_info_list (Dict): Dictionary mapping service IDs to their info (name, service_type, and attributes).
        """
        for service_id, service_info in service_info_list.items():
            try:
                # Extract service_type and attributes from service_info
                service_type = service_info.get("name")
                if not service_type:
                    logging.error(f"No service_type found for service_id {service_id} in service_info_list")
                    continue

                attributes = service_info.get("attributes", {})

                # Call Database.update_server to store the service info
                self.db.update_server(
                    service_id=service_id,
                    service_type=service_type,
                    node_id=node_id,
                    attributes=attributes
                )
                logging.debug(f"Updated service {service_id} with type {service_type} for node {node_id} in database")

            except Exception as e:
                logging.error(f"Failed to update service {service_id} for node {node_id} in database: {e}")

    async def update_database(self, node_id: int, dsdl_messages: Dict[int, str]) -> None:
        # Full node information update
        node_info = self.all_nodes[node_id]
        self.update_node_info(node_info)
        self.update_node_uptime(node_info)
        await self.update_subject_info(node_id, dsdl_messages)

    def start(self) -> None:
        if not self._running:
            self._running = True
            asyncio.create_task(self.update_subject_info())
            logging.debug("Database updater task started.")

    def close(self) -> None:
        self._running = False
        self.db.close()