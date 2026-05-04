#!/usr/bin/env python3

import os
import asyncio
import logging
import datetime
import re
import importlib
import time
import numpy as np
from pycyphal.dsdl import get_model
from pydsdl import CompositeType, Field

from typing import Any, Optional, Callable
import pycyphal
import pycyphal.application

import uavcan.node
import uavcan.node.port
import uavcan.register
import uavcan.primitive
import uavcan.diagnostic
import uavcan.pnp

from node_info import NodeInfo

class ScannerNode:
    # Constants
    REGISTER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_app.db")
    NUM_NODES = 128
    HEARTBEAT_SUBJECT_ID = 7509
    PORT_LIST_SUBJECT_ID = 7510
    PNP_SUBJECT_ID_V2 = 8165
    PNP_SUBJECT_ID_V1 = 8166
    MESSAGE_QUEUE_SIZE = 1000
    REGISTER_TIMEOUT = 2.0
    SERVICE_CALL_TIMEOUT = 5.0
    MESSAGE_RATE_WINDOW_SECONDS = 10
    MAX_SUBJECT_ID = 8191
    MAX_SERVICE_ID = 511
    STANDARD_SERVICES = {
        384: 'uavcan.register.Access_1_0',
        385: 'uavcan.register.List_1_0',
        430: 'uavcan.node.GetInfo_1_0'
    }

    def __init__(self) -> None:
        node_information = uavcan.node.GetInfo_1.Response(
            software_version=uavcan.node.Version_1(major=1, minor=0),
            name="org.dontpanic.pycyphal.utility.monitor_app",
        )

        self._node = pycyphal.application.make_node(node_information, ScannerNode.REGISTER_FILE)
        self._node.heartbeat_publisher.mode = uavcan.node.Mode_1.OPERATIONAL
        self._node.heartbeat_publisher.vendor_specific_status_code = os.getpid() % 100

        self.heartbeat_subscriber = self._node.make_subscriber(uavcan.node.Heartbeat_1_0, self.HEARTBEAT_SUBJECT_ID)
        self.heartbeat_subscriber.receive_in_background(self.heartbeat_callback)

        self.port_subscriber = self._node.make_subscriber(uavcan.node.port.List_1_0, self.PORT_LIST_SUBJECT_ID)
        self.port_subscriber.receive_in_background(self.port_callback)

        self.pnp_v2_subscriber = self._node.make_subscriber(uavcan.pnp.NodeIDAllocationData_2_0, self.PNP_SUBJECT_ID_V2)
        self.pnp_v2_subscriber.receive_in_background(self.pnp_v2_callback)

        self.pnp_v1_subscriber = self._node.make_subscriber(uavcan.pnp.NodeIDAllocationData_1_0, self.PNP_SUBJECT_ID_V1)
        self.pnp_v1_subscriber.receive_in_background(self.pnp_v1_callback)

        self.all_nodes: dict[int, NodeInfo] = {node_id: NodeInfo(node_id=node_id) for node_id in range(self.NUM_NODES)}
        self.subject_attributes: dict[int, list[str]] = {}                # Maps subject_id to list of attribute names
        self.publishers_subscribers: dict[int, pycyphal.application.Subscriber] = {}
        self.message_timestamps: dict[int, list[float]] = {}              # Maps subject_id to timestamps for rate calculation
        self.message_queue: asyncio.Queue = asyncio.Queue(maxsize=self.MESSAGE_QUEUE_SIZE)
        self.active_publishers: dict[int, set] = {}                        # Maps subject_id to set of publisher node_ids
        self.services  = []
        self.service_metadata = {}  # (node_id, service_id) -> {"namespace": str, "service_name": str}
        self.service_clients = {}   
        try:
            self._node.start()
        except Exception as e:
            logging.error(f"Failed to start PyCyphal node: {e}")
            raise


    async def update_reg_list(self, node_id: int) -> tuple[dict[int, str], dict[int, str]]:
        """
        Read node registers and return dictionaries where subject/service IDs are keys and message/service types are values.
        
        Args:
            node_id: The ID of the node to query.
        
        Returns:
            Tuple of two dictionaries: (pub_messages_dict, srv_messages_dict)
        """
        await asyncio.sleep(1)  # Allow node to stabilize
        self.register_list_client = self._node.make_client(uavcan.register.List_1_0, node_id, "register_list")
        self.register_access_client = self._node.make_client(uavcan.register.Access_1_0, node_id, "register_access")
        dsdl_pub_messages: dict[int, str] = {}
        dsdl_srv_messages: dict[int, str] = {}

        try:
            register_names = []
            index = 0

            # Step 1: Collect all register names
            while True:
                list_request = uavcan.register.List_1_0.Request(index=index)
                list_response_tuple = await asyncio.wait_for(
                    self.register_list_client.call(list_request), timeout=self.REGISTER_TIMEOUT
                )
                if not list_response_tuple or not list_response_tuple[0]:
                    logging.debug(f"No response for register list at index {index} for node {node_id}")
                    break

                list_response = list_response_tuple[0]
                register_name = list_response.name.name.tobytes().decode("utf-8")
                if not register_name:
                    logging.debug(f"No more registers found for node {node_id} at index {index}")
                    break

                register_names.append(register_name)
                index += 1

            # Step 2: Process registers
            for reg_name in register_names:
                # Process publisher registers (uavcan.pub.<port_name>.id)
                if re.search(r'\.pub\..*\.id$', reg_name):
                    access_request = uavcan.register.Access_1_0.Request(
                        name=uavcan.register.Name_1_0(name=reg_name.encode("utf-8"))
                    )
                    access_response_tuple = await asyncio.wait_for(
                        self.register_access_client.call(access_request), timeout=self.REGISTER_TIMEOUT
                    )
                    if not access_response_tuple or not access_response_tuple[0]:
                        logging.warning(f"Failed to access register '{reg_name}' for node {node_id}")
                        continue

                    access_response = access_response_tuple[0]
                    field_name, value = self._find_non_none_field(access_response.value)

                    if field_name != 'natural16':
                        logging.warning(f"Register '{reg_name}' has unexpected type '{field_name}' (expected natural16)")
                        continue

                    try:
                        subject_id = int(value[0])  # Natural16 is an array with one element
                        if not (0 <= subject_id <= self.MAX_SUBJECT_ID):
                            logging.warning(f"Invalid subject-ID {subject_id} in register '{reg_name}' for node {node_id}")
                            continue
                    except (TypeError, ValueError, IndexError) as e:
                        logging.warning(f"Invalid subject-ID in register '{reg_name}': {value}, error: {e}")
                        continue

                    type_reg_name = re.sub(r'\.id$', '.type', reg_name)
                    type_access_request = uavcan.register.Access_1_0.Request(
                        name=uavcan.register.Name_1_0(name=type_reg_name.encode("utf-8"))
                    )
                    type_access_response_tuple = await asyncio.wait_for(
                        self.register_access_client.call(type_access_request), timeout=self.REGISTER_TIMEOUT
                    )
                    if not type_access_response_tuple or not type_access_response_tuple[0]:
                        logging.warning(f"Failed to access type register '{type_reg_name}' for node {node_id}")
                        continue

                    type_access_response = type_access_response_tuple[0]
                    type_field_name, type_value = self._find_non_none_field(type_access_response.value)
                    if type_field_name != 'string':
                        logging.warning(f"Type register '{type_reg_name}' has unexpected type '{type_field_name}' (expected string)")
                        continue

                    value_str = str(type_value)
                    dots_to_underscores = re.sub(r'(?<=\d)\.(?=\d)|(?<=\w)\.(?=\d)', '_', value_str)
                    dsdl_pub_messages[subject_id] = dots_to_underscores
                    logging.debug(f"Node {node_id} subject {subject_id}: {dots_to_underscores}")

                # Process service registers (uavcan.srv.<service_id>.type)
                elif re.match(r'^uavcan\.srv\.(\d+)\.type$', reg_name):
                    service_id_match = re.match(r'^uavcan\.srv\.(\d+)\.type$', reg_name)
                    service_id = int(service_id_match.group(1))
                    
                    # Validate service ID
                    if not (0 <= service_id <= self.MAX_SERVICE_ID):
                        logging.warning(f"Invalid service ID {service_id} in register '{reg_name}' for node {node_id}")
                        continue

                    access_request = uavcan.register.Access_1_0.Request(
                        name=uavcan.register.Name_1_0(name=reg_name.encode("utf-8"))
                    )
                    access_response_tuple = await asyncio.wait_for(
                        self.register_access_client.call(access_request), timeout=5.0
                    )
                    if not access_response_tuple or not access_response_tuple[0]:
                        logging.warning(f"Failed to access service register '{reg_name}' for node {node_id}")
                        continue

                    access_response = access_response_tuple[0]
                    field_name, value = self._find_non_none_field(access_response.value)
                    if field_name != 'string':
                        logging.warning(f"Service register '{reg_name}' has unexpected type '{field_name}' (expected string)")
                        continue

                    value_str = str(value)
                    dots_to_underscores = re.sub(r'(?<=\d)\.(?=\d)|(?<=\w)\.(?=\d)', '_', value_str)
                    dsdl_srv_messages[service_id] = dots_to_underscores
                    logging.debug(f"Node {node_id} service {service_id}: {dots_to_underscores}")

            # Step 3: Check for standard services
            for standard_service_id, service_type in self.STANDARD_SERVICES.items():
                if standard_service_id not in dsdl_srv_messages:
                    dsdl_srv_messages[standard_service_id] = re.sub(r'(?<=\d)\.(?=\d)|(?<=\w)\.(?=\d)', '_', service_type)
                    logging.debug(f"Node {node_id} standard service {standard_service_id}: {dsdl_srv_messages[standard_service_id]}")

            logging.debug(f"Node {node_id} has registered messages: {dsdl_pub_messages}, services: {dsdl_srv_messages}")
            logging.debug(f"Services for node {node_id}: {dsdl_srv_messages}")
            return dsdl_pub_messages, dsdl_srv_messages

        finally:
            # Clean up clients
            if self.register_list_client:
                self.register_list_client.close()
            if self.register_access_client:
                self.register_access_client.close()

    async def add_subscriptions(self, node_id: int, dsdl_pub_messages: dict[int, str]) -> None:
        # Creates a subscription if it was not existed before.
        for subject_id, message_type in dsdl_pub_messages.items():

            if subject_id not in self.active_publishers:
                self.active_publishers[subject_id] = set()
            self.active_publishers[subject_id].add(node_id)

            # Breaks message name type into the class and message types. For example "uavcan.primitive.String_1_0" -> "uavcan.primitive" and "String_1_0"
            last_dot_index = message_type.rfind('.')
            if last_dot_index == -1:
                continue

            namespace = message_type[:last_dot_index]
            data_type = message_type[last_dot_index + 1:]

            # Imports modules with this messages to extract attributes and create subscriptions
            module              = importlib.import_module(namespace)
            data_type_class     = getattr(module, data_type)
            message_type_model  = get_model(data_type_class)

            attribute_list = [attribute.name for attribute in message_type_model.attributes if isinstance(attribute, Field)]
            self.subject_attributes[subject_id] = attribute_list

            # Just a wrapper around "_publisher_callback" to pass subject_id into "receive_in_background" method
            async def callback_with_subject_id(msg, transfer, sub_id=subject_id):
                await self._publisher_callback(msg, transfer, sub_id)
            
            if subject_id not in self.publishers_subscribers:
                subscriber = self._node.make_subscriber(data_type_class, subject_id)
                subscriber.receive_in_background(callback_with_subject_id)
                self.publishers_subscribers[subject_id] = subscriber
                logging.debug(f"Subscribed to publisher on subject {subject_id}")

    async def add_servers(self, node_id: int, dsdl_srv_messages: dict[int, str]) -> dict[int, dict[str, Any]]:
        """
        Creates clients for the services specified in dsdl_srv_messages for the given node_id.
        
        Args:
            node_id (int): The ID of the node to create service clients for.
            dsdl_srv_messages (Dict[int, str]): Dictionary mapping service IDs to service type names.
        """
        service_info_list = {}
        
        for service_id, service_type in dsdl_srv_messages.items():
            try:
                # Break service name into namespace and service type
                last_dot_index = service_type.rfind('.')
                if last_dot_index == -1:
                    logging.error(f"Invalid service type format for service_id {service_id}: {service_type}")
                    continue

                namespace = service_type[:last_dot_index]
                service_name = service_type[last_dot_index + 1:]

                # Import the service module and class
                module = importlib.import_module(namespace)
                service_class = getattr(module, service_name)

                client_key = (node_id, service_id)
                
                # Only create if not already cached
                if client_key not in self.service_clients:
                    client = self._node.make_client(service_class, node_id, service_id)
                    if not client:
                        logging.error(f"Failed to create client for service {service_type} (ID: {service_id}) on node {node_id}")
                        continue
                    self.service_clients[client_key] = client
                    logging.debug(f"Created and cached client for service {service_type} (ID: {service_id}) on node {node_id}")
                else:
                    logging.debug(f"Using cached client for service {service_type} (ID: {service_id}) on node {node_id}")

                self.service_metadata[client_key] = {
                    "namespace": namespace,
                    "service_name": service_name
                }

                if service_name in self.services:
                    logging.debug(f"Service type {service_type} already exists, adding service_id {service_id} for node {node_id}")
                    service_info_list[service_id] = {
                        "name": service_name,
                        "node_id": node_id,
                        "attributes": None
                    }
                    continue
                else:
                    self.services.append(service_name)

                service_info_list[service_id] = {
                "name": service_name,
                "node_id": node_id,
                "attributes": {}
                }
                

                for attribute in service_class.Request._MODEL_.attributes:
                    attr_name = attribute.name
                    attr_type = str(attribute.data_type)
                    service_info_list[service_id]["attributes"][attr_name] = attr_type

            except Exception as e:
                logging.error(f"Failed to create client for service {service_type} (ID: {service_id}) on node {node_id}: {e}")

        return service_info_list
    
    async def make_service_call(self, node_id: int, service_id: int, service_type: str, attributes: dict[str, dict[str, Any]]) -> str:
        """
        Makes a service call to the specified node and service with given attributes.

        Args:
        node_id (int): ID of the target node.
        service_id (int): ID of the service .
        service_type (str): Service type (e.g., 'SumService_1_0').
        attributes (Dict[str, Dict[str, Any]]): Dictionary of {attr_name: {value: Any, type: Optional[str]}}.

        Returns:
        str: String representation of the service response.

        Raises:
        ValueError: If service_type is invalid, attributes are empty/incorrect, or client is missing.
        Exception: If the service call fails.
        """
        logging.debug(f"Attempting service call: node_id={node_id}, service_id={service_id}, service_type={service_type}, attributes={attributes}")

        try:
            client_key = (node_id, service_id)
            client = self.service_clients.get(client_key)

            metadata = self.service_metadata.get(client_key)

            namespace = metadata["namespace"]
            service_name = metadata["service_name"]

            module = importlib.import_module(namespace)
            service_class = getattr(module, service_name)

            # Get the Request class
            request_class = getattr(service_class, "Request", None)
            if not request_class:
                logging.error(f"No Request class found for service {service_type}")
                raise ValueError(f"No Request class found for service {service_type}")

            # Get the cached client
            
            if not client:
                logging.error(f"No client found for service {service_id} on node {node_id}")
                raise ValueError(f"No client found for service {service_id} on node {node_id}")
            logging.debug(f"Using cached client for service {service_id} on node {node_id}")

            # Instantiate the request object
            request = request_class()

            for attr in request._MODEL_.attributes:
                logging.debug(f"Processing attribute: name={attr.name}, data_type={str(attr.data_type)}")
                
                try:
                    if isinstance(attr.data_type, CompositeType):
                        logging.debug(f"Attribute {attr.name} is a composite type: {str(attr.data_type)}")
                        
                        for key in attributes:
                            if str(key) != str(attr.name):
                                continue
                            
                            logging.debug(f"Matched attribute key: {key}")
                            
                            try:
                                # Access type and value
                                attr_data = attributes.get(key, {})
                                attr_type = attr_data.get('type')
                                attr_value = attr_data.get('value')
                                
                                if attr_type is None or attr_value is None:
                                    logging.error(f"Missing type or value for attribute {key}: type={attr_type}, value={attr_value}")
                                    raise ValueError(f"Attribute {key} missing 'type' or 'value'")
                                
                                logging.debug(f"Attribute {key}: type={attr_type}, value={attr_value}")
                                
                                # Split type into namespace and type name
                                try:
                                    attribyte_type_split = attr_type.split('.')
                                    if len(attribyte_type_split) < 3:
                                        logging.error(f"Invalid type format for attribute {key}: {attr_type}")
                                        raise ValueError(f"Invalid type format: {attr_type}")
                                    
                                    attribute_type_name = '_'.join(attribyte_type_split[-3:])
                                    attribute_type_namespace = '.'.join(attribyte_type_split[:-3])
                                    logging.debug(f"Parsed type: namespace={attribute_type_namespace}, type_name={attribute_type_name}")
                                except AttributeError as e:
                                    logging.error(f"Failed to split type {attr_type} for attribute {key}: {str(e)}")
                                    raise ValueError(f"Invalid type string {attr_type}: {str(e)}")
                                
                                # Import module and class
                                try:
                                    module = importlib.import_module(attribute_type_namespace)
                                    attribute_class = getattr(module, attribute_type_name)
                                    logging.debug(f"Imported class {attribute_type_name} from {attribute_type_namespace}")
                                except (ImportError, AttributeError) as e:
                                    logging.error(f"Failed to import {attribute_type_name} from {attribute_type_namespace}: {str(e)}")
                                    raise ValueError(f"Cannot import {attribute_type_name}: {str(e)}")
                                
                                # Create instance and set field
                                try:
                                    struct_instance = attribute_class()
                                    fields = struct_instance._MODEL_.fields
                                    if not fields:
                                        logging.error(f"No fields found in {attribute_type_name} for attribute {key}")
                                        raise ValueError(f"No fields defined in {attribute_type_name}")
                                    
                                    field_name = fields[0].name
                                    logging.debug(f"Setting field {field_name} in {attribute_type_name} to value={attr_value}")
                                    
                                    # Attempt to set the field with the value
                                    try:
                                        setattr(struct_instance, field_name, attr_value)
                                        logging.debug(f"Set {field_name}={attr_value} on {attribute_type_name} instance")
                                    except (TypeError, ValueError) as e:
                                        logging.error(f"Failed to set {field_name}={attr_value} on {attribute_type_name}: {str(e)}")
                                        raise ValueError(f"Invalid value {attr_value} for field {field_name}: {str(e)}")
                                    
                                    # Assign struct to request
                                    setattr(request, attr.name, struct_instance)
                                    logging.debug(f"Assigned {attribute_type_name} instance to request attribute {attr.name}")
                                
                                except Exception as e:
                                    logging.error(f"Failed to create or configure {attribute_type_name} for attribute {key}: {str(e)}")
                                    raise ValueError(f"Error processing composite attribute {key}: {str(e)}")
                            
                            except Exception as e:
                                logging.error(f"Error processing attribute {key} for composite type {attr.name}: {str(e)}")
                                raise ValueError(f"Error processing attribute {key}: {str(e)}")
                    
                    else:
                        logging.debug(f"Attribute {attr.name} is a primitive type: {str(attr.data_type)}")
                        
                        for key in attributes:
                            if str(key) != str(attr.name):
                                continue
                            
                            logging.debug(f"Matched attribute key: {key}")
                            
                            try:
                                # Access value
                                attr_data = attributes.get(key, {})
                                attr_value = attr_data.get('value')
                                
                                if attr_value is None:
                                    logging.error(f"Missing value for attribute {key}")
                                    raise ValueError(f"Attribute {key} missing 'value'")
                                
                                logging.debug(f"Attribute {key}: value={attr_value}")
                                
                                # Convert value for primitive type (e.g., float for saturated float16)
                                try:
                                    if str(attr.data_type) == "saturated float16":
                                        parsed_value = float(attr_value)  # PyCyphal coerces to float16
                                        logging.debug(f"Converted {attr_value} to float: {parsed_value}")
                                    else:
                                        parsed_value = attr_value  # Keep as-is for other types
                                    
                                    setattr(request, attr.name, parsed_value)
                                    logging.debug(f"Set request attribute {attr.name}={parsed_value} (type: {type(parsed_value).__name__})")
                                
                                except (TypeError, ValueError) as e:
                                    logging.error(f"Failed to set {attr.name}={attr_value} on request: {str(e)}")
                                    raise ValueError(f"Invalid value {attr_value} for attribute {attr.name}: {str(e)}")
                            
                            except Exception as e:
                                logging.error(f"Error processing attribute {key} for primitive type {attr.name}: {str(e)}")
                                raise ValueError(f"Error processing attribute {key}: {str(e)}")
                
                except Exception as e:
                    logging.error(f"Failed to process attribute {attr.name}: {str(e)}")
                    raise ValueError(f"Error processing attribute {attr.name}: {str(e)}")


            # Log request details
            logging.info(f"Sending request for {service_type} on node {node_id}, service {service_id}:")
            for attr_name in [a.name for a in request._MODEL_.attributes]:
                logging.info(f"  {attr_name}: {getattr(request, attr_name)} (type: {type(getattr(request, attr_name)).__name__})")

            # Make service call with 5-second timeout
            try:
                response_tuple = await asyncio.wait_for(client.call(request), timeout=self.SERVICE_CALL_TIMEOUT)
                logging.debug(f"Received response tuple for service {service_id}: {response_tuple}")
            except asyncio.TimeoutError:
                logging.error(f"Service call timed out for service {service_id} on node {node_id} after {self.SERVICE_CALL_TIMEOUT}s")
                raise Exception(f"Service {service_id} on node {node_id} timed out after {self.SERVICE_CALL_TIMEOUT}s")

            if response_tuple is None or response_tuple[0] is None:
                logging.error(f"Service call failed for service {service_id} on node {node_id}: No response received")
                raise Exception(f"Service {service_id} call failed or no response received")

            response = response_tuple[0]
            logging.info(f"Received response for service {service_id}: {response}")

            # Format response for SumService_1_0
            if service_type == "dontpanic.SumService_1_0":
                try:
                    response_str = f"sum: {response.sum}"
                    logging.debug(f"Formatted SumService_1_0 response: {response_str}")
                    return response_str
                except AttributeError as e:
                    logging.error(f"Failed to access response.sum for SumService_1_0: {str(e)}")
                    raise ValueError(f"Invalid response format for SumService_1_0: {str(e)}")

            # Generic response formatting
            response_str = str(response)
            logging.debug(f"Formatted response as string: {response_str}")
            return response_str

        except Exception as e:
            logging.error(f"Error making service call for service {service_id} on node {node_id}: {str(e)}")
            raise

    def _find_non_none_field(self, union_value: uavcan.register.Value_1_0) -> tuple[str | None, Any | None]:
        """
        Extract the non-none field from a uavcan.register.Value_1_0 union.
        
        Args:
            union_value: The register value union.
        
        Returns:
            Tuple of (field_name, field_value) or ('empty', None) if empty.
        """
        field_names = [
            'empty', 'string', 'unstructured', 'bit', 'integer64', 'integer32',
            'integer16', 'integer8', 'natural64', 'natural32', 'natural16',
            'natural8', 'real64', 'real32', 'real16'
        ]
        
        for field_name in field_names:
            value = getattr(union_value, field_name)
            if value is not None:
                if field_name == 'empty':
                    return field_name, None
                if field_name == 'string':
                    return field_name, value.value.tobytes().decode("utf-8", errors="ignore")
                if field_name == 'unstructured':
                    return field_name, value.value.data.tobytes().hex()
                if field_name in ['bit', 'integer64', 'integer32', 'integer16', 'integer8',
                                'natural64', 'natural32', 'natural16', 'natural8',
                                'real64', 'real32', 'real16']:
                    return field_name, value.value if hasattr(value, 'value') else value
        raise ValueError("Malformed union: no field is set in uavcan.register.Value_1_0")

    async def get_registers(self, node_id: int) -> list[dict[str, Any]]:
        """
        Fetches all registers for a given node directly using uavcan.register services.

        Args:
            node_id (int): The ID of the target node.

        Returns:
            List[Dict[str, Any]]: A list of dictionaries, each containing register details:
                - register_name (str): Name of the register.
                - value (str): String representation of the register's value.
                - type (str): Data type of the register's value (e.g., 'string', 'integer32', 'real16').
                - access (str): Access type ('read-write' if mutable, 'read-only' if immutable).

        Raises:
            ValueError: If the register list or access request fails critically.
        """
        logging.info(f"Fetching registers for node_id={node_id}")
        registers = []

        try:
            # Ensure clients are initialized for the specific node
            self.register_list_client = self._node.make_client(uavcan.register.List_1_0, node_id, "register_list")
            self.register_access_client = self._node.make_client(uavcan.register.Access_1_0, node_id, "register_access")

            index = 0
            while True:
                # Create a request for the register name at the current index
                list_request = uavcan.register.List_1_0.Request(index=index)
                logging.debug(f"Sending List_1_0 request for node_id={node_id}, index={index}")

                try:
                    # Send the list request with a timeout
                    list_response_tuple = await asyncio.wait_for(
                        self.register_list_client.call(list_request), timeout=self.REGISTER_TIMEOUT
                    )
                    if not list_response_tuple or not list_response_tuple[0]:
                        logging.debug(f"No response for List request for node_id={node_id}, index={index}, trying again")
                        try:
                            list_response_tuple = await asyncio.wait_for(
                                self.register_list_client.call(list_request), timeout=self.REGISTER_TIMEOUT
                            )
                        except asyncio.TimeoutError:
                            logging.debug(f"List request timed out after retry for node_id={node_id}, index={index}")
                            index += 1
                            continue
                        
                        if not list_response_tuple or not list_response_tuple[0]:
                            logging.debug(f"Still no response after retry for node_id={node_id}, index={index}")
                            index += 1
                            continue

                    list_response = list_response_tuple[0]
                    register_name = list_response.name.name.tobytes().decode("utf-8")

                    # Check if we've reached the end of the register list
                    if not list_response.name.name.size:
                        logging.debug(f"No more registers found for node_id={node_id} at index={index}")
                        break

                    logging.info(f"Found register '{register_name}' at index={index} for node_id={node_id}")

                    # Access the register to get its value and metadata
                    access_request = uavcan.register.Access_1_0.Request(
                        name=uavcan.register.Name_1_0(name=register_name.encode("utf-8")),
                        value=uavcan.register.Value_1_0(empty=uavcan.primitive.Empty_1_0())
                    )

                    try:
                        access_response_tuple = await asyncio.wait_for(
                            self.register_access_client.call(access_request), timeout=self.REGISTER_TIMEOUT
                        )
                        if not access_response_tuple or not access_response_tuple[0]:
                            try:
                                access_response_tuple = await asyncio.wait_for(
                                    self.register_access_client.call(access_request), timeout=2.0
                                )
                            except asyncio.TimeoutError:
                                logging.debug(f"Access request timed out after retry for register '{register_name}' on node_id={node_id}")
                                index += 1
                                continue
                            
                            if not access_response_tuple or not access_response_tuple[0]:
                                logging.debug(f"Access request failed for register '{register_name}' on node_id={node_id}")
                                index += 1
                                continue

                        access_response = access_response_tuple[0]
                        field_name, value = self._find_non_none_field(access_response.value)
                        access_type = "read-write" if access_response.mutable else "read-only"

                        # Format the register data
                        register_data = {
                            "register_name": register_name,
                            "value": str(value) if value is not None else "N/A",
                            "type": field_name if field_name else "unknown",
                            "access": access_type
                        }
                        registers.append(register_data)
                        logging.info(
                            f"Register for node_id={node_id}: name={register_name}, value={value}, "
                            f"type={field_name}, access={access_type}"
                        )

                    except asyncio.TimeoutError:
                        logging.error(f"Timeout on Access_1_0 request for register '{register_name}' on node_id={node_id}")
                    except Exception as e:
                        logging.error(f"Error accessing register '{register_name}' on node_id={node_id}: {str(e)}")

                    index += 1

                except asyncio.TimeoutError:
                    logging.error(f"Timeout on List_1_0 request for node_id={node_id}, index={index}")
                    index += 1
                    continue
                except Exception as e:
                    logging.error(f"Error fetching register list for node_id={node_id} at index={index}: {str(e)}")
                    index += 1
                    continue
            self.register_list_client.close()
            self.register_access_client.close()
            if not registers:
                logging.warning(f"No registers found for node_id={node_id}")
                return registers

            return registers

        except Exception as e:
            logging.error(f"Critical error fetching registers for node_id={node_id}: {str(e)}")
            raise ValueError(f"Failed to fetch registers for node {node_id}: {str(e)}")
        


    def get_message_rate(self, subject_id: int) -> float:
        timestamps = self.message_timestamps.get(subject_id, [])
        if len(timestamps) < 2:
            return 0.0
        time_span = timestamps[-1] - timestamps[0]
        if time_span == 0:
            return 0.0
        return len(timestamps) / time_span

    def _extract_attributes(self, msg: Any, field_names: list[str]) -> list[dict]:
        """Recursively extract attributes from a DSDL message, flattening nested composites."""
        results: list[dict] = []
        for name in field_names:
            val = getattr(msg, name, None)
            if val is None:
                continue
            self._extract_value(name, val, results)
        return results

    def _extract_value(self, name: str, val: Any, results: list[dict]) -> None:
        """Extract a single value, recursing into composite DSDL types."""
        # Byte arrays / strings
        if isinstance(val, (bytes, bytearray)):
            results.append({"attribute": name, "value": val.decode("utf-8", errors="ignore")})
        elif isinstance(val, np.ndarray):
            try:
                results.append({"attribute": name, "value": bytes(val).decode("utf-8", errors="ignore")})
            except Exception:
                results.append({"attribute": name, "value": val.tolist()})
        # Primitives
        elif isinstance(val, (str, int, float, bool, np.integer, np.floating)):
            results.append({"attribute": name, "value": self._make_json_serializable(val)})
        # Nested composite DSDL type — recurse into its fields
        else:
            try:
                nested_model = get_model(type(val))
                nested_fields = [a.name for a in nested_model.attributes if isinstance(a, Field)]
                # Single-field wrappers (e.g. enums): unwrap, keep parent name
                if len(nested_fields) == 1:
                    field_name = nested_fields[0]
                    sub_val = getattr(val, field_name, None)
                    if sub_val is not None:
                        self._extract_value(name, sub_val, results)
                        # Add unit hint from field name (skip generic "value")
                        if field_name != "value" and results and results[-1]["attribute"] == name:
                            results[-1]["unit"] = field_name
                else:
                    for field_name in nested_fields:
                        sub_val = getattr(val, field_name, None)
                        if sub_val is not None:
                            self._extract_value(f"{name}.{field_name}", sub_val, results)
            except Exception:
                results.append({"attribute": name, "value": str(val)})

    def _make_json_serializable(self, value: Any) -> Any:
        """
        Convert numpy and other types to JSON-serializable Python types.
        
        Args:
            value: The value to convert.
        
        Returns:
            JSON-serializable value (int, float, str, bool, or list).
        """
        if isinstance(value, (np.integer, np.floating)):
            return value.item()  # Convert numpy scalar to Python scalar
        if isinstance(value, np.ndarray):
            return value.tolist()  # Convert array to list
        return value

    def _track_rate(self, subject_id: int, timestamp: float) -> int:
        """Update message timestamps for rate calculation and return current rate."""
        if subject_id not in self.message_timestamps:
            self.message_timestamps[subject_id] = []
        self.message_timestamps[subject_id].append(timestamp)
        self.message_timestamps[subject_id] = [t for t in self.message_timestamps[subject_id]
                                                if t > timestamp - self.MESSAGE_RATE_WINDOW_SECONDS]
        return round(self.get_message_rate(subject_id))

    async def _queue_event(self, subject_id: int, message_type: str, attributes: list, publisher_node_id: int) -> None:
        """Build a standardized event dict and put it on the message queue."""
        timestamp = time.time()
        rate = self._track_rate(subject_id, timestamp)
        event = {
            "subject_id": subject_id,
            "timestamp": datetime.datetime.fromtimestamp(timestamp).isoformat(timespec="seconds"),
            "timestamp_unix": timestamp,
            "rate": rate,
            "message_type": message_type,
            "attributes": attributes,
            "publisher_node_id": publisher_node_id,
        }
        try:
            self.message_queue.put_nowait(event)
        except asyncio.QueueFull:
            self.message_queue.get_nowait()
            await self.message_queue.put(event)

    async def _publisher_callback(self, msg, transfer: pycyphal.transport.TransferFrom, subject_id: int) -> None:
        """
        Callback for publisher messages. Extracts attributes and queues standardized event.
        
        Event dict format:
        {
            "subject_id": int,
            "timestamp": str,           # ISO 8601 timestamp
            "timestamp_unix": float,    # Unix timestamp
            "rate": int,                # Messages per second (Hz)
            "message_type": str,        # Message class name (e.g., "Heartbeat_1_0")
            "attributes": list,         # List of {attribute, value, unit?, ...}
            "publisher_node_id": int,   # Source node ID
        }
        """
        timestamp = time.time()
        timestamp_str = datetime.datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
        publisher_node_id = transfer.source_node_id
        message_type = msg.__class__.__name__

        if subject_id not in self.message_timestamps:
            self.message_timestamps[subject_id] = []
        self.message_timestamps[subject_id].append(timestamp)
        self.message_timestamps[subject_id] = [t for t in self.message_timestamps[subject_id] 
                                                if t > timestamp - self.MESSAGE_RATE_WINDOW_SECONDS]
        rate = round(self.get_message_rate(subject_id))

        # Check if subject attributes exist before processing
        if subject_id not in self.subject_attributes:
            logging.warning(f"Subject attributes not found for subject_id {subject_id}")
            return
        
        # Extract values for each attribute (recursively handles nested DSDL types)
        attributes = self._extract_attributes(msg, self.subject_attributes[subject_id])
        
        # Create standardized event dict
        event = {
            "subject_id": subject_id,
            "timestamp": timestamp_str,
            "timestamp_unix": timestamp,
            "rate": rate,
            "message_type": message_type,
            "attributes": attributes,
            "publisher_node_id": publisher_node_id,
        }
        
        try:
            self.message_queue.put_nowait(event)
        except asyncio.QueueFull:
            logging.warning("Message queue full, dropping oldest message")
            self.message_queue.get_nowait()  # Remove oldest
            await self.message_queue.put(event)

    async def set_register(self, node_id: int, register_name: str, value: str, type: str) -> Any | None:
        """
        Sets the value of a register on a remote node using uavcan.register.Access_1_0.

        Args:
            node_id (int): The ID of the target node.
            register_name (str): The name of the register to set.
            value (str): The new value to set (will be converted to the appropriate type).
            type (str): The expected data type of the register (e.g., 'string', 'natural16', 'real32').

        Returns:
            Optional[Any]: The updated value (converted to JSON-serializable format) if successful, None if the operation fails.

        Raises:
            ValueError: If the register is read-only, the type is incompatible, or the value is invalid.
            Exception: If the service call fails due to network issues or other errors.
        """
        logging.info(f"Attempting to set register '{register_name}' on node_id={node_id} to value '{value}' (type: {type})")

        try:
            # Initialize or reuse the register access client for the specific node
            self.register_access_client = self._node.make_client(uavcan.register.Access_1_0, node_id, uavcan.register.Access_1_0._FIXED_PORT_ID_)

            # Step 1: Read the current register to check its type and mutability
            access_request = uavcan.register.Access_1_0.Request(
                name=uavcan.register.Name_1_0(name=register_name.encode("utf-8")),
                value=uavcan.register.Value_1_0(empty=uavcan.primitive.Empty_1_0())
            )

            access_response_tuple = await asyncio.wait_for(
                self.register_access_client.call(access_request), timeout=self.REGISTER_TIMEOUT
            )
            if not access_response_tuple or not access_response_tuple[0]:
                logging.error(f"Failed to access register '{register_name}' on node_id={node_id}")
                return None

            access_response = access_response_tuple[0]
            current_field_name, current_value = self._find_non_none_field(access_response.value)
            access_type = "read-write" if access_response.mutable else "read-only"

            if access_type == "read-only":
                logging.error(f"Register '{register_name}' on node_id={node_id} is read-only")
                raise ValueError(f"Register '{register_name}' is read-only")

            # Step 2: Validate the provided type against the register's actual type
            if current_field_name != type:
                logging.warning(f"Provided type '{type}' does not match register type '{current_field_name}'")
                numeric_types = {'integer64', 'integer32', 'integer16', 'integer8',
                                'natural64', 'natural32', 'natural16', 'natural8',
                                'real64', 'real32', 'real16'}
                if (type in numeric_types and current_field_name in numeric_types):
                    logging.warning(f"Type mismatch tolerated: converting '{type}' to '{current_field_name}'")
                else:
                    raise ValueError(f"Type mismatch: expected '{current_field_name}', got '{type}'")

            # Step 3: Convert the input value to the appropriate UAVCAN type
            new_value = uavcan.register.Value_1_0()
            try:
                match type:
                    case 'string':
                        new_value.string = uavcan.primitive.String_1_0(value=value.encode("utf-8"))
                    case 'unstructured':
                        new_value.unstructured = uavcan.primitive.Unstructured_1_0(value=uavcan.primitive.array.UnstructuredElement1_0(data=bytearray.fromhex(value)))
                    case 'bit':
                        new_value.bit = uavcan.primitive.array.Bit_1_0(value=bool(int(value)))
                    case 'integer64':
                        new_value.integer64 = uavcan.primitive.array.Integer64_1_0(value=int(value))
                    case 'integer32':
                        new_value.integer32 = uavcan.primitive.array.Integer32_1_0(value=int(value))
                    case 'integer16':
                        new_value.integer16 = uavcan.primitive.array.Integer16_1_0(value=int(value))
                    case 'integer8':
                        new_value.integer8 = uavcan.primitive.array.Integer8_1_0(value=int(value))
                    case 'natural64':
                        new_value.natural64 = uavcan.primitive.array.Natural64_1_0(value=int(value))
                    case 'natural32':
                        new_value.natural32 = uavcan.primitive.array.Natural32_1_0(value=int(value))
                    case 'natural16':
                        new_value.natural16 = uavcan.primitive.array.Natural16_1_0(value=int(value))
                    case 'natural8':
                        new_value.natural8 = uavcan.primitive.array.Natural8_1_0(value=int(value))
                    case 'real64':
                        new_value.real64 = uavcan.primitive.array.Real64_1_0(value=float(value))
                    case 'real32':
                        new_value.real32 = uavcan.primitive.array.Real32_1_0(value=float(value))
                    case 'real16':
                        new_value.real16 = uavcan.primitive.array.Real16_1_0(value=float(value))
                    case _:
                        logging.error(f"Unsupported register type '{type}'")
                        raise ValueError(f"Unsupported register type '{type}'")
            except ValueError as e:
                logging.error(f"Invalid value '{value}' for type '{type}': {str(e)}")
                raise ValueError(f"Invalid value '{value}' for type '{type}'")

            # Step 4: Send the update request
            access_request.value = new_value
            access_response_tuple = await asyncio.wait_for(
                self.register_access_client.call(access_request), timeout=self.REGISTER_TIMEOUT
            )
            if not access_response_tuple or not access_response_tuple[0]:
                logging.error(f"Failed to set register '{register_name}' on node_id={node_id}")
                return None

            access_response = access_response_tuple[0]
            updated_field_name, updated_value = self._find_non_none_field(access_response.value)

            # Step 5: Convert ndarray to JSON-serializable format
            if isinstance(updated_value, np.ndarray):
                updated_value = updated_value.tolist() if updated_value.size > 1 else updated_value.item()
            elif updated_value is None:
                updated_value = "N/A"

            logging.info(f"Successfully set register '{register_name}' on node_id={node_id} to '{updated_value}' (type: {updated_field_name})")
            return updated_value

        except asyncio.TimeoutError:
            logging.error(f"Timeout while setting register '{register_name}' on node_id={node_id}")
            return None
        except Exception as e:
            logging.error(f"Error setting register '{register_name}' on node_id={node_id}: {str(e)}")
            raise
        finally:
            if hasattr(self, 'register_access_client'):
                self.register_access_client.close()

    def cleanup_subscriptions(self, node_id: int) -> None:
        # Stops subscriptions if no publishers exist. It removes offline node from all lists in active_publishers 
        to_remove = []

        for subject_id, nodes in self.active_publishers.items():
            if node_id in nodes:
                nodes.remove(node_id)

            if not nodes and subject_id in self.publishers_subscribers:
                logging.info(f"Unsubscribing from subject {subject_id}, no active publishers")
                self.publishers_subscribers[subject_id].close()
                del self.publishers_subscribers[subject_id]
                to_remove.append(subject_id)

        for subject_id in to_remove:
            del self.active_publishers[subject_id]


    HEALTH_NAMES = {0: "NOMINAL", 1: "ADVISORY", 2: "CAUTION", 3: "WARNING"}
    MODE_NAMES = {0: "OPERATIONAL", 1: "INITIALIZATION", 2: "MAINTENANCE", 3: "SOFTWARE_UPDATE"}

    async def port_callback(self, msg: uavcan.node.port.List_1_0, transfer: pycyphal.transport.TransferFrom):
        node_id: int = transfer.source_node_id
        self.all_nodes[node_id].set_port_list(port_list=msg)

        # Count advertised ports for telemetry
        node = self.all_nodes[node_id]
        pub_count = len(node.publisher_SubjectIDs) if node.has_publishers else 0
        sub_count = len(node.subscriber_SubjectIDs) if node.has_subscribers else 0
        srv_count = len(node.server_ServiceIDs) if node.has_servers else 0
        clt_count = len(node.client_ServiceIDs) if node.has_clients else 0
        await self._queue_event(
            subject_id=self.PORT_LIST_SUBJECT_ID,
            message_type="List_1_0",
            attributes=[
                {"attribute": "publishers", "value": pub_count},
                {"attribute": "subscribers", "value": sub_count},
                {"attribute": "servers", "value": srv_count},
                {"attribute": "clients", "value": clt_count},
            ],
            publisher_node_id=node_id,
        )

    async def heartbeat_callback(self, msg: uavcan.node.Heartbeat_1_0, transfer: pycyphal.transport.TransferFrom):
        node_id = transfer.source_node_id
        node    = self.all_nodes[node_id]
        now     = datetime.datetime.now()

        if not node.has_appeared:
            node.mark_appeared(first_seen=now)
            logging.debug(f"Node {node_id} has appeared for the first time.")
            await self.getInfo(node_id)

        else:
            node.mark_seen(last_seen=now)
            node.uptime = msg.uptime

            # Refresh getInfo periodically (every 60s) to detect node replacements
            needs_refresh = (
                node.last_info_time is None
                or (now - node.last_info_time).total_seconds() >= 60
            )
            if needs_refresh:
                old_unique_id = ''.join(f'{byte:02x}' for byte in node.unique_id) if node.has_responded_to_getInfo else None
                await self.getInfo(node_id)
                new_unique_id = ''.join(f'{byte:02x}' for byte in node.unique_id) if node.has_responded_to_getInfo else None

                if old_unique_id and new_unique_id and old_unique_id != new_unique_id:
                    logging.info(f"Node {node_id} unique_id changed: {old_unique_id} -> {new_unique_id}. Resetting.")
                    self.all_nodes[node_id] = NodeInfo(node_id=node.node_id)
                    await self.getInfo(node_id)

        # Queue heartbeat as a telemetry event
        health_val = self._make_json_serializable(msg.health.value)
        mode_val = self._make_json_serializable(msg.mode.value)
        await self._queue_event(
            subject_id=self.HEARTBEAT_SUBJECT_ID,
            message_type="Heartbeat_1_0",
            attributes=[
                {"attribute": "uptime", "value": self._make_json_serializable(msg.uptime), "unit": "s"},
                {"attribute": "health", "value": self.HEALTH_NAMES.get(health_val, str(health_val))},
                {"attribute": "mode", "value": self.MODE_NAMES.get(mode_val, str(mode_val))},
                {"attribute": "vssc", "value": self._make_json_serializable(msg.vendor_specific_status_code)},
            ],
            publisher_node_id=node_id,
        )

    async def pnp_v2_callback(self, msg: uavcan.pnp.NodeIDAllocationData_2_0, transfer: pycyphal.transport.TransferFrom):
        raw_uid = msg.unique_id
        unique_id = bytes(raw_uid).hex() if hasattr(raw_uid, '__iter__') else str(raw_uid)
        node_id_value = self._make_json_serializable(msg.node_id.value)
        logging.info(f"PnP v2 allocation: unique_id={unique_id}, node_id={node_id_value}")
        attrs = [
            {"attribute": "unique_id", "value": unique_id},
            {"attribute": "node_id", "value": node_id_value},
        ]
        await self._queue_event(
            subject_id=self.PNP_SUBJECT_ID_V2,
            message_type="NodeIDAllocationData_2_0",
            attributes=attrs,
            publisher_node_id=transfer.source_node_id if transfer.source_node_id is not None else -1,
        )

    async def pnp_v1_callback(self, msg: uavcan.pnp.NodeIDAllocationData_1_0, transfer: pycyphal.transport.TransferFrom):
        unique_id_hash = self._make_json_serializable(msg.unique_id_hash)
        allocated = msg.allocated_node_id
        allocated_id = self._make_json_serializable(allocated[0].value) if len(allocated) > 0 else None
        logging.info(f"PnP v1 allocation: unique_id_hash={unique_id_hash}, allocated_node_id={allocated_id}")
        attrs = [{"attribute": "unique_id_hash", "value": unique_id_hash}]
        if allocated_id is not None:
            attrs.append({"attribute": "allocated_node_id", "value": allocated_id})
        await self._queue_event(
            subject_id=self.PNP_SUBJECT_ID_V1,
            message_type="NodeIDAllocationData_1_0",
            attributes=attrs,
            publisher_node_id=transfer.source_node_id if transfer.source_node_id is not None else -1,
        )

    async def getInfo(self, node_id: int) -> None:
        self.info_client    = self._node.make_client(uavcan.node.GetInfo_1, node_id)
        try:
            request         = uavcan.node.GetInfo_1.Request()
            response        = await self.info_client.call(request)
            self.all_nodes[node_id].set_info(get_info_response=response[0], transfer_from=response[1])
            self.all_nodes[node_id].last_info_time = datetime.datetime.now()
            logging.debug(f"Node {node_id} has responded to getInfo service.")
        except Exception as e:
            logging.warning(f"Node {node_id} could not provide GetInfo service. Threw {e}")
        finally:
            if hasattr(self, 'info_client') and self.info_client:
                self.info_client.close()

    def close(self) -> None:
        self._node.close()

    @property
    def node(self):
        return self._node

    @property
    def nodes(self):
        return self.all_nodes