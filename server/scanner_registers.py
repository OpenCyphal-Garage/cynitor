"""The scanner's dealings with a node's registers (uavcan.register.List and Access).

Three things are done with them: the ports a node names in its registers are
read when it is registered (update_reg_list), all of them are listed for the
dashboard's Registers tab (get_registers), and one is written (set_register).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Optional

import numpy as np

import uavcan.primitive
import uavcan.register


class RegisterNotApplied(Exception):
    """A register write the node answered with another value than the one sent: it kept its own."""


class ScannerRegisters:
    """The register part of ScannerNode, which has the pycyphal node (``_node``),
    the limits (REGISTER_TIMEOUT, MAX_REGISTERS, ...) and the type-name helpers these use."""

    # Set by ScannerNode, declared here for the type checker.
    _node: Any
    REGISTER_TIMEOUT: float
    MAX_REGISTERS: int
    MAX_UNANSWERED_INDEXES: int
    MAX_SUBJECT_ID: int
    MAX_SERVICE_ID: int
    UNSET_PORT_ID: int
    STANDARD_SERVICES: dict[int, str]
    _dsdl_type_to_module_name: Callable[[str], str]
    _canonical_type_name: Callable[[str], str]

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
            port_id = int(value[0])  # type: ignore[index]  # Natural16 is an array with one element
        except (TypeError, ValueError, IndexError) as e:
            logging.warning(f"Invalid {label}-ID in register '{reg_name}': {value}, error: {e}")
            return None
        if port_id == self.UNSET_PORT_ID:  # a port the node has but does not use, as the standard allows
            logging.debug(f"Register '{reg_name}' of node {node_id} is unset: the port is inactive")
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
            if value.dtype == bool:  # true/false, as DSDL and yakut write them, not Python's True/False
                return ', '.join('true' if v else 'false' for v in value.tolist())
            if value.size == 1:
                return str(value.item())
            return ', '.join(str(v) for v in value.tolist())
        return str(value)

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

            # A register that is not named or not read, asked for twice, is
            # passed over: the rest of the list is still worth showing. Two
            # in a row unanswered means the node has stopped answering.
            misses = 0
            for index in range(self.MAX_REGISTERS):
                if misses == self.MAX_UNANSWERED_INDEXES:
                    break
                try:
                    register_name = await self._register_name_at(register_list_client, node_id, index)
                    if register_name is None:
                        misses += 1
                        continue
                    misses = 0
                    if not register_name:
                        logging.debug(f"No more registers found for node_id={node_id} at index={index}")
                        break
                    logging.info(f"Found register '{register_name}' at index={index} for node_id={node_id}")
                    register_data = await self._read_register_entry(register_access_client, node_id, register_name)
                    if register_data is not None:
                        registers.append(register_data)
                except asyncio.TimeoutError:
                    misses += 1
                    logging.error(f"Timeout on List_1_0 request for node_id={node_id}, index={index}")
                except Exception as e:
                    logging.error(f"Error fetching register list for node_id={node_id} at index={index}: {str(e)}")
            if misses == self.MAX_UNANSWERED_INDEXES and not registers:
                raise TimeoutError(f"Node {node_id} does not answer register requests")
            if not registers:
                logging.warning(f"No registers found for node_id={node_id}")
            return registers

        except TimeoutError:
            raise
        except Exception as e:
            logging.error(f"Critical error fetching registers for node_id={node_id}: {str(e)}")
            raise ValueError(f"Failed to fetch registers for node {node_id}: {str(e)}")
        finally:
            register_list_client.close()
            register_access_client.close()

    async def _register_name_at(self, register_list_client, node_id: int, index: int) -> Optional[str]:
        """The name of the node's register at ``index``, "" past its last one; None if it did not say, asked twice."""
        list_request = uavcan.register.List_1_0.Request(index=index)
        logging.debug(f"Sending List_1_0 request for node_id={node_id}, index={index}")
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
                return None
            if not list_response_tuple or not list_response_tuple[0]:
                logging.debug(f"Still no response after retry for node_id={node_id}, index={index}")
                return None
        return list_response_tuple[0].name.name.tobytes().decode("utf-8")

    async def _read_register_entry(self, register_access_client, node_id: int,
                                   register_name: str) -> Optional[dict[str, Any]]:
        """The register as get_registers lists it; None if it could not be read in two tries, which is logged."""
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
                        register_access_client.call(access_request), timeout=self.REGISTER_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logging.debug(f"Access request timed out after retry for register '{register_name}' on node_id={node_id}")
                    return None
                if not access_response_tuple or not access_response_tuple[0]:
                    logging.debug(f"Access request failed for register '{register_name}' on node_id={node_id}")
                    return None

            access_response = access_response_tuple[0]
            field_name, value = self._find_non_none_field(access_response.value)
            access_type = "read-write" if access_response.mutable else "read-only"
            logging.info(
                f"Register for node_id={node_id}: name={register_name}, value={value}, "
                f"type={field_name}, access={access_type}"
            )
            return {
                "register_name": register_name,
                "value": self._format_register_value(value),
                "type": field_name if field_name else "unknown",
                "access": access_type
            }
        except asyncio.TimeoutError:
            logging.error(f"Timeout on Access_1_0 request for register '{register_name}' on node_id={node_id}")
        except Exception as e:
            logging.error(f"Error accessing register '{register_name}' on node_id={node_id}: {str(e)}")
        return None

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

            # Read the register first: its type and whether it may be written.
            access_request = uavcan.register.Access_1_0.Request(
                name=uavcan.register.Name_1_0(name=register_name.encode("utf-8")),
                value=uavcan.register.Value_1_0(empty=uavcan.primitive.Empty_1_0())
            )
            access_response_tuple = await asyncio.wait_for(
                register_access_client.call(access_request), timeout=self.REGISTER_TIMEOUT
            )
            if not access_response_tuple or not access_response_tuple[0]:
                # The node did not answer: an outcome of the request (504), not a server error.
                logging.warning(f"Failed to access register '{register_name}' on node_id={node_id}")
                return None

            access_response = access_response_tuple[0]
            current_field_name, _current_value = self._find_non_none_field(access_response.value)
            if current_field_name is None or current_field_name == 'empty':  # Cyphal's answer for a name it does not have
                raise ValueError(f"Node {node_id} has no register '{register_name}'")
            if not access_response.mutable:
                logging.info(f"Register '{register_name}' on node_id={node_id} is read-only: not written")
                raise ValueError(f"Register '{register_name}' is read-only")

            self._check_register_type(reg_type, current_field_name)
            # A number for a number register goes as the register's own type: one
            # of another type the node may keep its value for, as Cyphal allows.
            reg_type = current_field_name
            self._check_register_range(reg_type, value)
            access_request.value = self._register_value(reg_type, value)

            access_response_tuple = await asyncio.wait_for(
                register_access_client.call(access_request), timeout=self.REGISTER_TIMEOUT
            )
            if not access_response_tuple or not access_response_tuple[0]:
                logging.error(f"Failed to set register '{register_name}' on node_id={node_id}")
                return None

            updated_field_name, updated_value = self._find_non_none_field(access_response_tuple[0].value)
            # What the node now holds, as JSON can carry it.
            if isinstance(updated_value, np.ndarray):
                updated_value = updated_value.tolist() if updated_value.size > 1 else updated_value.item()
            elif updated_value is None:
                updated_value = "N/A"

            if not self._holds(reg_type, value, updated_value):
                raise RegisterNotApplied(f"Node {node_id} did not take the value: '{register_name}' holds "
                                         f"{self._format_register_value(np.array(updated_value)) if isinstance(updated_value, list) else updated_value}")
            logging.info(f"Successfully set register '{register_name}' on node_id={node_id} to '{updated_value}' (type: {updated_field_name})")
            return updated_value

        except asyncio.TimeoutError:
            logging.error(f"Timeout while setting register '{register_name}' on node_id={node_id}")
            return None
        except (ValueError, RegisterNotApplied):  # the request's or the node's say: the API answers it
            raise
        except Exception as e:
            logging.error(f"Error setting register '{register_name}' on node_id={node_id}: {str(e)}")
            raise
        finally:
            register_access_client.close()

    # What makes an element of each numeric register type from the text given for it.
    _NUMBER_OF = {
        **{name: int for name in ('integer64', 'integer32', 'integer16', 'integer8',
                                  'natural64', 'natural32', 'natural16', 'natural8')},
        **{name: float for name in ('real64', 'real32', 'real16')},
    }
    _INTEGER_RANGES = {
        'natural8': (0, 255), 'natural16': (0, 65535),
        'natural32': (0, 4294967295), 'natural64': (0, 2**64 - 1),
        'integer8': (-128, 127), 'integer16': (-32768, 32767),
        'integer32': (-2147483648, 2147483647), 'integer64': (-(2**63), 2**63 - 1),
    }
    _REAL_RANGES = {
        'real16': (-65504, 65504),
        'real32': (-3.4028235e+38, 3.4028235e+38),
    }

    # The precision a numeric register holds its values in, to compare what was
    # written with what the node reads back: 0.1 as real32 reads back 0.100000001.
    _DTYPE_OF = {'real16': np.float16, 'real32': np.float32, 'real64': np.float64, 'bit': np.bool_}

    def _holds(self, reg_type: str, written: str, read_back: Any) -> bool:
        """Whether ``read_back`` (as set_register reports it) is the value ``written`` (as typed) was sent as."""
        if reg_type == 'string':
            return read_back == written
        if reg_type == 'unstructured':
            return str(read_back).lower() == bytes.fromhex(written).hex()
        if reg_type == 'bit' or reg_type in self._NUMBER_OF:
            sent = self._register_value(reg_type, written)
            sent_values = list(getattr(sent, reg_type).value)
            back = read_back if isinstance(read_back, list) else [read_back]
            dtype = self._DTYPE_OF.get(reg_type, np.int64 if reg_type.startswith('integer') else np.uint64)
            try:
                # A NaN read back is the NaN written, though NaN equals nothing.
                return len(back) == len(sent_values) and bool(np.array_equal(
                    np.array(sent_values, dtype=dtype), np.array(back, dtype=dtype),
                    equal_nan=bool(np.issubdtype(dtype, np.floating))))
            except (TypeError, ValueError, OverflowError):
                return False
        return True  # 'empty': nothing to compare

    def _check_register_type(self, reg_type: str, current_field_name: str) -> None:
        """ValueError unless a value of ``reg_type`` may go to a register that holds ``current_field_name``:
        the same type, or a number for a number."""
        if current_field_name == reg_type:
            return
        logging.warning(f"Provided type '{reg_type}' does not match register type '{current_field_name}'")
        if reg_type in self._NUMBER_OF and current_field_name in self._NUMBER_OF:
            logging.warning(f"Type mismatch tolerated: converting '{reg_type}' to '{current_field_name}'")
        else:
            raise ValueError(f"Type mismatch: expected '{current_field_name}', got '{reg_type}'")

    def _check_register_range(self, reg_type: str, value: str) -> None:
        """ValueError if a number in ``value`` does not fit ``reg_type``, before pycyphal is asked to carry it."""
        if reg_type in self._INTEGER_RANGES:
            lo, hi = self._INTEGER_RANGES[reg_type]
            for v in self._parse_array_value(value):
                n = int(v)
                if n < lo or n > hi:
                    raise ValueError(f"Value {n} out of range for {reg_type} ({lo}–{hi})")
        elif reg_type in self._REAL_RANGES:
            low, high = self._REAL_RANGES[reg_type]
            for v in self._parse_array_value(value):
                x = float(v)
                if x < low or x > high:
                    raise ValueError(f"Value {x} out of range for {reg_type}")

    def _register_value(self, reg_type: str, value: str):
        """``value``, as typed, as the uavcan.register.Value of ``reg_type`` to send; ValueError if it is none."""
        new_value = uavcan.register.Value_1_0()
        try:
            if reg_type == 'string':
                new_value.string = uavcan.primitive.String_1_0(value=value.encode("utf-8"))
            elif reg_type == 'unstructured':
                new_value.unstructured = uavcan.primitive.Unstructured_1_0(value=bytearray.fromhex(value))
            elif reg_type == 'bit':
                valid_bool = {'true': True, '1': True, 'false': False, '0': False}
                bools = []
                for v in self._parse_array_value(value):
                    key = v.lower()
                    if key not in valid_bool:
                        raise ValueError(f"Invalid boolean: '{v}' — use True/False or 1/0")
                    bools.append(valid_bool[key])
                new_value.bit = uavcan.primitive.array.Bit_1_0(value=bools)
            elif reg_type in self._NUMBER_OF:
                # 'natural16' is carried by uavcan.primitive.array.Natural16_1_0, and so on.
                array_type = getattr(uavcan.primitive.array, f"{reg_type.capitalize()}_1_0")
                number = self._NUMBER_OF[reg_type]
                setattr(new_value, reg_type, array_type(value=[number(v) for v in self._parse_array_value(value)]))
            else:
                logging.error(f"Unsupported register type '{reg_type}'")
                raise ValueError(f"Unsupported register type '{reg_type}'")
        except ValueError as e:
            logging.error(f"Invalid value '{value}' for type '{reg_type}': {str(e)}")
            raise ValueError(f"Invalid value '{value}' for type '{reg_type}'")
        return new_value
