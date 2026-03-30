#!/usr/bin/env python3
# This class defines the database structure with automatic batch writing every second.

from pymongo import MongoClient, UpdateOne
import logging
from typing import List, Dict
import threading
import time

class Database:  
    def __init__(self, uri: str = 'mongodb://user:wicon@localhost:27018/admin', db_name: str = 'cyphal_database', batch_interval: float = 1.0):
        self.client = MongoClient(uri)
        self.db = self.client[db_name]

        self.nodes_collection = self.db['cyphal_nodes']
        self.nodes_collection.create_index("unique_id", unique=True)

        self.subjects_collection = self.db['pub_sub_subjects']
        self.subjects_collection.create_index("subject_id", unique=True)

        self.servers_collection = self.db['servers']
        self.servers_collection.create_index("service_type", unique=True) 

        # Clean up database at startup
        self.nodes_collection.delete_many({})
        self.subjects_collection.delete_many({})
        self.servers_collection.delete_many({})
        logging.debug("Database fully cleaned up at startup.")

        # The information is written in batches with batch_interval
        self.node_buffer    = {}
        self.subject_buffer = {}
        self.server_buffer = {} 
        self.lock           = threading.Lock()
        
        self.running = True
        self.batch_interval = batch_interval
        self.batch_thread = threading.Thread(target=self._batch_worker, daemon=True)
        self.batch_thread.start()

    def update_node(self, unique_id: str, node_id: int = None, node_name: str = None, uptime: str = None, 
                    publishers: List[int] = None, subscribers: List[int] = None, servers: List[int] = None, 
                    clients: List[int] = None) -> None:
        
        update_data = {"unique_id": unique_id}
        if node_id      is not None: update_data["node_id"]     = node_id
        if node_name    is not None: update_data["node_name"]   = node_name
        if uptime       is not None: update_data["uptime"]      = uptime
        if publishers   is not None: update_data["publishers"]  = publishers
        if subscribers  is not None: update_data["subscribers"] = subscribers
        if servers      is not None: update_data["servers"]     = servers
        if clients      is not None: update_data["clients"]     = clients
        
        with self.lock:
            self.node_buffer[unique_id] = UpdateOne({"unique_id": unique_id}, {"$set": update_data}, upsert=True)

    def update_subject(self, subject_id: int, timestamp: str, rate: int, message_type: str, attributes: List[Dict]) -> None:

        update_data = {"subject_id": subject_id}
        if timestamp    is not None: update_data["timestamp"]   = timestamp
        if rate         is not None: update_data["rate"]        = rate
        if message_type is not None: update_data["message_type"]= message_type
        if attributes   is not None: update_data["attributes"]  = attributes
        
        with self.lock:
            self.subject_buffer[subject_id] = UpdateOne({"subject_id": subject_id}, {"$set": update_data}, upsert=True)

    def update_server(self, service_id: int, service_type: str, node_id: int, attributes: Dict[str, str] = None) -> None:
        """
        Updates or creates a server entry in the servers_collection, mapping node_id to service_id.
        
        Args:
            service_id (int): The ID of the service.
            service_type (str): The type of the service (e.g., 'uavcan.node.GetInfo_1_0'), must be unique.
            node_id (int): The ID of the node providing the service.
            attributes (Dict[str, str], optional): Dictionary of attribute names and their types.
        """
        update_data = {
            "$set": {
                f"node_service_map.{node_id}": service_id  # Map node_id to service_id
            }
        }
        if attributes is not None:
            update_data["$set"]["attributes"] = attributes

        with self.lock:
            # Use service_type as the buffer key to ensure uniqueness
            self.server_buffer[service_type] = UpdateOne(
                {"service_type": service_type},
                update_data,
                upsert=True
            )

    def _batch_worker(self):
        while self.running:
            time.sleep(self.batch_interval)
            self.flush_buffers()
    
    def flush_buffers(self):
        with self.lock:
            if self.node_buffer:
                try:
                    self.nodes_collection.bulk_write(list(self.node_buffer.values()))
                except Exception as e:
                    logging.error(f"Node batch update failed: {str(e)}")
                self.node_buffer.clear()

            if self.subject_buffer:
                try:
                    self.subjects_collection.bulk_write(list(self.subject_buffer.values()))  # Convert dict_values to list
                except Exception as e:
                    logging.error(f"Subject batch update failed: {str(e)}")
                self.subject_buffer.clear()

            if self.server_buffer:
                try:
                    self.servers_collection.bulk_write(list(self.server_buffer.values()))  # Convert dict_values to list
                except Exception as e:
                    logging.error(f"Subject batch update failed: {str(e)}")
                self.server_buffer.clear()

    def close(self) -> None:
        self.running = False
        self.batch_thread.join()
        self.flush_buffers()
        self.client.close()
        logging.debug("Database connection closed.")
