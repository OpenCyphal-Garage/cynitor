#!/usr/bin/env python3

import os
import asyncio
import collections
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
from node_identity_map import NodeIdentityMap


def _fire_and_log(coro, context: str = "background task"):
    """Schedule a coroutine and log any exception with full traceback."""
    task = asyncio.ensure_future(coro)
    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logging.error("%s failed", context, exc_info=(type(exc), exc, exc.__traceback__))
    task.add_done_callback(_on_done)
    return task

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
    MAX_REGISTERS = 256
    INFO_REFRESH_S = 60.0   # GetInfo refresh period once a node has answered
    INFO_RETRY_S = 10.0     # retry period while it has not
    STANDARD_SERVICES = {
        384: 'uavcan.register.Access_1_0',
        385: 'uavcan.register.List_1_0',
        430: 'uavcan.node.GetInfo_1_0'
    }

    def __init__(self, register_file: Optional[str] = None) -> None:
        """``register_file`` is where the node's registers persist; the session
        puts it in the data folder (see data_dir). Defaults to REGISTER_FILE."""
        node_information = uavcan.node.GetInfo_1.Response(
            software_version=uavcan.node.Version_1(major=1, minor=0),
            name="org.dontpanic.pycyphal.utility.monitor_app",
        )

        self._node = pycyphal.application.make_node(node_information, register_file or ScannerNode.REGISTER_FILE)
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
        self.identity_map = NodeIdentityMap()
        self.subject_attributes: dict[int, list[str]] = {}                # Maps subject_id to list of attribute names
        self.publishers_subscribers: dict[int, pycyphal.application.Subscriber] = {}
        self.message_queue: asyncio.Queue = asyncio.Queue(maxsize=self.MESSAGE_QUEUE_SIZE)
        self.dropped_events = 0  # events discarded because message_queue was full
        self.active_publishers: dict[int, set] = {}                        # Maps subject_id to set of publisher node_ids
        self.services: set[str] = set()
        self.service_metadata = {}  # (node_id, service_id) -> {"namespace": str, "service_name": str}
        self.service_clients = {}
        self.node_service_types: dict[int, dict[int, str]] = {}  # node_id -> {service_id -> type_string}
        self.on_node_event: Optional[Callable] = None
        self.on_node_data_save: Optional[Callable] = None
        self._prev_health: dict[int, int] = {}
        self._prev_mode: dict[int, int] = {}
        self._prev_uptime: dict[int, int] = {}
        self._prev_ports: dict[int, dict] = {}  # node_id -> {publishers: [...], ...}
        self._info_in_flight: set[int] = set()  # node_ids with a GetInfo running
        self._rate_timestamps: dict[tuple[int, int], collections.deque] = {}  # (subject_id, node_id) -> times
        try:
            self._node.start()
        except Exception as e:
            logging.error(f"Failed to start PyCyphal node: {e}")
            raise

    def get_transport_info(self) -> dict:
        """Read-only snapshot of transport-layer protocol params and statistics.

        Reaches the shared CAN transport (``node.presentation.transport``) and
        reads pycyphal's own counters. Synchronous and cheap (attribute reads +
        a dataclass copy), so it is safe to call directly from the event loop.
        Returns ``{}`` if the transport is unavailable for any reason.
        """
        try:
            transport = self._node.presentation.transport
        except Exception:
            return {}

        info: dict = {}
        try:
            pp = transport.protocol_parameters
            info["protocol"] = {
                "mtu": pp.mtu,
                "transfer_id_modulo": pp.transfer_id_modulo,
                "max_nodes": pp.max_nodes,
                # Single-frame payload limit: 7 for Classic CAN, up to 63 for CAN FD.
                "is_fd": pp.mtu > 7,
            }
        except Exception:
            pass
        try:
            st = transport.sample_statistics()
            info["statistics"] = {
                "in_frames": st.in_frames,
                "in_frames_cyphal": st.in_frames_cyphal,
                "in_frames_cyphal_accepted": st.in_frames_cyphal_accepted,
                "in_frames_errored": st.in_frames_errored,
                "in_frames_loopback": st.in_frames_loopback,
                "out_frames": st.out_frames,
                "out_frames_timeout": st.out_frames_timeout,
                "out_frames_loopback": st.out_frames_loopback,
                "media_acceptance_filtering_efficiency": st.media_acceptance_filtering_efficiency,
                "lost_loopback_frames": st.lost_loopback_frames,
            }
            info["capture_active"] = transport.capture_active
        except Exception:
            pass
        return info

    def begin_frame_capture(self, handler) -> None:
        """Enable transport-level frame capture, routing every frame to handler.

        Sticky: pycyphal cannot stop capture without closing the transport, and
        it forces loopback + accept-all filtering. Used by FrameCaptureManager.
        """
        self._node.presentation.transport.begin_capture(handler)

    @property
    def capture_active(self) -> bool:
        try:
            return self._node.presentation.transport.capture_active
        except Exception:
            return False

    @staticmethod
    def _dsdl_type_to_module_name(dsdl_type: str) -> str:
        """
        Map a wire type name to its compiled Python module name.

        "uavcan.node.Heartbeat.1.0" becomes "uavcan.node.Heartbeat_1_0": only
        the dots before a version digit are rewritten, so namespace dots
        survive and the result still splits into (namespace, class name).
        """
        return re.sub(r'(?<=\d)\.(?=\d)|(?<=\w)\.(?=\d)', '_', dsdl_type)

    @staticmethod
    def _canonical_type_name(module_name: str) -> str:
        """The spelling the compiled type actually has, if ``module_name`` differs only in case.

        DSDL type names are case-sensitive, but some firmware advertises them
        in lowercase ("uavcan.register.access.1.0"), and the class lookup then
        fails. When the exact name does not exist and exactly one type in the
        namespace matches it ignoring case, that one is meant. Anything else --
        an unknown namespace, an exact match, no match, several -- is returned
        unchanged.
        """
        namespace, _, name = module_name.rpartition(".")
        try:
            module = importlib.import_module(namespace)
        except ImportError:
            return module_name
        if hasattr(module, name):
            return module_name
        matches = [attr for attr in dir(module) if attr.lower() == name.lower()]
        return f"{namespace}.{matches[0]}" if len(matches) == 1 else module_name

    async def _read_register(self, register_access_client, reg_name: str,
                             node_id: int) -> tuple[str | None, Any | None] | None:
        """
        Read one register by name.

        Returns the (field_name, value) pair of the register's union value.
        Raises TimeoutError when the node did not answer: a lost response is
        not a missing register, and must not be mistaken for one.
        """
        request = uavcan.register.Access_1_0.Request(
            name=uavcan.register.Name_1_0(name=reg_name.encode("utf-8"))
        )
        response_tuple = await asyncio.wait_for(
            register_access_client.call(request), timeout=self.REGISTER_TIMEOUT
        )
        if not response_tuple or not response_tuple[0]:
            raise TimeoutError(f"Node {node_id} did not answer a read of register '{reg_name}'")
        return self._find_non_none_field(response_tuple[0].value)

    async def _resolve_port_register(self, register_access_client, reg_name: str, node_id: int,
                                     max_port_id: int, label: str) -> tuple[int, str] | None:
        """
        Resolve a "uavcan.<pub|srv>.<port_name>.id" register into (port_id, dsdl_type).

        Ports are advertised by name, not by number, so the numeric ID lives in
        the register's value (a natural16) and the DSDL type name in the sibling
        ".type" register (a string). The legacy "uavcan.srv.<digits>.type" form is
        a subset of this shape and still resolves. Returns None when either
        register is missing or malformed.
        """
        id_fields = await self._read_register(register_access_client, reg_name, node_id)
        if id_fields is None:
            return None
        field_name, value = id_fields
        if field_name != 'natural16':
            logging.warning(f"Register '{reg_name}' has unexpected type '{field_name}' (expected natural16)")
            return None

        try:
            port_id = int(value[0])  # Natural16 is an array with one element
        except (TypeError, ValueError, IndexError) as e:
            logging.warning(f"Invalid {label}-ID in register '{reg_name}': {value}, error: {e}")
            return None
        if not (0 <= port_id <= max_port_id):
            logging.warning(f"Invalid {label}-ID {port_id} in register '{reg_name}' for node {node_id}")
            return None

        type_reg_name = re.sub(r'\.id$', '.type', reg_name)
        type_fields = await self._read_register(register_access_client, type_reg_name, node_id)
        if type_fields is None:
            return None
        type_field_name, type_value = type_fields
        if type_field_name != 'string':
            logging.warning(f"Type register '{type_reg_name}' has unexpected type '{type_field_name}' (expected string)")
            return None

        advertised = self._dsdl_type_to_module_name(str(type_value))
        canonical = self._canonical_type_name(advertised)
        if canonical != advertised:
            # The node's firmware should be fixed; say which register is wrong.
            logging.warning(
                f"Node {node_id} advertises '{type_value}' in '{type_reg_name}'; DSDL type names "
                f"are case-sensitive. Using {canonical}."
            )
        return port_id, canonical

    async def update_reg_list(self, node_id: int) -> tuple[dict[int, str], dict[int, str]]:
        """
        Read node registers and return dictionaries where subject/service IDs are keys and message/service types are values.
        
        Args:
            node_id: The ID of the node to query.
        
        Returns:
            Tuple of two dictionaries: (pub_messages_dict, srv_messages_dict)
        """
        await asyncio.sleep(1)  # Allow node to stabilize
        register_list_client = self._node.make_client(uavcan.register.List_1_0, node_id, "register_list")
        register_access_client = self._node.make_client(uavcan.register.Access_1_0, node_id, "register_access")
        dsdl_pub_messages: dict[int, str] = {}
        dsdl_srv_messages: dict[int, str] = {}

        try:
            register_names = []
            index = 0

            # Step 1: Collect all register names. Only an empty name ends the
            # list; no answer (after one retry) fails the whole registration,
            # so the caller retries later instead of keeping a truncated list.
            while index < self.MAX_REGISTERS:
                list_request = uavcan.register.List_1_0.Request(index=index)
                list_response_tuple = None
                for _attempt in range(2):
                    list_response_tuple = await asyncio.wait_for(
                        register_list_client.call(list_request), timeout=self.REGISTER_TIMEOUT
                    )
                    if list_response_tuple and list_response_tuple[0]:
                        break
                if not list_response_tuple or not list_response_tuple[0]:
                    raise TimeoutError(f"Node {node_id} did not answer register list request at index {index}")

                list_response = list_response_tuple[0]
                register_name = list_response.name.name.tobytes().decode("utf-8")
                if not register_name:
                    logging.debug(f"No more registers found for node {node_id} at index {index}")
                    break

                register_names.append(register_name)
                index += 1

            # Step 2: Process registers
            # Publishers and services are both advertised as
            # "uavcan.<pub|srv>.<port_name>.id" and resolve identically; only
            # the valid ID range and the destination differ.
            port_kinds = {
                "pub": (dsdl_pub_messages, self.MAX_SUBJECT_ID, "subject"),
                "srv": (dsdl_srv_messages, self.MAX_SERVICE_ID, "service"),
            }
            for reg_name in register_names:
                kind = re.search(r'\.(pub|srv)\..*\.id$', reg_name)
                if not kind:
                    continue
                target, max_port_id, label = port_kinds[kind.group(1)]
                resolved = await self._resolve_port_register(
                    register_access_client, reg_name, node_id, max_port_id, label
                )
                if resolved:
                    port_id, dsdl_type = resolved
                    target[port_id] = dsdl_type
                    logging.debug(f"Node {node_id} {label} {port_id}: {dsdl_type}")

            # Step 3: Check for standard services
            for standard_service_id, service_type in self.STANDARD_SERVICES.items():
                if standard_service_id not in dsdl_srv_messages:
                    dsdl_srv_messages[standard_service_id] = self._dsdl_type_to_module_name(service_type)
                    logging.debug(f"Node {node_id} standard service {standard_service_id}: {dsdl_srv_messages[standard_service_id]}")

            logging.debug(f"Node {node_id} has registered messages: {dsdl_pub_messages}, services: {dsdl_srv_messages}")
            logging.debug(f"Services for node {node_id}: {dsdl_srv_messages}")
            return dsdl_pub_messages, dsdl_srv_messages

        finally:
            # Clean up clients
            if register_list_client:
                register_list_client.close()
            if register_access_client:
                register_access_client.close()

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
            last_dot_index = service_type.rfind('.')
            if last_dot_index == -1:
                logging.error(f"Invalid service type format for service_id {service_id}: {service_type}")
                continue

            namespace = service_type[:last_dot_index]
            service_name = service_type[last_dot_index + 1:]
            client_key = (node_id, service_id)

            try:
                # Import the service module and class
                module = importlib.import_module(namespace)
                service_class = getattr(module, service_name)

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
                    "service_name": service_name,
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
                    self.services.add(service_name)

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
                if client_key not in self.service_metadata:
                    self.service_metadata[client_key] = {
                        "namespace": namespace,
                        "service_name": service_name,
                        "unavailable": True,
                    }

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

            # Get the cached client first — the request must be built from the
            # SAME class the client was created with. Re-importing the module here
            # can yield a different class object (e.g. after a custom-DSDL
            # recompile drops the module from sys.modules), which makes
            # client.call() reject the request with a confusing "expected X, got X"
            # identity mismatch. Sourcing Request from client.dtype avoids that.
            if not client:
                logging.error(f"No client found for service {service_id} on node {node_id}")
                raise ValueError(f"No client found for service {service_id} on node {node_id}")
            logging.debug(f"Using cached client for service {service_id} on node {node_id}")

            request_class = getattr(client.dtype, "Request", None)
            if not request_class:
                logging.error(f"No Request class found for service {service_type}")
                raise ValueError(f"No Request class found for service {service_type}")

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
                                    attribute_type_split = attr_type.split('.')
                                    if len(attribute_type_split) < 3:
                                        logging.error(f"Invalid type format for attribute {key}: {attr_type}")
                                        raise ValueError(f"Invalid type format: {attr_type}")
                                    
                                    attribute_type_name = '_'.join(attribute_type_split[-3:])
                                    attribute_type_namespace = '.'.join(attribute_type_split[:-3])
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

            response_str = str(response)
            logging.debug(f"Formatted response as string: {response_str}")
            return response_str

        except Exception as e:
            logging.error(f"Error making service call for service {service_id} on node {node_id}: {str(e)}")
            raise

    def get_service_schema(self, node_id: int) -> list[dict[str, Any]]:
        """Return structured schema for all services on a node."""
        node = self.all_nodes.get(node_id)
        if not node or not node.has_appeared:
            return []

        services = []
        node_types = self.node_service_types.get(node_id, {})
        for sid in node.server_ServiceIDs:
            sid_int = int(sid)
            meta = self.service_metadata.get((node_id, sid_int))
            if not meta:
                type_str = node_types.get(sid_int) or self.STANDARD_SERVICES.get(sid_int)
                if type_str:
                    dot = type_str.rfind('.')
                    ns = type_str[:dot] if dot != -1 else None
                    sn = type_str[dot + 1:] if dot != -1 else type_str
                    try:
                        mod = importlib.import_module(ns)
                        svc_cls = getattr(mod, sn)
                        fields = []
                        for attr in svc_cls.Request._MODEL_.attributes:
                            fields.append(self._describe_field(attr))
                        services.append({
                            "service_id": sid_int,
                            "name": sn, "namespace": ns,
                            "full_type": type_str,
                            "callable": True,
                            "request_fields": fields,
                        })
                        continue
                    except Exception as e:
                        logging.warning(f"Cannot import {type_str} for service {sid_int} on node {node_id}: {e}")
                services.append({
                    "service_id": sid_int,
                    "name": None,
                    "namespace": None,
                    "full_type": type_str,
                    "callable": False,
                    "request_fields": None,
                })
                continue

            namespace = meta.get("namespace")
            service_name = meta.get("service_name")
            full_type = f"{namespace}.{service_name}" if namespace and service_name else None
            is_unavailable = meta.get("unavailable", False)

            if is_unavailable:
                services.append({
                    "service_id": sid_int,
                    "name": service_name,
                    "namespace": namespace,
                    "full_type": full_type,
                    "callable": False,
                    "request_fields": None,
                })
                continue

            fields = []
            try:
                module = importlib.import_module(namespace)
                service_class = getattr(module, service_name)
                for attr in service_class.Request._MODEL_.attributes:
                    fields.append(self._describe_field(attr))
            except Exception as e:
                logging.warning(f"Cannot introspect request fields for {full_type}: {e}")

            services.append({
                "service_id": sid_int,
                "name": service_name,
                "namespace": namespace,
                "full_type": full_type,
                "callable": True,
                "request_fields": fields,
            })

        return services

    def get_client_info(self, node_id: int) -> list[dict[str, Any]]:
        """Return enriched info for client ports: type name and possible server nodes."""
        node = self.all_nodes.get(node_id)
        if not node or not node.has_appeared or not node.has_clients:
            return []

        type_map = self.node_service_types.get(node_id, {})
        results = []
        for cid in node.client_ServiceIDs:
            cid_int = int(cid)
            type_str = type_map.get(cid_int)
            server_nodes = []
            for nid, n in self.all_nodes.items():
                if nid == node_id or not n.has_appeared or n.has_disappeared:
                    continue
                if n.has_servers and cid_int in [int(s) for s in n.server_ServiceIDs]:
                    server_nodes.append(nid)
            results.append({
                "service_id": cid_int,
                "full_type": type_str,
                "server_nodes": sorted(server_nodes),
            })
        return results

    @staticmethod
    def _parse_array_value(value_str: str) -> list[str]:
        """Parse a value string that may be a scalar or array: '1200', '[1200]', '[1000000 4000000]', '[1, 2, 3]'."""
        s = value_str.strip()
        if s.startswith('[') and s.endswith(']'):
            s = s[1:-1].strip()
        if not s:
            return []
        parts = [p.strip() for p in re.split(r'[,\s]+', s) if p.strip()]
        return parts

    @staticmethod
    def _format_register_value(value) -> str:
        """Format a register value for display — scalars as plain values, arrays with brackets."""
        if value is None:
            return "N/A"
        if isinstance(value, np.ndarray):
            if value.size == 1:
                return str(value.item())
            return ', '.join(str(v) for v in value.tolist())
        return str(value)

    def _describe_field(self, attr) -> dict[str, Any]:
        """Build a JSON-safe field descriptor from a pydsdl attribute."""
        type_str = str(attr.data_type)
        is_composite = isinstance(attr.data_type, CompositeType)
        desc = {
            "name": attr.name,
            "type": type_str,
            "kind": "composite" if is_composite else "primitive",
        }
        if is_composite:
            try:
                sub_fields = []
                for sub_attr in attr.data_type.attributes:
                    sub_fields.append({
                        "name": sub_attr.name,
                        "type": str(sub_attr.data_type),
                        "kind": "composite" if isinstance(sub_attr.data_type, CompositeType) else "primitive",
                    })
                desc["fields"] = sub_fields
            except Exception:
                desc["fields"] = []
        return desc

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
            register_list_client = self._node.make_client(uavcan.register.List_1_0, node_id, "register_list")
            register_access_client = self._node.make_client(uavcan.register.Access_1_0, node_id, "register_access")

            index = 0
            while index < self.MAX_REGISTERS:
                # Create a request for the register name at the current index
                list_request = uavcan.register.List_1_0.Request(index=index)
                logging.debug(f"Sending List_1_0 request for node_id={node_id}, index={index}")

                try:
                    # Send the list request with a timeout
                    list_response_tuple = await asyncio.wait_for(
                        register_list_client.call(list_request), timeout=self.REGISTER_TIMEOUT
                    )
                    if not list_response_tuple or not list_response_tuple[0]:
                        logging.debug(f"No response for List request for node_id={node_id}, index={index}, trying again")
                        try:
                            list_response_tuple = await asyncio.wait_for(
                                register_list_client.call(list_request), timeout=self.REGISTER_TIMEOUT
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
                            register_access_client.call(access_request), timeout=self.REGISTER_TIMEOUT
                        )
                        if not access_response_tuple or not access_response_tuple[0]:
                            try:
                                access_response_tuple = await asyncio.wait_for(
                                    register_access_client.call(access_request), timeout=2.0
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
                            "value": self._format_register_value(value),
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
            if not registers:
                logging.warning(f"No registers found for node_id={node_id}")
            return registers

        except Exception as e:
            logging.error(f"Critical error fetching registers for node_id={node_id}: {str(e)}")
            raise ValueError(f"Failed to fetch registers for node {node_id}: {str(e)}")
        finally:
            register_list_client.close()
            register_access_client.close()


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

    def _windowed_rate(self, times: collections.deque, now: float) -> float:
        """Messages per second in ``times`` over the rate window ending at ``now``."""
        cutoff = now - self.MESSAGE_RATE_WINDOW_SECONDS
        while times and times[0] <= cutoff:
            times.popleft()
        if len(times) < 2 or times[-1] == times[0]:
            return 0.0
        return (len(times) - 1) / (times[-1] - times[0])

    def _track_rate(self, subject_id: int, node_id: int, timestamp: float) -> tuple[float, float]:
        """Record one message and return (this publisher's rate, the whole subject's rate).

        Rates are kept per publisher: several nodes publish the same subject
        (every node publishes Heartbeat), and a per-subject rate would credit
        each of them with all the others' traffic.
        """
        times = self._rate_timestamps.setdefault((subject_id, node_id), collections.deque())
        times.append(timestamp)
        publisher_rate = self._windowed_rate(times, timestamp)
        subject_rate = sum(
            self._windowed_rate(other, timestamp)
            for (sid, _nid), other in self._rate_timestamps.items() if sid == subject_id
        )
        return round(publisher_rate, 1), round(subject_rate, 1)

    @staticmethod
    def _transfer_times(transfer) -> tuple[float, float]:
        """(wall-clock, monotonic) receive time of ``transfer`` in seconds.

        pycyphal stamps a frame when the driver receives it (SocketCAN: in the
        kernel), which is earlier and steadier than the moment our callback
        runs. Falls back to now when no transfer is given.
        """
        stamp = getattr(transfer, "timestamp", None)
        try:
            return float(stamp.system), float(stamp.monotonic)
        except (AttributeError, TypeError, ValueError):
            return time.time(), time.monotonic()

    @staticmethod
    def _transfer_payload_bytes(transfer) -> Optional[int]:
        """Best-effort size of a received transfer's serialized payload, in bytes."""
        payload = getattr(transfer, "fragmented_payload", None)
        if payload is None:
            return None
        try:
            return sum(len(fragment) for fragment in payload)
        except (TypeError, AttributeError):
            return None

    async def _queue_event(self, subject_id: int, message_type: str, attributes: list,
                           publisher_node_id: int, payload_bytes: Optional[int] = None,
                           transfer=None) -> None:
        """Build a standardized event dict and put it on the message queue."""
        timestamp, monotonic = self._transfer_times(transfer)
        rate, subject_rate = self._track_rate(subject_id, publisher_node_id, monotonic)
        event = {
            "subject_id": subject_id,
            "timestamp": datetime.datetime.fromtimestamp(timestamp).isoformat(timespec="seconds"),
            "timestamp_unix": timestamp,
            "rate": rate,
            "subject_rate": subject_rate,
            "message_type": message_type,
            "attributes": attributes,
            "publisher_node_id": publisher_node_id,
            "unique_id": self._get_node_unique_id_hex(publisher_node_id) or self.identity_map.get_uid(publisher_node_id),
            "payload_bytes": payload_bytes,
        }
        try:
            self.message_queue.put_nowait(event)
        except asyncio.QueueFull:
            self.message_queue.get_nowait()
            self.dropped_events += 1
            await self.message_queue.put(event)

    async def _publisher_callback(self, msg, transfer: pycyphal.transport.TransferFrom, subject_id: int) -> None:
        """Callback for publisher messages. Extracts attributes and queues standardized event."""
        if subject_id not in self.subject_attributes:
            logging.warning(f"Subject attributes not found for subject_id {subject_id}")
            return

        attributes = self._extract_attributes(msg, self.subject_attributes[subject_id])
        await self._queue_event(
            subject_id=subject_id,
            message_type=msg.__class__.__name__,
            attributes=attributes,
            publisher_node_id=transfer.source_node_id,
            payload_bytes=self._transfer_payload_bytes(transfer),
            transfer=transfer,
        )

    async def set_register(self, node_id: int, register_name: str, value: str, reg_type: str) -> Any | None:
        """
        Sets the value of a register on a remote node using uavcan.register.Access_1_0.

        Args:
            node_id (int): The ID of the target node.
            register_name (str): The name of the register to set.
            value (str): The new value to set (will be converted to the appropriate type).
            reg_type (str): The expected data type of the register (e.g., 'string', 'natural16', 'real32').

        Returns:
            Optional[Any]: The updated value (converted to JSON-serializable format) if successful, None if the operation fails.

        Raises:
            ValueError: If the register is read-only, the type is incompatible, or the value is invalid.
            Exception: If the service call fails due to network issues or other errors.
        """
        logging.info(f"Attempting to set register '{register_name}' on node_id={node_id} to value '{value}' (type: {reg_type})")

        try:
            # Initialize or reuse the register access client for the specific node
            register_access_client = self._node.make_client(uavcan.register.Access_1_0, node_id, uavcan.register.Access_1_0._FIXED_PORT_ID_)

            # Step 1: Read the current register to check its type and mutability
            access_request = uavcan.register.Access_1_0.Request(
                name=uavcan.register.Name_1_0(name=register_name.encode("utf-8")),
                value=uavcan.register.Value_1_0(empty=uavcan.primitive.Empty_1_0())
            )

            access_response_tuple = await asyncio.wait_for(
                register_access_client.call(access_request), timeout=self.REGISTER_TIMEOUT
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
            if current_field_name != reg_type:
                logging.warning(f"Provided type '{reg_type}' does not match register type '{current_field_name}'")
                numeric_types = {'integer64', 'integer32', 'integer16', 'integer8',
                                'natural64', 'natural32', 'natural16', 'natural8',
                                'real64', 'real32', 'real16'}
                if (reg_type in numeric_types and current_field_name in numeric_types):
                    logging.warning(f"Type mismatch tolerated: converting '{reg_type}' to '{current_field_name}'")
                else:
                    raise ValueError(f"Type mismatch: expected '{current_field_name}', got '{reg_type}'")

            # Step 3: Range-check numeric values before UAVCAN conversion
            _int_ranges = {
                'natural8': (0, 255), 'natural16': (0, 65535),
                'natural32': (0, 4294967295), 'natural64': (0, 2**64 - 1),
                'integer8': (-128, 127), 'integer16': (-32768, 32767),
                'integer32': (-2147483648, 2147483647), 'integer64': (-(2**63), 2**63 - 1),
            }
            _real_ranges = {
                'real16': (-65504, 65504),
                'real32': (-3.4028235e+38, 3.4028235e+38),
            }
            if reg_type in _int_ranges:
                lo, hi = _int_ranges[reg_type]
                for v in self._parse_array_value(value):
                    n = int(v)
                    if n < lo or n > hi:
                        raise ValueError(f"Value {n} out of range for {reg_type} ({lo}–{hi})")
            elif reg_type in _real_ranges:
                lo, hi = _real_ranges[reg_type]
                for v in self._parse_array_value(value):
                    n = float(v)
                    if n < lo or n > hi:
                        raise ValueError(f"Value {n} out of range for {reg_type}")

            # Step 4: Convert the input value to the appropriate UAVCAN type
            new_value = uavcan.register.Value_1_0()
            try:
                match reg_type:
                    case 'string':
                        new_value.string = uavcan.primitive.String_1_0(value=value.encode("utf-8"))
                    case 'unstructured':
                        new_value.unstructured = uavcan.primitive.Unstructured_1_0(value=uavcan.primitive.array.UnstructuredElement1_0(data=bytearray.fromhex(value)))
                    case 'bit':
                        parsed = self._parse_array_value(value)
                        valid_bool = {'true': True, '1': True, 'false': False, '0': False}
                        bools = []
                        for v in parsed:
                            key = v.lower()
                            if key not in valid_bool:
                                raise ValueError(f"Invalid boolean: '{v}' — use True/False or 1/0")
                            bools.append(valid_bool[key])
                        new_value.bit = uavcan.primitive.array.Bit_1_0(value=bools)
                    case 'integer64':
                        new_value.integer64 = uavcan.primitive.array.Integer64_1_0(value=[int(v) for v in self._parse_array_value(value)])
                    case 'integer32':
                        new_value.integer32 = uavcan.primitive.array.Integer32_1_0(value=[int(v) for v in self._parse_array_value(value)])
                    case 'integer16':
                        new_value.integer16 = uavcan.primitive.array.Integer16_1_0(value=[int(v) for v in self._parse_array_value(value)])
                    case 'integer8':
                        new_value.integer8 = uavcan.primitive.array.Integer8_1_0(value=[int(v) for v in self._parse_array_value(value)])
                    case 'natural64':
                        new_value.natural64 = uavcan.primitive.array.Natural64_1_0(value=[int(v) for v in self._parse_array_value(value)])
                    case 'natural32':
                        new_value.natural32 = uavcan.primitive.array.Natural32_1_0(value=[int(v) for v in self._parse_array_value(value)])
                    case 'natural16':
                        new_value.natural16 = uavcan.primitive.array.Natural16_1_0(value=[int(v) for v in self._parse_array_value(value)])
                    case 'natural8':
                        new_value.natural8 = uavcan.primitive.array.Natural8_1_0(value=[int(v) for v in self._parse_array_value(value)])
                    case 'real64':
                        new_value.real64 = uavcan.primitive.array.Real64_1_0(value=[float(v) for v in self._parse_array_value(value)])
                    case 'real32':
                        new_value.real32 = uavcan.primitive.array.Real32_1_0(value=[float(v) for v in self._parse_array_value(value)])
                    case 'real16':
                        new_value.real16 = uavcan.primitive.array.Real16_1_0(value=[float(v) for v in self._parse_array_value(value)])
                    case _:
                        logging.error(f"Unsupported register type '{reg_type}'")
                        raise ValueError(f"Unsupported register type '{reg_type}'")
            except ValueError as e:
                logging.error(f"Invalid value '{value}' for type '{reg_type}': {str(e)}")
                raise ValueError(f"Invalid value '{value}' for type '{reg_type}'")

            # Step 4: Send the update request
            access_request.value = new_value
            access_response_tuple = await asyncio.wait_for(
                register_access_client.call(access_request), timeout=self.REGISTER_TIMEOUT
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
            register_access_client.close()

    def cleanup_subscriptions(self, node_id: int) -> None:
        self._snapshot_node_for_identity(node_id)
        self._prev_ports.pop(node_id, None)
        self._prev_health.pop(node_id, None)
        self._prev_mode.pop(node_id, None)
        self._prev_uptime.pop(node_id, None)
        self._emit_node_event(node_id, "disappeared")
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

        # Release per-node service state. Each entry in service_clients holds a
        # pycyphal Client object — if we don't close them here, they live until
        # the backend restarts (one-way ratchet during long debug sessions
        # that cycle many short-lived nodes). _migrate_node_state still closes
        # any clients that survive into an identity migration; once cleanup has
        # run on the old slot, that path is a no-op and the new node gets
        # fresh clients via add_servers — same end state, no stale references.
        stale_service_keys = [k for k in self.service_clients if k[0] == node_id]
        for key in stale_service_keys:
            client = self.service_clients.pop(key, None)
            if client is not None:
                try:
                    client.close()
                except Exception as e:
                    logging.warning(f"Failed to close service client {key}: {e}")
        for key in [k for k in self.service_metadata if k[0] == node_id]:
            self.service_metadata.pop(key, None)
        self.node_service_types.pop(node_id, None)


    HEALTH_NAMES = {0: "NOMINAL", 1: "ADVISORY", 2: "CAUTION", 3: "WARNING"}
    MODE_NAMES = {0: "OPERATIONAL", 1: "INITIALIZATION", 2: "MAINTENANCE", 3: "SOFTWARE_UPDATE"}

    def _get_node_unique_id_hex(self, node_id: int) -> Optional[str]:
        node = self.all_nodes.get(node_id)
        if node and node.has_responded_to_getInfo and node.unique_id.size > 0:
            uid = ''.join(f'{byte:02x}' for byte in node.unique_id)
            if any(c != '0' for c in uid):
                return uid
        return None

    def _emit_node_event(self, node_id: int, event_type: str, detail: Optional[dict] = None) -> None:
        if self.on_node_event:
            uid = self._get_node_unique_id_hex(node_id) or self.identity_map.get_uid(node_id)
            _fire_and_log(self.on_node_event(node_id, event_type, detail, unique_id=uid), f"node_event({event_type})")

    async def port_callback(self, msg: uavcan.node.port.List_1_0, transfer: pycyphal.transport.TransferFrom):
        node_id: int = transfer.source_node_id

        # Snapshot ports before update for change detection
        node = self.all_nodes[node_id]
        def _port_ids(ids):
            return sorted(self._make_json_serializable(s.value) if hasattr(s, 'value') else int(s) for s in ids)

        old_ports = {
            "publishers": _port_ids(node.publisher_SubjectIDs) if node.has_publishers else [],
            "subscribers": _port_ids(node.subscriber_SubjectIDs) if node.has_subscribers else [],
            "servers": _port_ids(node.server_ServiceIDs) if node.has_servers else [],
            "clients": _port_ids(node.client_ServiceIDs) if node.has_clients else [],
        }

        self.all_nodes[node_id].set_port_list(port_list=msg)

        # Detect port changes
        new_ports = {
            "publishers": _port_ids(node.publisher_SubjectIDs) if node.has_publishers else [],
            "subscribers": _port_ids(node.subscriber_SubjectIDs) if node.has_subscribers else [],
            "servers": _port_ids(node.server_ServiceIDs) if node.has_servers else [],
            "clients": _port_ids(node.client_ServiceIDs) if node.has_clients else [],
        }
        prev = self._prev_ports.get(node_id)
        if prev is not None and new_ports != prev:
            self._emit_node_event(node_id, "port_change", {"old": prev, "new": new_ports})
        self._prev_ports[node_id] = new_ports

        # Count advertised ports for telemetry
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
            transfer=transfer,
        )

    async def heartbeat_callback(self, msg: uavcan.node.Heartbeat_1_0, transfer: pycyphal.transport.TransferFrom):
        node_id = transfer.source_node_id
        node    = self.all_nodes[node_id]
        now     = datetime.datetime.now()

        if not node.has_appeared:
            node.mark_appeared(first_seen=now)
            logging.debug(f"Node {node_id} has appeared for the first time.")
            self._emit_node_event(node_id, "first_seen")
            self._schedule_info_refresh(node_id, was_disappeared=False)

        else:
            was_disappeared = node.has_disappeared
            node.mark_seen(last_seen=now)

            if was_disappeared:
                self._prev_ports.pop(node_id, None)

            # Restart detection: uptime jumped backward
            prev_uptime = self._prev_uptime.get(node_id)
            new_uptime = self._make_json_serializable(msg.uptime)
            if prev_uptime is not None and new_uptime < prev_uptime and prev_uptime > 5:
                self._emit_node_event(node_id, "restart_suspected", {
                    "old_uptime": prev_uptime, "new_uptime": new_uptime,
                })

            node.uptime = msg.uptime

            # Refresh getInfo: immediately on reappearance, otherwise periodically
            scheduled = (was_disappeared or self._info_due(node, now)) and \
                self._schedule_info_refresh(node_id, was_disappeared)
            if was_disappeared and not scheduled:
                self._emit_node_event(node_id, "reappeared")

        # Track health/mode changes
        health_val = self._make_json_serializable(msg.health.value)
        mode_val = self._make_json_serializable(msg.mode.value)

        prev_health = self._prev_health.get(node_id)
        if prev_health is not None and health_val != prev_health:
            self._emit_node_event(node_id, "health_change", {
                "old": self.HEALTH_NAMES.get(prev_health, str(prev_health)),
                "new": self.HEALTH_NAMES.get(health_val, str(health_val)),
            })
        self._prev_health[node_id] = health_val

        prev_mode = self._prev_mode.get(node_id)
        if prev_mode is not None and mode_val != prev_mode:
            self._emit_node_event(node_id, "mode_change", {
                "old": self.MODE_NAMES.get(prev_mode, str(prev_mode)),
                "new": self.MODE_NAMES.get(mode_val, str(mode_val)),
            })
        self._prev_mode[node_id] = mode_val

        self._prev_uptime[node_id] = self._make_json_serializable(msg.uptime)

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
            transfer=transfer,
        )

    async def pnp_v2_callback(self, msg: uavcan.pnp.NodeIDAllocationData_2_0, transfer: pycyphal.transport.TransferFrom):
        raw_uid = msg.unique_id
        unique_id = bytes(raw_uid).hex() if hasattr(raw_uid, '__iter__') else str(raw_uid)
        node_id_value = self._make_json_serializable(msg.node_id.value)
        logging.info(f"PnP v2 allocation: unique_id={unique_id}, node_id={node_id_value}")

        if isinstance(node_id_value, int) and 0 <= node_id_value < self.NUM_NODES:
            old_node_id = self.identity_map.register(node_id_value, unique_id)
            if old_node_id is not None:
                self._migrate_node_state(old_node_id, node_id_value, unique_id)

        attrs = [
            {"attribute": "unique_id", "value": unique_id},
            {"attribute": "node_id", "value": node_id_value},
        ]
        await self._queue_event(
            subject_id=self.PNP_SUBJECT_ID_V2,
            message_type="NodeIDAllocationData_2_0",
            attributes=attrs,
            publisher_node_id=transfer.source_node_id if transfer.source_node_id is not None else -1,
            transfer=transfer,
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
            transfer=transfer,
        )

    def _snapshot_node_for_identity(self, node_id: int) -> None:
        """Save last known state of a node into the identity map before it gets displaced."""
        try:
            uid = self.identity_map.get_uid(node_id)
            if not uid:
                return
            node = self.all_nodes.get(node_id)
            if not node or not node.has_appeared or not node.has_responded_to_getInfo:
                return
            info = getattr(node, "info_response", None)

            def _ids(attr):
                items = getattr(node, attr, [])
                return sorted(self._make_json_serializable(s.value) if hasattr(s, 'value') else int(s) for s in items)

            name = None
            if info:
                n = info.name
                if hasattr(n, 'tobytes'):
                    n = n.tobytes().decode('utf-8', errors='replace')
                name = str(n) if n else None

            snapshot = {
                "unique_id": [int(b) for b in node.unique_id] if hasattr(node.unique_id, '__iter__') else [],
                "name": name or self.identity_map._node_names.get(uid),
                "software_version": {
                    "major": self._make_json_serializable(info.software_version.major),
                    "minor": self._make_json_serializable(info.software_version.minor),
                } if info else None,
                "publishers": _ids("publisher_SubjectIDs") if getattr(node, 'has_publishers', False) else [],
                "subscribers": _ids("subscriber_SubjectIDs") if getattr(node, 'has_subscribers', False) else [],
                "servers": _ids("server_ServiceIDs") if getattr(node, 'has_servers', False) else [],
                "clients": _ids("client_ServiceIDs") if getattr(node, 'has_clients', False) else [],
                "uptime": self._make_json_serializable(node.uptime) if getattr(node, 'uptime', None) else None,
                "last_seen": node.last_seen[-1].isoformat() if getattr(node, 'last_seen', None) and len(node.last_seen) else None,
            }
            self.identity_map.save_node_snapshot(uid, snapshot)
            if self.on_node_data_save:
                _fire_and_log(self.on_node_data_save(uid, snapshot), "node_data_save")
        except Exception as e:
            logging.warning(f"Failed to snapshot node {node_id}: {e}")

    def _migrate_node_state(self, old_node_id: int, new_node_id: int, unique_id_hex: str) -> None:
        """Transfer accumulated state from old node_id slot to new one after re-allocation."""
        logging.info(f"Migrating node state: node_id {old_node_id} -> {new_node_id} (unique_id={unique_id_hex})")

        for subject_id, publishers in self.active_publishers.items():
            if old_node_id in publishers:
                publishers.discard(old_node_id)
                publishers.add(new_node_id)

        old_service_types = self.node_service_types.pop(old_node_id, None)
        if old_service_types:
            self.node_service_types[new_node_id] = old_service_types

        old_keys = [k for k in self.service_metadata if k[0] == old_node_id]
        for old_key in old_keys:
            service_id = old_key[1]
            self.service_metadata[(new_node_id, service_id)] = self.service_metadata.pop(old_key)
            client = self.service_clients.pop(old_key, None)
            if client:
                client.close()

        for tracker in (self._prev_health, self._prev_mode, self._prev_uptime):
            val = tracker.pop(old_node_id, None)
            if val is not None:
                tracker[new_node_id] = val

        prev_ports = self._prev_ports.pop(old_node_id, None)
        if prev_ports is not None:
            self._prev_ports[new_node_id] = prev_ports

        self.all_nodes[old_node_id] = NodeInfo(node_id=old_node_id)

        self._emit_node_event(new_node_id, "node_id_migration", {
            "old_node_id": old_node_id,
            "new_node_id": new_node_id,
            "unique_id": unique_id_hex,
        })

    def _info_due(self, node: NodeInfo, now: datetime.datetime) -> bool:
        """Whether a node's GetInfo should be sent again: periodically once it
        has answered, sooner while it has not."""
        if node.last_info_attempt is None:
            return True
        interval = self.INFO_REFRESH_S if node.has_responded_to_getInfo else self.INFO_RETRY_S
        return (now - node.last_info_attempt).total_seconds() >= interval

    def _schedule_info_refresh(self, node_id: int, was_disappeared: bool) -> bool:
        """Start a GetInfo refresh in the background; False if one is already running.

        GetInfo can take up to the response timeout. Awaiting it inside the
        heartbeat callback would hold up every other node's heartbeats behind it.
        """
        if node_id in self._info_in_flight:
            return False
        self._info_in_flight.add(node_id)
        self.all_nodes[node_id].last_info_attempt = datetime.datetime.now()
        _fire_and_log(self._refresh_info(node_id, was_disappeared), f"getInfo({node_id})")
        return True

    async def _refresh_info(self, node_id: int, was_disappeared: bool) -> None:
        """Run GetInfo and handle a changed identity behind the same node-ID."""
        try:
            node = self.all_nodes[node_id]
            old_unique_id = ''.join(f'{byte:02x}' for byte in node.unique_id) if node.has_responded_to_getInfo else None
            await self.getInfo(node_id)
            new_unique_id = ''.join(f'{byte:02x}' for byte in node.unique_id) if node.has_responded_to_getInfo else None

            is_replacement = old_unique_id and new_unique_id and old_unique_id != new_unique_id
            if is_replacement:
                known_nid = self.identity_map.get_nid(new_unique_id)
                if known_nid is None or known_nid == node_id:
                    logging.info(f"Node {node_id} identity changed: {old_unique_id} -> {new_unique_id}. Resetting node state.")
                    self._prev_ports.pop(node_id, None)
                    self._prev_health.pop(node_id, None)
                    self._prev_mode.pop(node_id, None)
                    self._prev_uptime.pop(node_id, None)
                    self.all_nodes[node_id] = NodeInfo(node_id=node.node_id)
                    await self.getInfo(node_id)
            elif was_disappeared:
                self._emit_node_event(node_id, "reappeared")
        finally:
            self._info_in_flight.discard(node_id)

    async def getInfo(self, node_id: int) -> None:
        info_client    = self._node.make_client(uavcan.node.GetInfo_1, node_id)
        try:
            request         = uavcan.node.GetInfo_1.Request()
            response        = await info_client.call(request)
            self._snapshot_node_for_identity(node_id)
            self.all_nodes[node_id].set_info(get_info_response=response[0], transfer_from=response[1])
            self.all_nodes[node_id].last_info_time = datetime.datetime.now()
            logging.debug(f"Node {node_id} has responded to getInfo service.")

            uid_hex = self._get_node_unique_id_hex(node_id)
            if uid_hex:
                displaced_uid = self.identity_map.get_uid(node_id)
                if displaced_uid and displaced_uid != uid_hex and self.on_node_event:
                    _fire_and_log(self.on_node_event(node_id, "lost_node_id", {"node_id": node_id}, unique_id=displaced_uid), "node_event(lost_node_id)")
                prev_nid = self.identity_map.get_nid(uid_hex)
                old_node_id = self.identity_map.register(node_id, uid_hex)
                if prev_nid != node_id:
                    self._emit_node_event(node_id, "got_node_id", {"node_id": node_id})
                if old_node_id is not None:
                    self._snapshot_node_for_identity(old_node_id)
                    self._migrate_node_state(old_node_id, node_id, uid_hex)
                self._snapshot_node_for_identity(node_id)
                name = response[0].name
                if hasattr(name, 'tobytes'):
                    name = name.tobytes().decode('utf-8', errors='replace')
                if name:
                    self.identity_map.set_node_name(uid_hex, str(name))
        except Exception as e:
            logging.warning(f"Node {node_id} could not provide GetInfo service. Threw {e}")
        finally:
            info_client.close()

    def close(self) -> None:
        self._node.close()

    @property
    def node(self):
        return self._node

    @property
    def nodes(self):
        return self.all_nodes