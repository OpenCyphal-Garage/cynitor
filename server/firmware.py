"""Firmware updates over Cyphal: the files, and the file server nodes read them from.

A node with a Cyphal bootloader (e.g. Zubax Kocherga) is told to update with
uavcan.node.ExecuteCommand BEGIN_SOFTWARE_UPDATE and a file name. It then
reads that file, in chunks, with uavcan.file.Read from the node that sent the
command, and a chunk shorter than a full one ends the file.

Only uavcan.file.Read is served, and only for the files in one folder:
pycyphal's own FileServer also serves Write and Modify, which would let any
node on the bus change files on this computer.

uavcan is imported where it is used: it exists only once the DSDL is compiled.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

FIRMWARE_DIR = "firmware"  # inside the data folder
MAX_FIRMWARE_BYTES = 32 * 1024 * 1024
# A plain file name: no folders, and short enough for uavcan.file.Path (255 bytes).
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
COMMAND_TIMEOUT = 2.0  # seconds a node has to answer the update command
# uavcan.node.ExecuteCommand's STATUS_* values, in order.
COMMAND_STATUS = ["success", "failure", "not authorized", "bad command",
                  "bad parameter", "bad state", "internal error"]


def firmware_path(folder: Path, name: str) -> Optional[Path]:
    """The file ``name`` names in ``folder``, or None if it is not a plain file name."""
    return folder / name if _NAME.match(name) else None


def list_firmware(folder: Path) -> list[dict]:
    """Uploaded firmware files, by name."""
    if not folder.is_dir():
        return []
    files = []
    for path in sorted(folder.iterdir()):
        if _NAME.match(path.name) and path.is_file():
            stat = path.stat()
            files.append({"name": path.name, "bytes": stat.st_size, "modified_unix": stat.st_mtime})
    return files


class FirmwareServer:
    """Serves uavcan.file.Read from ``folder`` on ``node``, and follows each update.

    ``updates`` maps a node-ID to its latest update: the file, its size, how
    far the node has read it, and a state: ``requested`` (the node accepted
    the command), ``reading``, or ``transferred`` (it read the last chunk;
    checking and starting the image is up to its bootloader).
    """

    CHUNK = 256  # uavcan.primitive.Unstructured.1.0 holds at most this many bytes

    def __init__(self, folder: Path) -> None:
        self._folder = folder
        self.updates: dict[int, dict] = {}

    def serve_on(self, node) -> None:
        """Answer uavcan.file.Read on ``node``, until the node closes."""
        import uavcan.file
        node.get_server(uavcan.file.Read_1_1).serve_in_background(self._serve_read)

    def begin(self, node_id: int, name: str, size: int) -> dict:
        now = time.time()
        self.updates[node_id] = {"file": name, "bytes": size, "read": 0, "state": "requested",
                                 "started_unix": now, "updated_unix": now}
        return self.updates[node_id]

    def read(self, node_id: int, name: str, offset: int) -> Optional[bytes]:
        """The chunk of ``name`` at ``offset`` for ``node_id``; None if there is no such file.

        OSError if it cannot be read.
        """
        path = firmware_path(self._folder, name.lstrip("/"))
        if path is None or not path.is_file():
            return None
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read(self.CHUNK)
        update = self.updates.get(node_id)
        if update is not None and update["file"] == path.name:  # else a node reading on its own
            update["read"] = max(update["read"], offset + len(data))
            update["state"] = "transferred" if len(data) < self.CHUNK else "reading"
            update["updated_unix"] = time.time()
        return data

    async def _serve_read(self, request, meta):
        from uavcan.file import Error_1_0 as Error, Read_1_1 as Read
        from uavcan.primitive import Unstructured_1_0 as Unstructured
        name = request.path.path.tobytes().decode("utf-8", errors="replace")
        try:
            data = self.read(meta.client_node_id, name, request.offset)
        except OSError as exc:
            logger.warning("Firmware %s: could not read it for node %s: %s", name, meta.client_node_id, exc)
            return Read.Response(error=Error(Error.IO_ERROR))
        if data is None:
            return Read.Response(error=Error(Error.NOT_FOUND))
        return Read.Response(data=Unstructured(data))

async def send_update_command(node, node_id: int, name: str) -> Optional[int]:
    """Tell ``node_id`` to update from the file ``name``; its status, or None if it did not answer."""
    import uavcan.node
    command = uavcan.node.ExecuteCommand_1_3
    client = node.make_client(command, node_id)
    client.response_timeout = COMMAND_TIMEOUT
    try:
        result = await client.call(command.Request(
            command=command.Request.COMMAND_BEGIN_SOFTWARE_UPDATE, parameter=name))
    finally:
        client.close()
    return None if result is None else int(result[0].status)
