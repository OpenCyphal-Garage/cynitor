"""What is on the bus: nodes and their latest messages, subjects' types, services, registers, history and identities.

Handlers of the REST API, which WebSocketServer (websocket_server.py) inherits and routes to."""

import asyncio
import sqlite3
import time
from typing import Optional

from aiohttp import web

from api_helpers import ApiHandlers, _parse_int, command_refusal, listen_only_refusal, logger, MAX_NODE_ID, MAX_SERVICE_ID, MAX_SUBJECT_ID


def _synthesize_nodes_from_recording(db_path, recording_id: int) -> dict:
    """Reconstruct a /api/nodes-shaped payload from a recording's events.

    Grouped by publisher_node_id: each node's publishers list is the unique
    set of subject_ids that node appeared as the publisher of within the
    recording window. Approximate by design — no GetInfo, no health/mode,
    no client/server ports. Frontend should surface this as a replay view.
    """
    nodes: dict[int, dict] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT publisher_node_id, unique_id, subject_or_service_id, message_type"
            "  FROM recording_events"
            " WHERE recording_id = ? AND kind = 'subject'"
            "   AND publisher_node_id IS NOT NULL",
            (int(recording_id),),
        ).fetchall()
    for r in rows:
        nid = int(r["publisher_node_id"])
        sid = r["subject_or_service_id"]
        if nid not in nodes:
            nodes[nid] = {
                "node_id": nid,
                "unique_id": None,
                "unique_id_hex": r["unique_id"],
                "uptime": None,
                "has_disappeared": False,
                "has_responded_to_getinfo": False,
                "name": f"Node {nid}",
                "software_version": None,
                "publishers": [],
                "subscribers": [],
                "clients": [],
                "servers": [],
                "last_seen": None,
                "_replay": True,
            }
        if sid is not None and sid not in nodes[nid]["publishers"]:
            nodes[nid]["publishers"].append(int(sid))
    return {"node_count": len(nodes), "nodes": {k: v for k, v in nodes.items()}}


