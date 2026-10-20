"""Raw CAN logs: candump .log files written, listed, downloaded and played (see raw_log.py).

Handlers of the REST API, which WebSocketServer (websocket_server.py) inherits and routes to."""

import asyncio
from pathlib import Path
from typing import Optional

from aiohttp import web

from api_helpers import ApiHandlers, logger
from raw_log import list_logs, log_path, sidecar_path


class RawLogsApi(ApiHandlers):
    """The rawlogs part of WebSocketServer, which has the session and the rest these use.

    What touches the data folder's disk runs in a worker thread: on a slow or
    busy disk (a USB stick being written) a stat takes seconds, and the event
    loop, which runs the CAN session and every other request too, must not
    wait them out.
    """

    async def _get_raw_logs(self, request: web.Request) -> web.Response:
        return web.json_response(await asyncio.to_thread(self._listing))

    def _listing(self) -> dict:
        active = self.session.raw_logs.active
        return {
            "active": active.status() if active is not None else None,
            "logs": list_logs(self.session.raw_logs.folder),
        }

    async def _start_raw_log(self, request: web.Request) -> web.Response:
        try:
            log = await asyncio.to_thread(self.session.raw_logs.start)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)
        except Exception as e:
            logger.error(f"Failed to start raw log: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response(await asyncio.to_thread(log.status), status=201)

    async def _stop_raw_log(self, request: web.Request) -> web.Response:
        log = await asyncio.to_thread(self.session.raw_logs.stop)
        if log is None:
            return web.json_response({"error": "No raw log is running"}, status=409)
        return web.json_response(await asyncio.to_thread(log.status))

    def _raw_log_file(self, request: web.Request) -> Optional[Path]:
        path = log_path(self.session.raw_logs.folder, request.match_info["name"])
        return path if path is not None and path.is_file() else None

    async def _download_raw_log(self, request: web.Request) -> web.StreamResponse:
        path = await asyncio.to_thread(self._raw_log_file, request)
        if path is None:
            return web.json_response({"error": "No such raw log"}, status=404)
        return web.FileResponse(path, headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{path.name}"',
        })

    async def _delete_raw_log(self, request: web.Request) -> web.Response:
        path = await asyncio.to_thread(self._raw_log_file, request)
        if path is None:
            return web.json_response({"error": "No such raw log"}, status=404)
        active = self.session.raw_logs.active
        if active is not None and active.path == path:
            return web.json_response({"error": "Stop the raw log before deleting it"}, status=409)
        await asyncio.to_thread(_delete, path)
        return web.json_response({"deleted": path.name})

    async def _play_raw_log(self, request: web.Request) -> web.Response:
        """Connect to a saved raw log as if it were the bus; POST /api/can/disconnect stops it."""
        if await asyncio.to_thread(self._raw_log_file, request) is None:
            return web.json_response({"error": "No such raw log"}, status=404)
        try:
            body = await request.json() if request.can_read_body else {}
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        speed = body.get("speed", 1) if isinstance(body, dict) else 1
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0 <= speed <= 1000:
            return web.json_response({"error": "speed must be a number from 0 (as fast as possible) to 1000"},
                                     status=400)
        if self.session.is_running:
            return web.json_response({"error": "Disconnect CAN before playing a raw log"}, status=409)
        name = request.match_info["name"]
        try:
            await self.session.play_raw_log(name, float(speed))
        except Exception as e:
            logger.error(f"Failed to play raw log {name}: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"status": "running", "can_interface": f"rawlog:{name}",
                                  "can_fd": self.session.can_fd})


def _delete(path: Path) -> None:
    path.unlink()
    sidecar_path(path).unlink(missing_ok=True)
