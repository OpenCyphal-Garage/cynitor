#!/usr/bin/env python3

import asyncio
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional, Set, Any
from aiohttp import web, WSCloseCode


def _csv_escape(value: Any) -> str:
    """Escape a value for inclusion in a CSV cell."""
    if value is None:
        return ""
    s = str(value)
    if any(c in s for c in (",", '"', "\n", "\r")):
        s = '"' + s.replace('"', '""') + '"'
    return s

logger = logging.getLogger(__name__)

MAX_NODE_ID = 127
MAX_SUBJECT_ID = 8191
MAX_SERVICE_ID = 511

def _parse_int(value: str, name: str, lo: int = 0, hi: int = MAX_NODE_ID):
    try:
        v = int(value)
    except (ValueError, TypeError):
        return None, web.json_response({"error": f"Invalid {name}"}, status=400)
    if v < lo or v > hi:
        return None, web.json_response({"error": f"{name} must be {lo}–{hi}"}, status=400)
    return v, None


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


class WebSocketServer:
    """
    WebSocket server for telemetry streaming.

    Features:
    - Real-time event streaming to multiple clients
    - Event filtering (by subject_id, node_id, message_type)
    - REST API for querying latest events
    - CAN interface connect/disconnect via REST
    - CORS support for web clients
    """

    # Paths the auth middleware always lets through. /api/health is the only
    # truly open endpoint (used by load balancers and uptime checks).
    _AUTH_OPEN_PATHS = frozenset({"/api/health"})

    def __init__(
        self,
        session: Any,
        host: str = "127.0.0.1",
        port: int = 8080,
        log_store: Optional[Any] = None,
        dsdl_manager: Optional[Any] = None,
        auth_token: Optional[str] = None,
        website_dir: Optional[Path] = None,
    ) -> None:
        self.session = session
        self.host = host
        self.port = port
        self.log_store = log_store
        self.dsdl_manager = dsdl_manager
        # When present, the dashboard is served from this server so a single
        # binary is all a deployment needs. None means API-only, which is how
        # the desktop app runs it (the shell carries its own copy of the UI).
        self.website_dir = website_dir if website_dir and website_dir.is_dir() else None
        # When set, every request outside _AUTH_OPEN_PATHS must present this
        # token via Authorization: Bearer <token> (REST) or ?token=<token>
        # (WebSocket). When None, the server runs open — same behaviour as
        # before this option was added.
        self.auth_token = auth_token or None

        # Client management
        self.clients: Set[web.WebSocketResponse] = set()
        self.client_filters: dict[web.WebSocketResponse, dict[str, Any]] = {}
        # Clients that opted into the raw frame-capture stream → their per-client
        # subscriber queue on the FrameCaptureManager. Empty unless a Debugging
        # view explicitly started capture.
        self.capture_clients: dict[web.WebSocketResponse, asyncio.Queue] = {}

        # App and runner. Auth middleware runs first so unauthorized requests
        # never reach the CORS layer or the handlers.
        self.app = web.Application(middlewares=[self._auth_middleware, self._cors_middleware])
        self.runner: Optional[web.AppRunner] = None
        self._running = False

        self._setup_routes()

    @staticmethod
    def _is_protected_path(path: str) -> bool:
        """Whether a path needs a token when auth is enabled.

        Only the API and the event stream are protected. The dashboard's own
        HTML, CSS and JavaScript are served open, because a browser has to be
        able to load the page before it can prompt for a token — and those
        assets are not secret. What they cannot do without one is read bus
        data or command a node.
        """
        return path == "/ws" or path == "/api" or path.startswith("/api/")

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if self.auth_token is None or request.method == "OPTIONS":
            return await handler(request)
        if request.path in self._AUTH_OPEN_PATHS:
            return await handler(request)
        if not self._is_protected_path(request.path):
            return await handler(request)
        token = self._extract_token(request)
        if token != self.auth_token:
            return web.json_response({"error": "missing or invalid token"}, status=401)
        return await handler(request)

    @staticmethod
    def _extract_token(request: web.Request) -> Optional[str]:
        # WebSocket clients can't set custom headers in browsers, so the WS
        # handshake accepts ?token=<value>. REST clients use the standard
        # Authorization: Bearer <value> header.
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[7:].strip() or None
        qs = request.query.get("token")
        return qs.strip() if qs else None

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            try:
                response = await handler(request)
            except web.HTTPException as ex:
                response = ex
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
        return response

    def _setup_routes(self) -> None:
        """Configure HTTP routes and WebSocket handler."""
        self.app.router.add_get('/ws', self._websocket_handler)

        self.app.router.add_get('/api', self._api_info)
        self.app.router.add_get('/api/info', self._api_info)
        self.app.router.add_get('/api/status', self._get_status)
        self.app.router.add_get('/api/nodes', self._get_all_nodes)
        self.app.router.add_get('/api/latest/subject/{subject_id}', self._get_latest_subject)
        self.app.router.add_get('/api/latest/node/{node_id}', self._get_latest_node)
        self.app.router.add_get('/api/logs', self._get_logs)
        self.app.router.add_get('/api/health', self._health_check)
        self.app.router.add_get('/api/services/{node_id}', self._get_services)
        self.app.router.add_get('/api/clients/{node_id}', self._get_clients)
        self.app.router.add_get('/api/registers/{node_id}', self._get_registers)
        self.app.router.add_post('/api/registers/{node_id}/set', self._set_register)
        self.app.router.add_post('/api/services/{node_id}/{service_id}/call', self._call_service)
        self.app.router.add_get('/api/nodes/{node_id}/history', self._get_node_history)
        self.app.router.add_get('/api/nodes/{node_id}/history/subjects', self._get_node_subject_summary)
        self.app.router.add_get('/api/services/{service_id}/history', self._get_service_call_history)
        self.app.router.add_get('/api/identity-map', self._get_identity_map)
        self.app.router.add_delete('/api/identity/{unique_id}', self._delete_identity)
        self.app.router.add_post('/api/can/connect', self._can_connect)
        self.app.router.add_post('/api/can/disconnect', self._can_disconnect)
        self.app.router.add_get('/api/can/transport', self._get_transport_diagnostics)
        self.app.router.add_get('/api/can/capture', self._get_capture)

        self.app.router.add_get('/api/recordings', self._get_recordings)
        self.app.router.add_post('/api/recordings', self._post_recording)
        self.app.router.add_post('/api/recordings/quick', self._post_quick_recording)
        self.app.router.add_get('/api/recordings/buffer', self._get_recordings_buffer)
        self.app.router.add_get('/api/recordings/{rec_id}', self._get_recording)
        self.app.router.add_patch('/api/recordings/{rec_id}', self._patch_recording)
        self.app.router.add_delete('/api/recordings/{rec_id}', self._delete_recording)
        self.app.router.add_post('/api/recordings/{rec_id}/stop', self._post_recording_stop)
        self.app.router.add_get('/api/recordings/{rec_id}/export', self._export_recording)

        self.app.router.add_post('/api/replay/start', self._replay_start)
        self.app.router.add_post('/api/replay/control', self._replay_control)
        self.app.router.add_post('/api/replay/seek', self._replay_seek)
        self.app.router.add_post('/api/replay/speed', self._replay_speed)
        self.app.router.add_get('/api/replay/status', self._replay_status)

        self.app.router.add_get('/api/dsdl/status', self._dsdl_status)
        self.app.router.add_get('/api/dsdl/namespaces', self._dsdl_namespaces)
        self.app.router.add_get('/api/dsdl/type/{full_name:.+}', self._dsdl_type_detail)
        self.app.router.add_post('/api/dsdl/custom/namespace', self._dsdl_create_namespace)
        self.app.router.add_post('/api/dsdl/custom/type', self._dsdl_save_type)
        self.app.router.add_delete('/api/dsdl/custom/type/{full_name:.+}', self._dsdl_delete_type)
        self.app.router.add_get('/api/dsdl/custom/namespaces', self._dsdl_list_custom_namespaces)
        self.app.router.add_post('/api/dsdl/compile', self._dsdl_compile)

        # The dashboard, when this server is also hosting it. Registered last:
        # aiohttp matches resources in registration order, so every /api and
        # /ws route above wins over the catch-all static mount below.
        if self.website_dir:
            self.app.router.add_get('/', self._serve_index)
            self.app.router.add_get('/config.js', self._serve_config_js)
            self.app.router.add_static('/', self.website_dir)
            logger.info("Serving the dashboard from %s", self.website_dir)

    async def _serve_index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self.website_dir / "index.html")

    async def _serve_config_js(self, request: web.Request) -> web.Response:
        """Tell the page which backend to talk to: this one.

        The frontend's default address is only correct when it is served by a
        separate static file server on the developer's own machine. Served
        from here, the API lives at this request's own origin, whatever host
        and port the user reached us on. website/config.js is an empty
        placeholder so the development flow keeps the built-in default.
        """
        origin = f"{request.scheme}://{request.host}"
        body = f"window.__CYNITOR = {json.dumps({'apiBase': origin})};\n"
        return web.Response(text=body, content_type="application/javascript")

    async def start(self) -> None:
        """Start the WebSocket server."""
        try:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            site = web.TCPSite(self.runner, self.host, self.port)
            await site.start()
            self._running = True
            logger.info(f"WebSocket server started on ws://{self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to start WebSocket server: {e}", exc_info=True)
            raise

    async def stop(self) -> None:
        """Stop the WebSocket server and clean up."""
        self._running = False

        for ws in list(self.clients):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"Server shutdown")

        if self.runner:
            await self.runner.cleanup()

        logger.info("WebSocket server stopped")

    # ------------------------------------------------------------------
    # CAN connect / disconnect
    # ------------------------------------------------------------------

    async def _get_status(self, request: web.Request) -> web.Response:
        from main import discover_can_interfaces
        bus_load = self.session.bus_load
        available = await asyncio.to_thread(discover_can_interfaces)
        return web.json_response({
            "status": "running" if self.session.is_running else "idle",
            "can_interface": self.session.can_interface,
            "available_interfaces": available,
            "bus_utilization": bus_load.utilization if bus_load else None,
            "last_error": self.session.last_error,
        })

    async def _can_connect(self, request: web.Request) -> web.Response:
        from main import discover_can_interfaces
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        iface = payload.get("interface", "").strip() if isinstance(payload, dict) else ""
        if not iface:
            return web.json_response({"error": "Field 'interface' required"}, status=400)

        if self.session.is_running:
            return web.json_response({"error": "Already connected"}, status=409)

        available = await asyncio.to_thread(discover_can_interfaces)
        if iface not in available:
            return web.json_response(
                {"error": f"Unknown interface: {iface}", "available_interfaces": available},
                status=400,
            )

        try:
            await self.session.connect(iface)
            return web.json_response({"status": "running", "can_interface": iface})
        except Exception as e:
            logger.error(f"Failed to connect CAN: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _can_disconnect(self, request: web.Request) -> web.Response:
        from main import discover_can_interfaces
        if not self.session.is_running:
            return web.json_response({"error": "Not connected"}, status=409)

        await self.session.disconnect()
        available = await asyncio.to_thread(discover_can_interfaces)
        return web.json_response({
            "status": "idle",
            "available_interfaces": available,
        })

    async def _get_transport_diagnostics(self, request: web.Request) -> web.Response:
        """Transport-layer ("below the DSDL") diagnostics snapshot.

        Returns ``{"connected": false}`` when no CAN session is active so the
        Debugging view can render an idle state instead of erroring. When
        connected, combines pycyphal transport counters/protocol params (cheap,
        in-process) with controller/bus-state details parsed from iproute2
        (run off the event loop via to_thread).
        """
        from main import get_can_link_diagnostics
        if not self.session.is_running or self.session.scanner is None:
            return web.json_response({"connected": False})

        info = self.session.scanner.get_transport_info()
        iface = self.session.can_interface
        link = await asyncio.to_thread(get_can_link_diagnostics, iface) if iface else {}
        bus_load = self.session.bus_load
        return web.json_response({
            "connected": True,
            "interface": iface,
            "protocol": info.get("protocol"),
            "statistics": info.get("statistics"),
            "capture_active": info.get("capture_active", False),
            "link": link,
            "bus_utilization": bus_load.utilization if bus_load else None,
        })

    async def _get_capture(self, request: web.Request) -> web.Response:
        """Snapshot of the raw frame-capture ring buffer + counters.

        Used by the Debugging view to backfill the frame monitor on open or
        after a reconnect. Returns an inactive empty result when no CAN session
        exists. Live frames arrive via the WebSocket ``can_frame`` stream.
        """
        mgr = self.session.frame_capture
        if mgr is None:
            return web.json_response({"active": False, "stats": None, "frames": []})
        try:
            limit = min(int(request.query.get("limit", "500")), 2000)
        except (ValueError, TypeError):
            limit = 500
        return web.json_response({
            "active": mgr.active,
            "stats": mgr.stats(),
            "frames": mgr.snapshot(limit),
        })

    # ------------------------------------------------------------------
    # Service endpoints
    # ------------------------------------------------------------------

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

        try:
            registers = await self.session.scanner.get_registers(node_id)
            return web.json_response({"node_id": node_id, "registers": registers})
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

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        name = body.get("name")
        value = body.get("value")
        reg_type = body.get("type")
        if not name or value is None or not reg_type:
            return web.json_response({"error": "Missing required fields: name, value, type"}, status=400)

        try:
            updated = await self.session.scanner.set_register(node_id, name, str(value), reg_type)
            if updated is None:
                return web.json_response({"error": "Write failed — no response from node"}, status=504)
            if isinstance(updated, list):
                formatted = ', '.join(str(v) for v in updated)
            else:
                formatted = str(updated)
            return web.json_response({"status": "ok", "name": name, "value": formatted})
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            logger.error(f"Error in POST /api/registers/{node_id}/set: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

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

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        attributes = body.get("attributes", {})

        meta = self.session.scanner.service_metadata.get((node_id, service_id))
        if not meta:
            return web.json_response(
                {"status": "error", "error": f"Service {service_id} not found on node {node_id}"},
                status=404,
            )

        service_type = f"{meta['namespace']}.{meta['service_name']}"

        uid = self.session.scanner._get_node_unique_id_hex(node_id) if self.session.scanner else None

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
                    "response": response_str,
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
                    "status": "timeout", "latency_ms": latency_ms,
                }, unique_id=uid)
            return web.json_response({
                "status": "timeout",
                "latency_ms": latency_ms,
                "error": f"Service {service_id} on node {node_id} timed out",
            }, status=504)
        except ValueError as e:
            return web.json_response({"status": "error", "error": str(e)}, status=400)
        except Exception as e:
            latency_ms = round((time.monotonic() - t0) * 1000)
            if self.session.event_logger:
                await self.session.event_logger.log_node_event(node_id, "service_call", {
                    "service_id": service_id, "service_type": service_type,
                    "status": "error", "latency_ms": latency_ms,
                }, unique_id=uid)
            logger.error(f"Service call failed: {e}", exc_info=True)
            return web.json_response({
                "status": "error",
                "latency_ms": latency_ms,
                "error": str(e),
            }, status=500)

    # ------------------------------------------------------------------
    # Node history endpoints
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Recordings (named time-range bookmarks over the events log)
    # ------------------------------------------------------------------

    _MAX_REC_ID = 2**31 - 1
    _EXPORT_PAGE_SIZE = 2000

    async def _read_json_body(self, request: web.Request) -> tuple[Optional[dict], Optional[web.Response]]:
        try:
            body = await request.json()
        except Exception:
            return None, web.json_response({"error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return None, web.json_response({"error": "JSON body must be an object"}, status=400)
        return body, None

    def _require_event_logger(self) -> Optional[web.Response]:
        if not self.session.event_logger:
            return web.json_response({"error": "Event logger not available"}, status=503)
        return None

    async def _get_recordings(self, request: web.Request) -> web.Response:
        # Before CAN is connected the event logger doesn't exist yet. Listing is a
        # read-only poll the frontend runs continuously, so return an empty list
        # (rather than 503) to avoid a spurious "Failed to load recordings" toast
        # at startup. The list populates once CAN connects and the logger starts.
        if not self.session.event_logger:
            return web.json_response({"recordings": []})
        recs = await self.session.event_logger.list_recordings()
        return web.json_response({"recordings": recs})

    @staticmethod
    def _parse_limits(body: dict) -> tuple[Optional[float], Optional[int], bool, Optional[web.Response]]:
        """Extract (max_length_seconds, max_events, stop_on_limit) from a POST
        body. Returns the parsed values + an optional error response."""
        max_length = body.get("max_length_seconds")
        if max_length is not None:
            if isinstance(max_length, bool) or not isinstance(max_length, (int, float)) or max_length <= 0:
                return None, None, False, web.json_response(
                    {"error": "max_length_seconds must be a positive number"}, status=400
                )
            max_length = float(max_length)
        max_events = body.get("max_events")
        if max_events is not None:
            if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
                return None, None, False, web.json_response(
                    {"error": "max_events must be a positive integer"}, status=400
                )
        stop_on_limit = bool(body.get("stop_on_limit", False))
        return max_length, max_events, stop_on_limit, None

    async def _post_recording(self, request: web.Request) -> web.Response:
        err = self._require_event_logger()
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return web.json_response({"error": "Name is required"}, status=400)
        max_length, max_events, stop_on_limit, err = self._parse_limits(body)
        if err:
            return err
        notes = body.get("notes")
        rec_id = await self.session.event_logger.create_recording(
            name=name.strip(),
            filter_spec=body.get("filter"),
            notes=notes if isinstance(notes, str) else None,
            max_length_seconds=max_length,
            max_events=max_events,
            stop_on_limit=stop_on_limit,
        )
        rec = await self.session.event_logger.get_recording_stats(rec_id)
        return web.json_response({"recording": rec}, status=201)

    async def _post_quick_recording(self, request: web.Request) -> web.Response:
        err = self._require_event_logger()
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return web.json_response({"error": "Name is required"}, status=400)
        last_seconds = body.get("last_seconds")
        if not isinstance(last_seconds, (int, float)) or isinstance(last_seconds, bool) or last_seconds <= 0:
            return web.json_response({"error": "last_seconds must be a positive number"}, status=400)
        notes = body.get("notes")
        now = time.time()
        rec_id = await self.session.event_logger.create_recording(
            name=name.strip(),
            start_unix=now - float(last_seconds),
            end_unix=now,
            filter_spec=body.get("filter"),
            notes=notes if isinstance(notes, str) else None,
        )
        rec = await self.session.event_logger.get_recording_stats(rec_id)
        return web.json_response({"recording": rec}, status=201)

    async def _get_recordings_buffer(self, request: web.Request) -> web.Response:
        err = self._require_event_logger()
        if err:
            return err
        stats = await self.session.event_logger.get_buffer_stats()
        return web.json_response({"buffer": stats})

    async def _post_recording_stop(self, request: web.Request) -> web.Response:
        rec_id, err = _parse_int(request.match_info.get('rec_id'), 'recording_id', 1, self._MAX_REC_ID)
        if err:
            return err
        if (e := self._require_event_logger()):
            return e
        ok = await self.session.event_logger.stop_recording(rec_id)
        if not ok:
            return web.json_response({"error": "Recording not found or already stopped"}, status=404)
        rec = await self.session.event_logger.get_recording_stats(rec_id)
        return web.json_response({"recording": rec})

    async def _patch_recording(self, request: web.Request) -> web.Response:
        rec_id, err = _parse_int(request.match_info.get('rec_id'), 'recording_id', 1, self._MAX_REC_ID)
        if err:
            return err
        if (e := self._require_event_logger()):
            return e
        body, err = await self._read_json_body(request)
        if err:
            return err
        name = body.get("name")
        notes = body.get("notes")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            return web.json_response({"error": "Name must be a non-empty string"}, status=400)
        if notes is not None and not isinstance(notes, str):
            return web.json_response({"error": "Notes must be a string"}, status=400)

        max_length = body.get("max_length_seconds")
        if max_length is not None:
            if isinstance(max_length, bool) or not isinstance(max_length, (int, float)) or max_length <= 0:
                return web.json_response({"error": "max_length_seconds must be a positive number"}, status=400)
            max_length = float(max_length)
        max_events = body.get("max_events")
        if max_events is not None:
            if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
                return web.json_response({"error": "max_events must be a positive integer"}, status=400)
        stop_on_limit = body.get("stop_on_limit")
        if stop_on_limit is not None and not isinstance(stop_on_limit, bool):
            return web.json_response({"error": "stop_on_limit must be a boolean"}, status=400)

        ok = await self.session.event_logger.update_recording(
            rec_id,
            name=name.strip() if isinstance(name, str) else None,
            notes=notes,
            max_length_seconds=max_length,
            max_events=max_events,
            stop_on_limit=stop_on_limit,
        )
        if not ok:
            existing = await self.session.event_logger.get_recording(rec_id)
            if not existing:
                return web.json_response({"error": "Recording not found"}, status=404)
            return web.json_response({"error": "Nothing to update"}, status=400)
        rec = await self.session.event_logger.get_recording_stats(rec_id)
        return web.json_response({"recording": rec})

    async def _get_recording(self, request: web.Request) -> web.Response:
        rec_id, err = _parse_int(request.match_info.get('rec_id'), 'recording_id', 1, self._MAX_REC_ID)
        if err:
            return err
        if (e := self._require_event_logger()):
            return e
        rec = await self.session.event_logger.get_recording_stats(rec_id)
        if not rec:
            return web.json_response({"error": "Recording not found"}, status=404)
        return web.json_response({"recording": rec})

    async def _delete_recording(self, request: web.Request) -> web.Response:
        rec_id, err = _parse_int(request.match_info.get('rec_id'), 'recording_id', 1, self._MAX_REC_ID)
        if err:
            return err
        if (e := self._require_event_logger()):
            return e
        purge = request.query.get("purge", "false").lower() == "true"
        ok = await self.session.event_logger.delete_recording(rec_id, purge_events=purge)
        if not ok:
            return web.json_response({"error": "Recording not found"}, status=404)
        return web.json_response({"deleted": True, "purged": purge})

    async def _export_recording(self, request: web.Request) -> web.StreamResponse:
        rec_id, err = _parse_int(request.match_info.get('rec_id'), 'recording_id', 1, self._MAX_REC_ID)
        if err:
            return err
        if (e := self._require_event_logger()):
            return e
        fmt = request.query.get("format", "csv").lower()
        if fmt not in ("csv", "jsonl"):
            return web.json_response({"error": "format must be csv or jsonl"}, status=400)
        rec = await self.session.event_logger.get_recording(rec_id)
        if not rec:
            return web.json_response({"error": "Recording not found"}, status=404)

        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", rec["name"])[:60] or f"recording-{rec_id}"
        if fmt == "csv":
            return await self._export_recording_csv(request, rec, safe_name)
        return await self._export_recording_jsonl(request, rec, safe_name)

    async def _export_recording_csv(self, request: web.Request, rec: dict, safe_name: str) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": f'attachment; filename="{safe_name}.csv"',
            }
        )
        await resp.prepare(request)
        header = "recording_id,timestamp_unix,timestamp,subject_id,publisher_node_id,unique_id,message_type,rate,attribute,value,unit\n"
        await resp.write(header.encode("utf-8"))

        offset = 0
        while True:
            events = await self.session.event_logger.get_recording_events(
                rec["id"], limit=self._EXPORT_PAGE_SIZE, offset=offset
            )
            if not events:
                break
            lines: list[str] = []
            for ev in events:
                base = [
                    str(rec["id"]),
                    f"{ev['timestamp_unix']:.6f}" if ev.get("timestamp_unix") is not None else "",
                    _csv_escape(ev.get("timestamp")),
                    _csv_escape(ev.get("subject_id")),
                    _csv_escape(ev.get("publisher_node_id")),
                    _csv_escape(ev.get("unique_id")),
                    _csv_escape(ev.get("message_type")),
                    _csv_escape(ev.get("rate")),
                ]
                attrs = ev.get("attributes") or []
                if not attrs:
                    lines.append(",".join(base + ["", "", ""]))
                else:
                    for a in attrs:
                        lines.append(",".join(base + [
                            _csv_escape(a.get("attribute")),
                            _csv_escape(a.get("value")),
                            _csv_escape(a.get("unit")),
                        ]))
            await resp.write(("\n".join(lines) + "\n").encode("utf-8"))
            offset += len(events)
            if len(events) < self._EXPORT_PAGE_SIZE:
                break
        await resp.write_eof()
        return resp

    async def _export_recording_jsonl(self, request: web.Request, rec: dict, safe_name: str) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "application/x-ndjson; charset=utf-8",
                "Content-Disposition": f'attachment; filename="{safe_name}.jsonl"',
            }
        )
        await resp.prepare(request)
        header = json.dumps({"recording": rec, "exported_at_unix": time.time()})
        await resp.write((header + "\n").encode("utf-8"))

        offset = 0
        while True:
            events = await self.session.event_logger.get_recording_events(
                rec["id"], limit=self._EXPORT_PAGE_SIZE, offset=offset
            )
            if not events:
                break
            chunk = "".join(json.dumps(ev) + "\n" for ev in events)
            await resp.write(chunk.encode("utf-8"))
            offset += len(events)
            if len(events) < self._EXPORT_PAGE_SIZE:
                break
        await resp.write_eof()
        return resp

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def _websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self.clients.add(ws)
        self.client_filters[ws] = {}

        logger.info(f"Client connected. Total clients: {len(self.clients)}")
        queue: Optional[asyncio.Queue] = None

        try:
            recv_task = asyncio.create_task(self._receive_client_messages(ws))
            metrics_task = asyncio.create_task(self._send_metrics_loop(ws))
            capture_task = asyncio.create_task(self._capture_loop(ws))

            while not ws.closed:
                telemetry = self.session.telemetry
                replay = self.session.replay
                source = telemetry if telemetry is not None else replay
                if source is not None:
                    queue = source.subscribe(max_queue=100)
                    consume_task = asyncio.create_task(self._consume_and_send(ws, queue))
                    done, pending = await asyncio.wait(
                        [consume_task, recv_task, metrics_task, capture_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        exc = task.exception()
                        if exc is not None:
                            logger.error(
                                "WebSocket subtask finished with exception",
                                exc_info=(type(exc), exc, exc.__traceback__),
                            )
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    break

                done, _ = await asyncio.wait(
                    [recv_task], timeout=1.0
                )
                if done:
                    break

            if not recv_task.done():
                recv_task.cancel()
                try:
                    await recv_task
                except asyncio.CancelledError:
                    pass
            if not metrics_task.done():
                metrics_task.cancel()
                try:
                    await metrics_task
                except asyncio.CancelledError:
                    pass
            if not capture_task.done():
                capture_task.cancel()
                try:
                    await capture_task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            logger.error(f"Error in WebSocket handler: {e}", exc_info=True)
        finally:
            self.clients.discard(ws)
            self.client_filters.pop(ws, None)
            cap_queue = self.capture_clients.pop(ws, None)
            if cap_queue is not None and self.session.frame_capture is not None:
                self.session.frame_capture.unsubscribe(cap_queue)
            if queue is not None:
                # The queue belongs to whichever source was active when we
                # subscribed. Both sources are unsubscribe-safe with an
                # unknown queue (set.discard is a no-op for missing keys).
                if self.session.telemetry is not None:
                    self.session.telemetry.unsubscribe(queue)
                if self.session.replay is not None:
                    self.session.replay.unsubscribe(queue)
            logger.info(f"Client disconnected. Total clients: {len(self.clients)}")

        return ws

    # Cap how long a single send may block waiting for a slow client. If TCP
    # backpressure stalls the send beyond this, the connection is considered
    # dead and the consumer exits — the handler then tears the WS down and
    # the broadcast loop stops queuing for the orphaned subscriber. Cynitor
    # broadcasts at most ~1 kHz of small JSON frames, so 10 s is far above
    # any healthy peer's worst-case latency.
    _WS_SEND_TIMEOUT = 10.0

    async def _consume_and_send(self, ws: web.WebSocketResponse, queue: asyncio.Queue) -> None:
        while self._running and not ws.closed:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue  # no events in the last 5s; loop back and re-check ws.closed
            except asyncio.CancelledError:
                break

            # End-of-session sentinel from ReplayManager: forward to the
            # client (so the UI can switch back to idle / re-fetch state)
            # then exit the consume loop. The WS handler will tear the
            # connection down and the client reconnects to pick up the
            # new state on a fresh subscriber.
            if isinstance(event, dict) and event.get("type") == "replay_ended":
                try:
                    await asyncio.wait_for(ws.send_json(event), timeout=self._WS_SEND_TIMEOUT)
                except Exception:
                    pass
                break

            if not self._event_matches_filter(event, ws):
                continue

            try:
                await asyncio.wait_for(ws.send_json(event), timeout=self._WS_SEND_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "WebSocket send blocked > %ss — closing slow/stuck client",
                    self._WS_SEND_TIMEOUT,
                )
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error sending to client: %s", e)
                break

    async def _send_metrics_loop(self, ws: web.WebSocketResponse) -> None:
        """Send bus utilization metrics to the client every second."""
        try:
            while self._running and not ws.closed:
                await asyncio.sleep(1.0)
                bus_load = self.session.bus_load
                await ws.send_json({
                    "type": "metrics",
                    "bus_utilization": bus_load.utilization if bus_load else None,
                })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Metrics loop ended: {e}")

    # Batch window for the raw frame stream. Captured frames are coalesced into
    # one message per window to bound message rate under heavy bus load.
    _CAPTURE_BATCH_WINDOW = 0.12
    _CAPTURE_BATCH_MAX = 250

    async def _capture_loop(self, ws: web.WebSocketResponse) -> None:
        """Forward raw captured frames to a client that opted in via a
        ``{"type":"capture","enabled":true}`` message. No-op (idle poll) until
        the client subscribes; batches frames to bound the WS message rate.
        """
        try:
            while self._running and not ws.closed:
                q = self.capture_clients.get(ws)
                if q is None:
                    await asyncio.sleep(0.2)
                    continue
                try:
                    first = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                frames = [first]
                await asyncio.sleep(self._CAPTURE_BATCH_WINDOW)
                while len(frames) < self._CAPTURE_BATCH_MAX:
                    try:
                        frames.append(q.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                mgr = self.session.frame_capture
                await asyncio.wait_for(ws.send_json({
                    "type": "can_frame",
                    "frames": frames,
                    "stats": mgr.stats() if mgr else None,
                }), timeout=self._WS_SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Capture loop ended: {e}")

    async def _handle_capture_message(self, ws: web.WebSocketResponse, enabled: bool) -> None:
        """Enable/disable raw frame forwarding for this client. Enabling also
        starts transport-level capture if it is not already active (sticky)."""
        mgr = self.session.frame_capture
        if enabled:
            if mgr is None:
                await ws.send_json({"type": "capture_status", "active": False,
                                    "error": "CAN not connected"})
                return
            mgr.start()
            if ws not in self.capture_clients:
                self.capture_clients[ws] = mgr.subscribe()
            await ws.send_json({"type": "capture_status", "active": mgr.active,
                                "stats": mgr.stats()})
        else:
            q = self.capture_clients.pop(ws, None)
            if q is not None and mgr is not None:
                mgr.unsubscribe(q)
            # Note: transport capture itself cannot be stopped without a CAN
            # disconnect; we only stop forwarding to this client.
            await ws.send_json({"type": "capture_status", "active": False,
                                "forwarding": False})

    async def _receive_client_messages(self, ws: web.WebSocketResponse) -> None:
        async for msg in ws:
            try:
                if msg.type == web.WSMsgType.TEXT:
                    data = json.loads(msg.data)

                    if data.get("type") == "filter":
                        self.client_filters[ws] = {
                            "subject_ids": data.get("subject_ids"),
                            "node_ids": data.get("node_ids"),
                            "message_types": data.get("message_types"),
                        }
                        logger.debug(f"Updated filter for client: {self.client_filters[ws]}")

                        await ws.send_json({
                            "type": "filter_updated",
                            "filter": self.client_filters[ws]
                        })

                    elif data.get("type") == "ping":
                        await ws.send_json({"type": "pong"})

                    elif data.get("type") == "capture":
                        await self._handle_capture_message(ws, bool(data.get("enabled")))

                    else:
                        logger.warning(f"Unknown message type: {data.get('type')}")

                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
                    break

            except json.JSONDecodeError:
                logger.warning("Invalid JSON from client")
                await ws.send_json({"error": "Invalid JSON"})
            except Exception as e:
                logger.error(f"Error processing client message: {e}")
                break

    def _event_matches_filter(self, event: dict[str, Any], ws: web.WebSocketResponse) -> bool:
        filters = self.client_filters.get(ws, {})

        if not any(filters.values()):
            return True

        if filters.get("subject_ids"):
            if event.get("subject_id") not in filters["subject_ids"]:
                return False

        if filters.get("node_ids"):
            if event.get("publisher_node_id") not in filters["node_ids"]:
                return False

        if filters.get("message_types"):
            if event.get("message_type") not in filters["message_types"]:
                return False

        return True

    # ------------------------------------------------------------------
    # REST endpoints
    # ------------------------------------------------------------------

    async def _api_info(self, request: web.Request) -> web.Response:
        return web.json_response({
            "name": "UAVCAN Telemetry API",
            "version": "2.0",
            "endpoints": {
                "WebSocket": {
                    "url": "ws://localhost:8080/ws",
                    "description": "Real-time event streaming with optional filtering"
                },
                "REST": {
                    "/api": "API information (this endpoint)",
                    "/api/status": "Server status, CAN interface, available interfaces",
                    "/api/health": "Server health check",
                    "/api/can/connect": "POST - Connect to a CAN interface",
                    "/api/can/disconnect": "POST - Disconnect from CAN interface",
                    "/api/can/transport": "Transport-layer diagnostics (MTU, frame stats, bus state)",
                    "/api/can/capture": "Raw frame-capture ring-buffer snapshot (WS 'capture' message streams live)",
                    "/api/nodes": "Get info about all discovered nodes",
                    "/api/latest/subject/{subject_id}": "Get latest event for a subject",
                    "/api/latest/node/{node_id}": "Get latest events from a node",
                    "/api/identity-map": "Get unique_id to node_id mappings",
                    "/api/logs": "Get recent application logs",
                }
            },
        })

    async def _get_logs(self, request: web.Request) -> web.Response:
        if self.log_store is None:
            return web.json_response(
                {"error": "Log store is not configured"},
                status=503,
            )

        limit_raw = request.query.get("limit", "200")
        level = request.query.get("level")
        min_level = request.query.get("min_level")

        try:
            limit = int(limit_raw)
        except ValueError:
            return web.json_response(
                {"error": "Query parameter 'limit' must be an integer"},
                status=400,
            )

        try:
            logs = self.log_store.get_logs(limit=limit, level=level, min_level=min_level)
        except ValueError as ex:
            return web.json_response({"error": str(ex)}, status=400)
        except Exception as ex:
            logger.error("Error while reading logs: %s", ex, exc_info=True)
            return web.json_response({"error": str(ex)}, status=500)

        return web.json_response({
            "count": len(logs),
            "logs": logs,
        })

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

    async def _health_check(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "healthy" if self._running else "degraded",
            "can_status": "running" if self.session.is_running else "idle",
            "can_interface": self.session.can_interface,
            "connected_clients": len(self.clients),
            "server": {
                "host": self.host,
                "port": self.port,
            }
        })

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
        scanner.identity_map.remove_identity(uid)
        if self.session.event_logger:
            await self.session.event_logger.delete_node_data(uid)
        return web.json_response({"status": "ok"})

    async def _get_all_nodes(self, request: web.Request) -> web.Response:
        telemetry = self.session.telemetry
        if telemetry is not None:
            try:
                nodes_info = telemetry.get_all_nodes_info()
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
                nodes_info = await asyncio.to_thread(
                    _synthesize_nodes_from_recording,
                    self.session.event_logger.db_path, replay.recording_id,
                )
                return web.json_response(nodes_info)
            except Exception as e:
                logger.error(f"Error synthesising replay nodes: {e}", exc_info=True)
                return web.json_response({"error": str(e)}, status=500)

        return web.json_response({"node_count": 0, "nodes": {}})

    # ------------------------------------------------------------------
    # Recording replay
    # ------------------------------------------------------------------

    async def _replay_start(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        rec_id, err = _parse_int(body.get("recording_id"), "recording_id", 1, 2**31 - 1)
        if err:
            return err
        speed = float(body.get("speed", 1.0)) if body.get("speed") is not None else 1.0
        start_offset_s = float(body.get("start_offset_s", 0.0))
        try:
            status = await self.session.start_replay(rec_id, speed=speed,
                                                    start_offset_s=start_offset_s)
            return web.json_response(status, status=200)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=404)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)
        except Exception as e:
            logger.error(f"Replay start failed: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _replay_control(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        action = body.get("action")
        if self.session.replay is None and action != "stop":
            return web.json_response({"error": "No replay running"}, status=404)
        if action == "pause":
            self.session.replay.pause()
        elif action == "resume":
            self.session.replay.resume()
        elif action == "stop":
            await self.session.stop_replay()
            return web.json_response({"active": False}, status=200)
        else:
            return web.json_response({"error": f"Unknown action: {action}"}, status=400)
        return web.json_response(self.session.replay.status(), status=200)

    async def _replay_seek(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if self.session.replay is None:
            return web.json_response({"error": "No replay running"}, status=404)
        try:
            position_s = float(body.get("position_s"))
        except (TypeError, ValueError):
            return web.json_response({"error": "position_s must be a number"}, status=400)
        self.session.replay.seek(position_s)
        return web.json_response(self.session.replay.status(), status=200)

    async def _replay_speed(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if self.session.replay is None:
            return web.json_response({"error": "No replay running"}, status=404)
        try:
            speed = float(body.get("speed"))
        except (TypeError, ValueError):
            return web.json_response({"error": "speed must be a number"}, status=400)
        self.session.replay.set_speed(speed)
        return web.json_response(self.session.replay.status(), status=200)

    async def _replay_status(self, request: web.Request) -> web.Response:
        if self.session.replay is None:
            return web.json_response({"active": False}, status=200)
        return web.json_response(self.session.replay.status(), status=200)

    # ------------------------------------------------------------------
    # DSDL introspection
    # ------------------------------------------------------------------

    async def _dsdl_status(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        data = await asyncio.to_thread(self.dsdl_manager.get_status)
        return web.json_response(data)

    async def _dsdl_namespaces(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        data = await asyncio.to_thread(self.dsdl_manager.get_namespaces)
        return web.json_response(data)

    async def _dsdl_type_detail(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        full_name = request.match_info["full_name"]
        data = await asyncio.to_thread(self.dsdl_manager.get_type_detail, full_name)
        if data is None:
            return web.json_response({"error": f"Type not found: {full_name}"}, status=404)
        return web.json_response(data)

    async def _dsdl_create_namespace(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        namespace = body.get("namespace", "").strip()
        if not namespace:
            return web.json_response({"error": "namespace is required"}, status=400)
        try:
            data = await asyncio.to_thread(self.dsdl_manager.create_namespace, namespace)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(data, status=201)

    async def _dsdl_save_type(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        namespace = body.get("namespace", "").strip()
        type_name = body.get("type_name", "").strip()
        version = body.get("version", "").strip()
        source_text = body.get("source_text", "")
        port_id = body.get("fixed_port_id")
        overwrite = bool(body.get("overwrite", False))
        if not all([namespace, type_name, version, source_text]):
            return web.json_response({"error": "namespace, type_name, version, source_text are required"}, status=400)
        if overwrite:
            full_name = f"{namespace}.{type_name}.{version}"
            if self.dsdl_manager.is_compiled(full_name):
                return web.json_response(
                    {"error": f"Cannot edit '{full_name}': type is already compiled. Recompile or clear python_compiled_messages first."},
                    status=409,
                )
        try:
            data = await asyncio.to_thread(
                self.dsdl_manager.save_type, namespace, type_name, version, source_text, port_id, overwrite
            )
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(data, status=201)

    async def _dsdl_delete_type(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        full_name = request.match_info["full_name"]
        parts = full_name.split(".")
        if len(parts) < 4:
            return web.json_response({"error": "Invalid full name (expected namespace.Type.major.minor)"}, status=400)
        namespace = ".".join(parts[:-3])
        type_name = parts[-3]
        version = f"{parts[-2]}.{parts[-1]}"
        try:
            # Deletion removes the .dsdl source AND any matching compiled .py
            # output, so the type disappears from both the tree and the
            # runtime. The namespace's __init__.py is intentionally left to
            # the next recompile (no clean way to patch it in-place when
            # other types in the namespace might still depend on it).
            data = await asyncio.to_thread(
                self.dsdl_manager.delete_type, namespace, type_name, version
            )
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(data)

    async def _dsdl_list_custom_namespaces(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        data = await asyncio.to_thread(self.dsdl_manager.list_custom_namespaces)
        return web.json_response({"namespaces": data})

    async def _dsdl_compile(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        try:
            body = await request.json()
        except Exception:
            body = {}
        scope = body.get("scope", "all")
        if scope == "custom":
            data = await asyncio.to_thread(self.dsdl_manager.compile_custom)
        elif scope == "public":
            data = await asyncio.to_thread(self.dsdl_manager.compile_public)
        else:
            data = await asyncio.to_thread(self.dsdl_manager.compile_all)
        if data.get("ok") and hasattr(self.session, "rescan_registrations"):
            self.session.rescan_registrations()
        status = 200 if data.get("ok") else 422
        return web.json_response(data, status=status)