class NodesApi(ApiHandlers):
    """The nodes part of WebSocketServer, which has the session and the rest these use."""

    async def _set_subject_type(self, request: web.Request) -> web.Response:
        subject_id, err = _parse_int(request.match_info.get('subject_id'), 'subject_id', 0, MAX_SUBJECT_ID)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        type_name = body.get("type") if isinstance(body, dict) else None
        if not isinstance(type_name, str) or not type_name.strip():
            return web.json_response({"error": "type is required, e.g. uavcan.primitive.scalar.Real32.1.0"},
                                     status=400)
        return self._subject_type_change(subject_id, type_name.strip())

    async def _clear_subject_type(self, request: web.Request) -> web.Response:
        subject_id, err = _parse_int(request.match_info.get('subject_id'), 'subject_id', 0, MAX_SUBJECT_ID)
        if err:
            return err
        return self._subject_type_change(subject_id, None)

    def _subject_type_change(self, subject_id: int, type_name: Optional[str]) -> web.Response:
        try:
            self.session.user_types.set(subject_id, type_name)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)
        return web.json_response({"subject_id": subject_id, "type": type_name})

    async def _guess_subject_type(self, request: web.Request) -> web.Response:
        subject_id, err = _parse_int(request.match_info.get('subject_id'), 'subject_id', 0, MAX_SUBJECT_ID)
        if err:
            return err
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        types = await asyncio.to_thread(self.dsdl_manager.message_types)
        try:
            result = await self.session.user_types.guess(subject_id, types)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)
        return web.json_response(result)

    async def _get_services(self, request: web.Request) -> web.Response:
        """Return service schema metadata for a node."""
        node_id, err = _parse_int(request.match_info.get('node_id'), 'node_id', 0, MAX_NODE_ID)
        if err:
            return err

        if not self.session.is_running:
            return web.json_response({"error": "CAN bus not connected"}, status=503)

        schema = self.session.telemetry.get_service_schema(node_id)
        if schema is None:
            return web.json_response({"error": f"Node {node_id} not found"}, status=404)
        return web.json_response(schema)

    async def _get_clients(self, request: web.Request) -> web.Response:
        """Return enriched client port info for a node."""
        node_id, err = _parse_int(request.match_info.get('node_id'), 'node_id', 0, MAX_NODE_ID)
        if err:
            return err

        if not self.session.is_running:
            return web.json_response({"error": "CAN bus not connected"}, status=503)

        info = self.session.telemetry.get_client_info(node_id)
        if info is None:
            return web.json_response({"error": f"Node {node_id} not found"}, status=404)
        return web.json_response(info)

    async def _get_registers(self, request: web.Request) -> web.Response:
        """Return all registers for a node."""
        node_id, err = _parse_int(request.match_info.get('node_id'), 'node_id', 0, MAX_NODE_ID)
        if err:
            return err

        if not self.session.is_running:
            return web.json_response({"error": "CAN bus not connected"}, status=503)
        if refused := listen_only_refusal(self.session):
            return refused

        try:
            registers = await self.session.scanner.get_registers(node_id)
            return web.json_response({"node_id": node_id, "registers": registers})
        except TimeoutError as e:
            return web.json_response({"error": str(e)}, status=504)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=500)
        except Exception as e:
            logger.error(f"Error in GET /api/registers/{node_id}: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _set_register(self, request: web.Request) -> web.Response:
        """Set a register value on a node."""
        node_id, err = _parse_int(request.match_info.get('node_id'), 'node_id', 0, MAX_NODE_ID)
        if err:
            return err

        if not self.session.is_running:
            return web.json_response({"error": "CAN bus not connected"}, status=503)
        if refused := command_refusal(self.session):
            return refused

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        name = body.get("name")
        value = body.get("value")
        reg_type = body.get("type")
        if not name or value is None or not reg_type:
            return web.json_response({"error": "Missing required fields: name, value, type"}, status=400)

        from scanner_registers import RegisterNotApplied  # imports the compiled DSDL, ready once connected
        client = request.remote  # who asked, for the node's history
        try:
            updated = await self.session.scanner.set_register(node_id, name, str(value), reg_type)
            if updated is None:
                await self._note_in_node_history(node_id, "register_write", {
                    "name": name, "value": str(value), "status": "timeout", "client": client})
                return web.json_response({"error": "Write failed — no response from node"}, status=504)
            if isinstance(updated, list):
                formatted = ', '.join(str(v) for v in updated)
            else:
                formatted = str(updated)
            await self._note_in_node_history(node_id, "register_write", {
                "name": name, "value": str(value), "read_back": formatted, "status": "ok", "client": client})
            return web.json_response({"status": "ok", "name": name, "value": formatted})
        except RegisterNotApplied as e:
            await self._note_in_node_history(node_id, "register_write", {
                "name": name, "value": str(value), "status": "not_applied", "client": client})
            return web.json_response({"error": str(e)}, status=409)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            logger.error(f"Error in POST /api/registers/{node_id}/set: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _note_in_node_history(self, node_id: int, event_type: str, detail: dict) -> None:
        """Record what Cynitor did to a node (a register write, a firmware update) in its history."""
        if self.session.event_logger is not None:
            uid = self.session.scanner._get_node_unique_id_hex(node_id) if self.session.scanner else None
            await self.session.event_logger.log_node_event(node_id, event_type, detail, unique_id=uid)

    async def _call_service(self, request: web.Request) -> web.Response:
        """Invoke a service on a remote node and return the response."""
        node_id, err = _parse_int(request.match_info.get('node_id'), 'node_id', 0, MAX_NODE_ID)
        if err:
            return err
        service_id, err = _parse_int(request.match_info.get('service_id'), 'service_id', 0, MAX_SERVICE_ID)
        if err:
            return err

        if not self.session.is_running:
            return web.json_response({"error": "CAN bus not connected"}, status=503)
        if refused := command_refusal(self.session):
            return refused

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        attributes = body.get("attributes", {})

        meta = self.session.scanner.service_metadata.get((node_id, service_id))
        if not meta:
            node = getattr(self.session.scanner, "all_nodes", {}).get(node_id)
            if node is not None and getattr(node, "has_disappeared", False) is True:
                # Its services were forgotten when it went offline (scanner_node): not "not found".
                return web.json_response({"status": "error", "error": f"Node {node_id} is offline"}, status=409)
            return web.json_response(
                {"status": "error", "error": f"Service {service_id} not found on node {node_id}"},
                status=404,
            )

        service_type = f"{meta['namespace']}.{meta['service_name']}"

        uid = self.session.scanner._get_node_unique_id_hex(node_id) if self.session.scanner else None
        client = request.remote  # who asked, for the node's history

        t0 = time.monotonic()
        try:
            response_str = await self.session.scanner.make_service_call(
                node_id, service_id, service_type, attributes
            )
            latency_ms = round((time.monotonic() - t0) * 1000)
            if self.session.event_logger:
                await self.session.event_logger.log_node_event(node_id, "service_call", {
                    "service_id": service_id, "service_type": service_type,
                    "status": "ok", "latency_ms": latency_ms,
                    "response": response_str, "client": client,
                }, unique_id=uid)
            return web.json_response({
                "status": "ok",
                "latency_ms": latency_ms,
                "response": response_str,
            })
        except asyncio.TimeoutError:
            latency_ms = round((time.monotonic() - t0) * 1000)
            if self.session.event_logger:
                await self.session.event_logger.log_node_event(node_id, "service_call", {
                    "service_id": service_id, "service_type": service_type,
                    "status": "timeout", "latency_ms": latency_ms, "client": client,
                }, unique_id=uid)
            return web.json_response({
                "status": "timeout",
                "latency_ms": latency_ms,
                "error": f"Service {service_id} on node {node_id} timed out",
            }, status=504)
        except ValueError as e:
            return web.json_response({"status": "error", "error": str(e)}, status=400)
        except ConnectionError as e:  # the node went offline while the call waited
            return web.json_response({"status": "error", "error": str(e)}, status=409)
        except Exception as e:
            latency_ms = round((time.monotonic() - t0) * 1000)
            if self.session.event_logger:
                await self.session.event_logger.log_node_event(node_id, "service_call", {
                    "service_id": service_id, "service_type": service_type,
                    "status": "error", "latency_ms": latency_ms, "client": client,
                }, unique_id=uid)
            logger.error(f"Service call failed: {e}", exc_info=True)
            return web.json_response({
                "status": "error",
                "latency_ms": latency_ms,
                "error": str(e),
            }, status=500)

    _TIME_RANGE_MAP = {"5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}

    async def _get_node_history(self, request: web.Request) -> web.Response:
        node_id, err = _parse_int(request.match_info.get('node_id'), 'node_id', 0, MAX_NODE_ID)
        if err:
            return err

        if not self.session.event_logger:
            return web.json_response({"error": "Event logger not available"}, status=503)

        unique_id = request.query.get("unique_id")

        range_str = request.query.get("range", "1h")
        offset = self._TIME_RANGE_MAP.get(range_str)
        since_unix = time.time() - offset if offset else time.time() - 3600

        types_str = request.query.get("types")
        event_types = types_str.split(",") if types_str else None

        try:
            limit = min(int(request.query.get("limit", "500")), 2000)
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid limit parameter"}, status=400)

        events = await self.session.event_logger.get_node_history(
            node_id, since_unix=since_unix, event_types=event_types, limit=limit, unique_id=unique_id,
        )
        return web.json_response({"node_id": node_id, "events": events})

    async def _get_recent_node_events(self, request: web.Request) -> web.Response:
        """Lifecycle events of every node, newest first, for an overview of the bus.

        ``now_unix`` is the server's clock, so that a client can tell an
        event's age without trusting its own.
        """
        if not self.session.event_logger:
            return web.json_response({"error": "Event logger not available"}, status=503)

        offset = self._TIME_RANGE_MAP.get(request.query.get("range", "15m"))
        if offset is None:
            return web.json_response({"error": "Invalid range parameter"}, status=400)
        types_str = request.query.get("types")
        event_types = types_str.split(",") if types_str else None
        try:
            limit = min(int(request.query.get("limit", "500")), 2000)
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid limit parameter"}, status=400)
        if limit < 1:
            return web.json_response({"error": "Invalid limit parameter"}, status=400)

        now = time.time()
        events = await self.session.event_logger.get_node_history(
            None, since_unix=now - offset, event_types=event_types, limit=limit,
        )
        return web.json_response({"now_unix": now, "events": events})

    async def _get_node_subject_summary(self, request: web.Request) -> web.Response:
        node_id, err = _parse_int(request.match_info.get('node_id'), 'node_id', 0, MAX_NODE_ID)
        if err:
            return err

        if not self.session.event_logger:
            return web.json_response({"error": "Event logger not available"}, status=503)

        unique_id = request.query.get("unique_id")
        subjects = await self.session.event_logger.get_subject_summary(node_id, unique_id=unique_id)
        return web.json_response({"node_id": node_id, "subjects": subjects})

    async def _get_service_call_history(self, request: web.Request) -> web.Response:
        service_id, err = _parse_int(request.match_info.get('service_id'), 'service_id', 0, MAX_SERVICE_ID)
        if err:
            return err

        if not self.session.event_logger:
            return web.json_response({"error": "Event logger not available"}, status=503)

        range_str = request.query.get("range", "7d")
        offset = self._TIME_RANGE_MAP.get(range_str)
        since_unix = time.time() - offset if offset else time.time() - 604800

        try:
            limit = min(int(request.query.get("limit", "50")), 200)
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid limit parameter"}, status=400)

        node_id_str = request.query.get("node_id")
        node_id = None
        if node_id_str:
            node_id, err = _parse_int(node_id_str, 'node_id', 0, MAX_NODE_ID)
            if err:
                return err
        unique_id = request.query.get("unique_id")

        history = await self.session.event_logger.get_service_call_history(
            service_id, since_unix=since_unix, limit=limit, node_id=node_id, unique_id=unique_id,
        )

        telemetry = self.session.telemetry
        if telemetry:
            nodes_info = telemetry.get_all_nodes_info().get("nodes", {})
            for entry in history:
                nid = entry.get("node_id")
                if nid is not None:
                    node_info = nodes_info.get(str(nid))
                    if node_info:
                        entry["node_name"] = node_info.get("name")
                        entry["node_unique_id"] = node_info.get("unique_id")

        return web.json_response({"service_id": service_id, "history": history})

    async def _get_latest_subject(self, request: web.Request) -> web.Response:
        telemetry = self.session.telemetry
        if telemetry is None:
            return web.json_response({"error": "CAN not connected"}, status=503)

        subject_id, err = _parse_int(request.match_info.get('subject_id'), 'subject_id', 0, MAX_SUBJECT_ID)
        if err:
            return err

        try:
            event = telemetry.get_latest_subject(subject_id)

            if event:
                return web.json_response(event)
            else:
                return web.json_response(
                    {"error": f"No data for subject {subject_id}"},
                    status=404
                )
        except Exception as e:
            logger.error(f"Error in GET /api/latest/subject: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _get_latest_node(self, request: web.Request) -> web.Response:
        telemetry = self.session.telemetry
        if telemetry is None:
            return web.json_response({"error": "CAN not connected"}, status=503)

        try:
            node_id, err = _parse_int(request.match_info.get('node_id'), 'node_id', 0, MAX_NODE_ID)
            if err:
                return err
            events = telemetry.get_latest_node(node_id)

            if not events:
                known_node = False
                scanner = getattr(telemetry, "scanner", None)
                if scanner is not None:
                    node = getattr(scanner, "all_nodes", {}).get(node_id)
                    known_node = bool(node and getattr(node, "has_appeared", False))

                return web.json_response(
                    {
                        "error": f"No telemetry cached for node {node_id}",
                        "known_node": known_node,
                    },
                    status=404,
                )

            return web.json_response({
                "node_id": node_id,
                "events": events
            })
        except Exception as e:
            logger.error(f"Error in GET /api/latest/node: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _get_identity_map(self, request: web.Request) -> web.Response:
        scanner = self.session.scanner
        if scanner is None:
            return web.json_response({"mappings": {}})
        return web.json_response({"mappings": scanner.identity_map.all_mappings()})

    async def _delete_identity(self, request: web.Request) -> web.Response:
        uid = request.match_info.get('unique_id', '')
        scanner = self.session.scanner
        if not scanner:
            return web.json_response({"error": "Not connected"}, status=503)
        await self._forget_device(uid)
        return web.json_response({"status": "ok"})

    async def _forget_device(self, unique_id: Optional[str]) -> None:
        """Forget a device: its identity, and what was saved about it."""
        if not unique_id:
            return
        scanner, event_logger = getattr(self.session, "scanner", None), getattr(self.session, "event_logger", None)
        if scanner is not None:
            scanner.identity_map.remove_identity(unique_id)
        if event_logger:
            await event_logger.delete_node_data(unique_id)

    async def _get_all_nodes(self, request: web.Request) -> web.Response:
        telemetry = self.session.telemetry
        if telemetry is not None:
            try:
                nodes_info = telemetry.get_all_nodes_info()
                for node in nodes_info["nodes"].values():
                    test_id = self.session.test_nodes.identify(node.get("node_id"), node.get("unique_id_hex"))
                    if test_id is not None:
                        node["test_node"] = test_id
                return web.json_response(nodes_info)
            except Exception as e:
                logger.error(f"Error in GET /api/nodes: {e}", exc_info=True)
                return web.json_response({"error": str(e)}, status=500)

        # When replay is active and CAN isn't connected, synthesise a node
        # payload from the recording itself so the node table has something
        # to render. The shape is approximate — no GetInfo, no health/mode/
        # uptime, no client port lists — but it's enough to let the user
        # navigate to a node and see what its publishers were doing.
        replay = self.session.replay
        if replay is not None and self.session.event_logger is not None:
            try:
                # Read once per replay: the dashboard asks every second, and a
                # big recording takes seconds to read.
                if replay.nodes_payload is None:
                    replay.nodes_payload = await asyncio.to_thread(
                        _synthesize_nodes_from_recording,
                        self.session.event_logger.db_path, replay.recording_id,
                    )
                return web.json_response(replay.nodes_payload)
            except Exception as e:
                logger.error(f"Error synthesising replay nodes: {e}", exc_info=True)
                return web.json_response({"error": str(e)}, status=500)

        return web.json_response({"node_count": 0, "nodes": {}})
