#!/usr/bin/env python3

import asyncio
import json
import logging
import time
from typing import Optional, Set, Any
from aiohttp import web, WSCloseCode

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        session: Any,
        host: str = "0.0.0.0",
        port: int = 8080,
        log_store: Optional[Any] = None,
    ) -> None:
        self.session = session
        self.host = host
        self.port = port
        self.log_store = log_store

        # Client management
        self.clients: Set[web.WebSocketResponse] = set()
        self.client_filters: dict[web.WebSocketResponse, dict[str, Any]] = {}

        # App and runner
        self.app = web.Application(middlewares=[self._cors_middleware])
        self.runner: Optional[web.AppRunner] = None
        self._running = False

        self._setup_routes()

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
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
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
        self.app.router.add_post('/api/services/{node_id}/{service_id}/call', self._call_service)
        self.app.router.add_post('/api/can/connect', self._can_connect)
        self.app.router.add_post('/api/can/disconnect', self._can_disconnect)

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

    # ------------------------------------------------------------------
    # Service endpoints
    # ------------------------------------------------------------------

    async def _get_services(self, request: web.Request) -> web.Response:
        """Return service schema metadata for a node."""
        try:
            node_id = int(request.match_info['node_id'])
        except (ValueError, KeyError):
            return web.json_response({"error": "Invalid node_id"}, status=400)

        if not self.session.is_running:
            return web.json_response({"error": "CAN bus not connected"}, status=503)

        schema = self.session.telemetry.get_service_schema(node_id)
        if schema is None:
            return web.json_response({"error": f"Node {node_id} not found"}, status=404)
        return web.json_response(schema)

    async def _get_clients(self, request: web.Request) -> web.Response:
        """Return enriched client port info for a node."""
        try:
            node_id = int(request.match_info['node_id'])
        except (ValueError, KeyError):
            return web.json_response({"error": "Invalid node_id"}, status=400)

        if not self.session.is_running:
            return web.json_response({"error": "CAN bus not connected"}, status=503)

        info = self.session.telemetry.get_client_info(node_id)
        if info is None:
            return web.json_response({"error": f"Node {node_id} not found"}, status=404)
        return web.json_response(info)

    async def _call_service(self, request: web.Request) -> web.Response:
        """Invoke a service on a remote node and return the response."""
        try:
            node_id = int(request.match_info['node_id'])
            service_id = int(request.match_info['service_id'])
        except (ValueError, KeyError):
            return web.json_response({"error": "Invalid node_id or service_id"}, status=400)

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

        t0 = time.monotonic()
        try:
            response_str = await self.session.scanner.make_service_call(
                node_id, service_id, service_type, attributes
            )
            latency_ms = round((time.monotonic() - t0) * 1000)
            return web.json_response({
                "status": "ok",
                "latency_ms": latency_ms,
                "response": response_str,
            })
        except asyncio.TimeoutError:
            latency_ms = round((time.monotonic() - t0) * 1000)
            return web.json_response({
                "status": "timeout",
                "latency_ms": latency_ms,
                "error": f"Service {service_id} on node {node_id} timed out",
            })
        except ValueError as e:
            return web.json_response({"status": "error", "error": str(e)}, status=400)
        except Exception as e:
            latency_ms = round((time.monotonic() - t0) * 1000)
            logger.error(f"Service call failed: {e}", exc_info=True)
            return web.json_response({
                "status": "error",
                "latency_ms": latency_ms,
                "error": str(e),
            })

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
            telemetry = self.session.telemetry
            if telemetry is None:
                # CAN not connected — just handle client messages until disconnect
                await self._receive_client_messages(ws)
            else:
                queue = telemetry.subscribe(max_queue=100)

                consume_task = asyncio.create_task(self._consume_and_send(ws, queue))
                recv_task = asyncio.create_task(self._receive_client_messages(ws))
                metrics_task = asyncio.create_task(self._send_metrics_loop(ws))

                done, pending = await asyncio.wait(
                    [consume_task, recv_task, metrics_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        except Exception as e:
            logger.error(f"Error in WebSocket handler: {e}", exc_info=True)
        finally:
            self.clients.discard(ws)
            self.client_filters.pop(ws, None)
            if queue is not None and self.session.telemetry is not None:
                self.session.telemetry.unsubscribe(queue)
            logger.info(f"Client disconnected. Total clients: {len(self.clients)}")

        return ws

    async def _consume_and_send(self, ws: web.WebSocketResponse, queue: asyncio.Queue) -> None:
        while self._running and not ws.closed:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=5.0)

                if self._event_matches_filter(event, ws):
                    await ws.send_json(event)

            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error sending to client: {e}")
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
                    "/api/nodes": "Get info about all discovered nodes",
                    "/api/latest/subject/{subject_id}": "Get latest event for a subject",
                    "/api/latest/node/{node_id}": "Get latest events from a node",
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

        try:
            subject_id = int(request.match_info['subject_id'])
            event = telemetry.get_latest_subject(subject_id)

            if event:
                return web.json_response(event)
            else:
                return web.json_response(
                    {"error": f"No data for subject {subject_id}"},
                    status=404
                )
        except ValueError:
            return web.json_response({"error": "Invalid subject_id"}, status=400)
        except Exception as e:
            logger.error(f"Error in GET /api/latest/subject: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _get_latest_node(self, request: web.Request) -> web.Response:
        telemetry = self.session.telemetry
        if telemetry is None:
            return web.json_response({"error": "CAN not connected"}, status=503)

        try:
            node_id = int(request.match_info['node_id'])
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
        except ValueError:
            return web.json_response({"error": "Invalid node_id"}, status=400)
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

    async def _get_all_nodes(self, request: web.Request) -> web.Response:
        telemetry = self.session.telemetry
        if telemetry is None:
            return web.json_response({"node_count": 0, "nodes": {}})

        try:
            nodes_info = telemetry.get_all_nodes_info()
            return web.json_response(nodes_info)
        except Exception as e:
            logger.error(f"Error in GET /api/nodes: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)
