# PyCyphal WebSocket Telemetry Server

Production-grade real-time telemetry streaming system for UAVCAN networks.

## Architecture

```
UAVCAN Network
      ↓
ScannerNode (reads CAN messages)
      ↓
message_queue (event buffer)
      ↓
TelemetryManager (pub-sub router)
      ├→ WebSocket Server (live streaming)
      ├→ EventLogger (SQLite persistence)
      └→ CLI/Debug Tools
```

## Features

✅ **Real-time Streaming** - WebSocket support for multiple concurrent clients  
✅ **Event Filtering** - Filter by subject_id, node_id, or message_type  
✅ **REST API** - Query latest events via HTTP  
✅ **Log API** - Retrieve recent application logs with log-level filtering  
✅ **Optional Persistence** - SQLite logging for historical replay  
✅ **Browser Origin Policy** - The API accepts browser requests only from its own dashboard and from pages served on this machine  
✅ **Clean Shutdown** - Graceful WebSocket disconnect and logger queue flush  
✅ **Allocator Guard** - Reuses external allocator if present, otherwise starts local allocator and re-checks every 10s
✅ **CAN Health Monitoring** - Detects CAN bus faults (BUS-OFF, ERROR-PASSIVE, interface disappearance on SocketCAN; a failing or unplugged adapter otherwise) and auto-disconnects
✅ **Bus Load Monitoring** - Real-time CAN bus utilization via `canbusload` subprocess on SocketCAN, or counted from the forwarded frames for other adapters (Classic CAN and CAN FD alike), streamed to clients via WebSocket  
✅ **Register Access** - Read and write Cyphal node registers via REST API  
✅ **Offline Node Detection** - Tracks node disappearance with `last_seen` timestamps and stale state handling  
✅ **Node History** - Lifecycle event tracking (health changes, mode changes, service calls) with 30-day retention  
✅ **Service Call History** - Persistent service call log with response bodies, queryable by service ID  
✅ **Node Identity Map** - Tracks stable hardware identity (unique_id) across node_id re-allocations, with state migration  

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

Key dependencies:
- `pycyphal` - UAVCAN protocol library
- `aiohttp` - Async HTTP/WebSocket server
- CORS and the origin policy are handled by built-in middleware (no extra dependency)

### 2. Run the System

Start directly:

```bash
python3 main.py --can vcan0
```

Or omit `--can` and select via startup API:

```bash
python3 main.py
```

In this mode, the server starts on port 8080 immediately. Use the REST API to connect:
- `GET /api/status` (lists available CAN interfaces and connection state)
- `POST /api/can/connect` with JSON body `{"interface":"vcan0"}`

Once connected, CAN telemetry begins. Disconnect with `POST /api/can/disconnect`.

By default the server binds to `127.0.0.1` (localhost only). To expose it on the network — for example to reach the dashboard from another host on a trusted LAN — pass `--bind`:

```bash
python3 main.py --can vcan0 --bind 0.0.0.0
```

When exposed on a non-loopback address, require a bearer token by setting the `CYNITOR_AUTH_TOKEN` environment variable:

```bash
CYNITOR_AUTH_TOKEN=$(openssl rand -hex 24) python3 main.py --can vcan0 --bind 0.0.0.0
```

With the variable set, every REST and WebSocket request outside `/api/health` must present the token. Static dashboard assets are exempt: a browser has to load the page before it can prompt for a token, and the markup is not secret. Concretely, `/ws`, `/api` and everything under `/api/` are protected; every other path is served open.

Protected requests must present the token as:
- REST: `Authorization: Bearer <token>` header
- WebSocket: `?token=<token>` query parameter (browsers can't attach custom headers on WS handshakes)

Unauthenticated requests get `HTTP 401 {"error": "missing or invalid token"}`. With the variable unset the server runs open — same behaviour as before this option existed. The frontend prompts the user to paste the token on the first 401 and stores it in `localStorage` under `cynitor.auth.token`.

When a terminal is attached, the server prints the token once at startup so it can be copied into the dashboard. It is written straight to stderr rather than logged: the log buffer is served through `/api/logs` and rendered in the dashboard's log panel, and a service manager captures stdout into the system journal, so logging it would scatter copies. Runs without a terminal, which is every supervised run, print nothing.

#### Browser origin policy

The API commands nodes on the bus, and any website a user visits could otherwise send requests to `localhost:8080` through their browser. So requests to `/ws`, `/api` and `/api/...` are refused with `HTTP 403 {"error": "cross-origin request refused"}` when their `Origin` header names any page other than:

- the dashboard this server serves itself (the `Origin` matches the `Host` it was reached at), or
- a page served from this machine (`localhost`, `127.0.0.1` or `[::1]`, any port), such as the frontend dev server on `:5500`.

Requests without an `Origin` header (curl, scripts, other non-browser clients) are not affected. `Origin: null`, which sandboxed frames and `file://` pages send, is refused. Allowed browser origins get `Access-Control-Allow-Origin` echoed back; nobody gets `*`.

While the server listens on loopback only (the default `--bind 127.0.0.1`), the `Host` header must also name loopback. This stops DNS rebinding, where a site re-points its own name at `127.0.0.1` so its requests look same-origin. Behind a reverse proxy, the proxy must pass the browser's original `Host` on (nginx: `proxy_set_header Host $host;`) and the server must not be bound to loopback only; set a token.

This works alongside the token, not instead of it: set `CYNITOR_AUTH_TOKEN` whenever the server is reachable from other machines.


### Serving the dashboard

When a `website/` directory is present next to the server (a source checkout, or bundled inside the frozen binary), the dashboard is served from the same port as the API:

| Route | Serves |
|-------|--------|
| `GET /` | `website/index.html` |
| `GET /config.js` | Generated: `window.__CYNITOR = {"apiBase": "<this request's origin>"}` |
| `GET /<path>` | Any other file under `website/` |

These are registered after the API routes, so `/api/*` and `/ws` always win over the catch-all static mount.

They are sent with `Cache-Control: no-cache`, so a browser checks for a newer version before reusing a cached file (an unchanged file costs a `304`). After an upgrade the dashboard is never a stale mix of old and new files. API responses are unaffected.

Start the server with `--no-frontend` to omit them entirely, for deployments where something other than the dashboard consumes the API. Those three routes then return `404` and everything else is unchanged.

`config.js` is how a browser-served dashboard learns its API address. The checked-in `website/config.js` is an empty placeholder, which is what a separate static file server on port 5500 delivers, leaving the address field at its built-in default. Served from the backend, the generated version wins and points the page at the origin it was fetched from, so no per-client configuration is needed.

### 3. Runtime Environment

At startup, Python setup runs automatically and configures:
- `UAVCAN__CAN__IFACE=socketcan:<selected_iface>`, or the python-can spec as given (e.g. `gs_usb:0`)
- `UAVCAN__CAN__BITRATE=<n> <n>` for non-SocketCAN adapters (cleared for SocketCAN)
- `UAVCAN__NODE__ID` (auto-assigned if not already set)
- `CYPHAL_PATH` and `PYCYPHAL_PATH` (DSDL locations)

### 4. Allocator Behavior

Before scanner startup, allocator procedure runs automatically:
- Detects allocator heartbeat on node-ID `1`
- If allocator exists, uses it (does not start local allocator)
- If not found, starts local allocator service
- While relying on external allocator, checks every 10 seconds and starts local allocator if external allocator disappears

```bash
python3 main.py
```

Output:
```
============================================================
SERVER RUNNING
============================================================
Bound to:    127.0.0.1:8080
REST API:    http://localhost:8080/api/
Health:      http://localhost:8080/api/health
Status:      http://localhost:8080/api/status
Mode:        selection  (waiting for the UI or POST /api/can/connect)
------------------------------------------------------------
Startup options:
  --can <iface>    attach to a CAN interface at startup (e.g. vcan0, can0, gs_usb:0, pcan:PCAN_USBBUS1)
  --bitrate <n>    bus speed in bit/s; required with --can for any adapter except SocketCAN
  --data-bitrate <n>  CAN FD data-phase speed; opens PCAN/Kvaser/Vector/IXXAT as CAN FD
  --bind <host>    bind HTTP server to <host>  (default 127.0.0.1; 0.0.0.0 to expose on the network)
  --port <n>       listen on <n> instead of 8080
  --data-dir <dir> keep history, recordings and node-IDs in <dir>
  --recompile      force DSDL recompilation via nnvg
  --help           full reference
============================================================
```

In direct mode (`--can <iface>`) the `Mode:` line reads `direct (attached to <iface> at startup)` and the block ends with a hint pointing back at selection mode. When `--bind 0.0.0.0` is passed, an additional `WARNING` line is emitted before the tips block to make the network-exposed posture obvious.

## Usage

### WebSocket Client (JavaScript/Browser)

**Connect:**
```javascript
const ws = new WebSocket('ws://localhost:8080/ws');

ws.onmessage = (event) => {
    const telemetry = JSON.parse(event.data);
    console.log(`Subject ${telemetry.subject_id}: ${telemetry.message_type}`);
    console.log(`Rate: ${telemetry.rate}Hz`);
    console.log(`Attributes:`, telemetry.attributes);
};
```

**Filter Events:**
```javascript
// Receive only Heartbeat messages from nodes 1 and 2
ws.send(JSON.stringify({
    type: 'filter',
    node_ids: [1, 2],
    message_types: ['Heartbeat_1_0']
}));

// Or filter by subjects only
ws.send(JSON.stringify({
    type: 'filter',
    subject_ids: [7509, 7510]
}));

// Ping to keep connection alive
ws.send(JSON.stringify({ type: 'ping' }));

// Subscribe to the raw frame-capture stream (Debugging view). Off by default.
// Enabling starts transport-level capture if not already active — see the note
// below. Send enabled:false to stop receiving frames on this connection.
ws.send(JSON.stringify({ type: 'capture', enabled: true }));
```

> **Frame capture is sticky and changes bus behaviour.** pycyphal implements
> capture by reconfiguring the acceptance filter to accept all frames and
> forcing loopback on every outgoing frame. It cannot be stopped without closing
> the transport (a CAN disconnect), and it adds bus/CPU overhead. It is therefore
> opt-in: only clients that send `{type:'capture',enabled:true}` receive frames,
> and `enabled:false` only stops *forwarding* to that client — the transport tap
> stays active until disconnect.

**Server Messages:**

Telemetry events (filtered per client):
```json
{
    "subject_id": 7509,
    "timestamp": "2026-03-13T10:30:45",
    "timestamp_unix": 1741949445.123,
    "rate": 1.0,
    "subject_rate": 5.0,
    "message_type": "Heartbeat_1_0",
    "publisher_node_id": 42,
    "payload_bytes": 7,
    "attributes": [
        {"attribute": "uptime", "value": 12345, "unit": "s"},
        {"attribute": "health", "value": "NOMINAL"},
        {"attribute": "mode", "value": "OPERATIONAL"},
        {"attribute": "vssc", "value": 42}
    ]
}
```

`rate` is this publisher's message rate on the subject, in Hz (one decimal, over the last 10 s). `subject_rate` is the subject's total over all its publishers: with five nodes publishing Heartbeat at 1 Hz, each event carries `rate` 1.0 and `subject_rate` 5.0. Events recorded before `subject_rate` existed lack it, and there `rate` was the subject total.

`timestamp_unix` is when the transfer was received as stamped by the transport (for SocketCAN, the kernel's receive timestamp), not when the backend got round to processing it.

`payload_bytes` is the size, in bytes, of the received transfer's serialized payload (sum of `transfer.fragmented_payload` fragment lengths). It is `null` if the transport did not expose the fragmented payload (best-effort field).

Metrics (sent to all clients every 1 second):
```json
{
    "type": "metrics",
    "bus_utilization": 3.0
}
```

Filter acknowledgement (sent in response to a client `filter` message):
```json
{
    "type": "filter_updated",
    "subject_ids": [7509, 7510],
    "node_ids": [1, 2],
    "message_types": ["Heartbeat_1_0"]
}
```

Each field echoes the active filter for the client. Empty / omitted dimensions mean "match anything in that dimension".

Pong (sent in response to a client `ping` message):
```json
{ "type": "pong" }
```

Capture status (sent in response to a client `capture` message):
```json
{ "type": "capture_status", "active": true, "stats": { "captured": 0, "rx": 0, "tx": 0, "cyphal": 0, "foreign": 0, "dropped": 0 } }
```
`active` reflects whether transport-level capture is running. When enabling
fails because no CAN session exists, the message carries `"active": false` and an
`"error"` field. A disable reply carries `"active": false, "forwarding": false`.

Raw frame batch (sent only to clients that opted into capture; batched ~every
120 ms to bound message rate):
```json
{
    "type": "can_frame",
    "stats": { "captured": 1024, "rx": 1000, "tx": 24, "cyphal": 1010, "foreign": 14, "dropped": 0 },
    "frames": [
        {
            "t": 12345.678, "ts": 1741949445.123, "dir": "rx",
            "id": "0x107D552A", "ext": true, "dlc": 8, "data": "01 02 03 04 05 06 07 E5",
            "cyphal": true, "priority": "NOMINAL", "src": 42, "dst": null,
            "kind": "msg", "port": 7509, "transfer_id": 5, "start": true, "end": true
        },
        { "t": 12345.679, "ts": 1741949445.124, "dir": "rx", "id": "0x00000123", "ext": false, "dlc": 2, "data": "AA BB", "cyphal": false }
    ]
}
```
`dir` is `tx`/`rx` (TX = forced-loopback of our own frames). `dlc` is the data
length in bytes (0–64), not the DLC code. For Cyphal frames,
`kind` is `msg`/`req`/`resp` and `port` is the subject- or service-ID; non-Cyphal
("foreign") frames carry only the raw fields with `cyphal: false`.

Protocol errors (sent when the client sends a frame the server cannot parse):
```json
{ "error": "Invalid JSON" }
```

These do not include a `type` field; the bare `error` key signals a protocol-level problem rather than a domain event. Subsequent frames are still accepted on the same connection.

### REST API

**Get server status (connection state, available interfaces, bus load):**
```bash
curl http://localhost:8080/api/status
```

Response:
```json
{
    "status": "running",
    "can_interface": "vcan0",
    "can_bitrate": null,
    "can_data_bitrate": null,
    "can_fd": false,
    "available_interfaces": ["vcan0", "can0"],
    "available_adapters": [
        {"interface": "vcan0", "label": "vcan0 (SocketCAN)", "needs_bitrate": false, "supports_fd": false},
        {"interface": "pcan:PCAN_USBBUS1", "label": "PEAK PCAN_USBBUS1", "needs_bitrate": true, "supports_fd": true}
    ],
    "bus_utilization": 3.0,
    "dropped": {"scanner": 0, "logger": 0, "clients": 12},
    "last_error": null
}
```

`dropped` counts decoded messages discarded since the CAN connection opened because a queue was full: `scanner` before reaching anything, `logger` missing from the 24 h history and from recordings, `clients` missing from some dashboard's live view (each open dashboard has its own 100-event queue). `null` when not connected to CAN. The dashboard shows the total next to the CAN message rate.

`status` is `"running"` when connected to CAN, `"idle"` otherwise. `last_error` contains the error message if CAN was auto-disconnected due to a bus fault. `can_bitrate` is the bitrate Cynitor opened the adapter at, or `null` for SocketCAN, whose bitrate the kernel sets. `can_data_bitrate` is the CAN FD data-phase bitrate it opened the adapter with, or `null` (Classic CAN, or SocketCAN). `can_fd` says whether the session runs Cyphal/CAN FD; for SocketCAN that follows the interface's own setup.

`available_interfaces` lists SocketCAN names only, as before. `available_adapters` lists everything the dashboard can offer: SocketCAN interfaces, adapters of vendor drivers python-can can enumerate (PEAK, Kvaser, Vector, IXXAT) and, off Linux, candleLight (`gs_usb`) and known slcan adapters. Pass an entry's `interface` to `POST /api/can/connect`, with a `bitrate` when `needs_bitrate` is true. `supports_fd` says whether a session on it can run CAN FD: for an adapter Cynitor opens itself (`needs_bitrate` true), that it can be opened as CAN FD when given a `data_bitrate`; for SocketCAN, that the interface is set up for CAN FD, which Cynitor then uses without being asked. The list is rescanned at most every 10 seconds, and not at all while connected.

**List CAN adapters:**
```bash
curl "http://localhost:8080/api/can/adapters?refresh=1"
```

Returns `{"adapters": [...]}`, entries as in `available_adapters`. `refresh=1` rescans now instead of serving a list up to 10 seconds old (ignored while connected).

**Connect to a CAN interface:**
```bash
curl -X POST http://localhost:8080/api/can/connect \
  -H 'Content-Type: application/json' \
  -d '{"interface":"vcan0"}'
```

Response (success):
```json
{"status": "running", "can_interface": "vcan0", "can_fd": false}
```

`interface` is either a SocketCAN name, which must be one of `available_interfaces`, or a python-can spec such as `gs_usb:0` or `pcan:PCAN_USBBUS1`, which is opened without that check. `bitrate` (integer, 1–1000000 bit/s) is the bus speed, required for every adapter except SocketCAN, which ignores it. It may be left out only if the server was started with `--bitrate`, which then applies.

`data_bitrate` (integer, 1–12000000 bit/s, optional) opens the adapter as CAN FD with that data-phase bitrate; left out, the session is Classic CAN unless the server was started with `--data-bitrate`. Only adapters whose `supports_fd` is true take it (PEAK, Kvaser, Vector, IXXAT); SocketCAN ignores it and runs CAN FD when the interface is set up for it. `can_fd` in the response says which it became.

```bash
curl -X POST http://localhost:8080/api/can/connect \
  -H 'Content-Type: application/json' \
  -d '{"interface":"pcan:PCAN_USBBUS1","bitrate":500000,"data_bitrate":2000000}'
```

```bash
curl -X POST http://localhost:8080/api/can/connect \
  -H 'Content-Type: application/json' \
  -d '{"interface":"gs_usb:0","bitrate":250000}'
```

Returns `409` if already connected, `400` if a SocketCAN name is unknown, `bitrate` is missing or invalid, or `data_bitrate` is invalid or given for an adapter that cannot run CAN FD.

**Disconnect from CAN:**
```bash
curl -X POST http://localhost:8080/api/can/disconnect
```

Response:
```json
{"status": "idle", "available_interfaces": ["vcan0", "can0"], "available_adapters": [...]}
```

Returns `409` if not connected.

**Transport-layer diagnostics (Debugging view):**
```bash
curl http://localhost:8080/api/can/transport
```

Read-only snapshot of the CAN transport *below* the DSDL/application layer —
used by the Debugging tab. When no CAN session is active it returns
`{"connected": false}` (HTTP 200) so the UI can render an idle state.

Response (connected):
```json
{
  "connected": true,
  "interface": "vcan0",
  "protocol": {"mtu": 7, "transfer_id_modulo": 32, "max_nodes": 128, "is_fd": false},
  "statistics": {
    "in_frames": 1024, "in_frames_cyphal": 1000, "in_frames_cyphal_accepted": 980,
    "in_frames_errored": 0, "in_frames_loopback": 12,
    "out_frames": 40, "out_frames_timeout": 0, "out_frames_loopback": 12,
    "media_acceptance_filtering_efficiency": 0.96, "lost_loopback_frames": 0
  },
  "capture_active": false,
  "link": {
    "operstate": "up", "state": "ERROR-ACTIVE", "bitrate": 500000, "dbitrate": null,
    "berr_tx": 0, "berr_rx": 0, "restart_ms": 0,
    "restarts": 0, "bus_errors": 0, "arbitration_lost": 0,
    "error_warning": 0, "error_passive": 0, "bus_off": 0
  },
  "bus_utilization": 12.0
}
```

`protocol` / `statistics` come from pycyphal's transport (`mtu` is the
single-frame payload limit: 7 = Classic CAN, up to 63 = CAN FD). `link` is
best-effort controller state parsed from `ip -details -statistics link show`;
fields are `null` on virtual interfaces (vcan) or where the controller does not
report them. For an adapter Cynitor opens itself, `link` holds what the CAN hub
counts instead: `bitrate`, `dbitrate` (the CAN FD data bitrate, or `null`),
`adapter_frames_in`, `adapter_frames_out`, `adapter_send_failures` and
`adapter_error_frames` (error frames, if the adapter's driver reports them).
The Debugging view polls this endpoint at ~1 Hz while active.

**Raw frame-capture snapshot (Debugging view frame monitor):**
```bash
curl 'http://localhost:8080/api/can/capture?limit=500'
```

Returns the recent-frame ring buffer plus capture counters — used to backfill
the frame monitor on open / after reconnect. Live frames stream over the
WebSocket `can_frame` message (see above); this endpoint does not start capture.

```json
{
  "active": true,
  "stats": {"captured": 1024, "rx": 1000, "tx": 24, "cyphal": 1010, "foreign": 14, "dropped": 0},
  "frames": [ /* same per-frame shape as the can_frame stream, oldest→newest */ ]
}
```

When no CAN session exists: `{"active": false, "stats": null, "frames": []}`.
`limit` is clamped to 2000 (default 500).

**DSDL status (does not require CAN connection):**
```bash
curl http://localhost:8080/api/dsdl/status
```

Response:
```json
{
    "paths": [{"path": "...", "label": "Public regulated types", "source": "regulated"}],
    "compiled": true,
    "last_compiled": 1773832423.0,
    "last_public_compiled": 1773832423.0,
    "last_custom_compiled": null,
    "source_types": 257,
    "custom_types": 0
}
```

`last_public_compiled` covers the `uavcan/` and `reg/` namespaces only; `last_custom_compiled` covers every other top-level namespace under `python_compiled_messages/` (i.e. user-created custom types). `last_compiled` is the max of both, kept for backward compatibility.

**DSDL namespace tree:**
```bash
curl http://localhost:8080/api/dsdl/namespaces
```

Returns a nested tree of namespaces with type entries. Each type includes `short_name`, `full_name`, `version`, `kind` (`"message"` or `"service"`), `fixed_port_id`, `source` (`"regulated"` or `"custom"`), and `compiled` (`true` if a corresponding `.py` exists in `python_compiled_messages/`).

**DSDL type detail:**
```bash
curl http://localhost:8080/api/dsdl/type/uavcan.node.Heartbeat.1.0
```

Returns full type info: fields (with types), constants, dependencies, compilation status, and raw `.dsdl` source text. For services, fields are split into `request` and `response`.

**Create custom namespace:**
```bash
curl -X POST http://localhost:8080/api/dsdl/custom/namespace \
  -H 'Content-Type: application/json' \
  -d '{"namespace": "myapp.sensors"}'
```
Returns `201` with `{"namespace": "myapp.sensors", "path": "..."}`.

**List custom namespaces:**
```bash
curl http://localhost:8080/api/dsdl/custom/namespaces
```
Returns `{"namespaces": ["myapp", "myapp.sensors"]}`.

**Save custom DSDL type:**
```bash
curl -X POST http://localhost:8080/api/dsdl/custom/type \
  -H 'Content-Type: application/json' \
  -d '{"namespace": "myapp.sensors", "type_name": "Temperature", "version": "1.0", "source_text": "float32 celsius\nfloat32 fahrenheit\n@sealed", "fixed_port_id": null}'
```
Returns `201` with `{"full_name": "myapp.sensors.Temperature.1.0", "path": "..."}`.

Pass `"overwrite": true` to replace an existing custom type's source. Only allowed while the type is **not compiled** — the server returns `409` if the type has already been compiled.

**Delete custom DSDL type:**
```bash
curl -X DELETE http://localhost:8080/api/dsdl/custom/type/myapp.sensors.Temperature.1.0
```
Returns `200` with `{"full_name": "myapp.sensors.Temperature.1.0", "deleted": true}`.
Returns `404` if the source file is missing, or `400` on a malformed name. Deleting also removes the type's compiled code.

**Compile DSDL types:**
```bash
# Compile custom namespaces only
curl -X POST http://localhost:8080/api/dsdl/compile \
  -H 'Content-Type: application/json' \
  -d '{"scope": "custom"}'

# Recompile public (regulated) types only — reg + uavcan
curl -X POST http://localhost:8080/api/dsdl/compile \
  -H 'Content-Type: application/json' \
  -d '{"scope": "public"}'

# Recompile everything (regulated + custom)
curl -X POST http://localhost:8080/api/dsdl/compile \
  -H 'Content-Type: application/json' \
  -d '{"scope": "all"}'
```
Returns `200` with `{"ok": true}` on success, or `422` with `{"ok": false, "errors": [...]}`.

Compilation runs inside the server. In the packaged binaries the public types are built in: `"public"` is refused and `"all"` compiles the custom types; `GET /api/dsdl/status` reports this as `"public_compilable": false`. Custom types and their compiled code are kept in the data folder (`dsdl/custom`, `dsdl/compiled`).

**Get latest event for a subject:**
```bash
curl http://localhost:8080/api/latest/subject/7509
```

Response:
```json
{
    "subject_id": 7509,
    "timestamp": "2026-03-13T10:30:45",
    ...
}
```

**Get all latest events from a node:**
```bash
curl http://localhost:8080/api/latest/node/42
```

Response:
```json
{
    "node_id": 42,
    "events": {
        "7509": {...},
        "7510": {...}
    }
}
```

If the node is discovered but no live telemetry has been cached yet, the endpoint returns `404`:

```json
{
    "error": "No telemetry cached for node 42",
    "known_node": true
}
```

**Get information about all discovered nodes on CAN network:**
```bash
curl http://localhost:8080/api/nodes
```

Response:
```json
{
    "node_count": 2,
    "nodes": {
        "37": {
            "node_id": 37,
            "unique_id": [215, 79, 139, 105, 174, 196, 24, 37, 77, 69, 95, 193, 150, 10, 89, 50],
            "unique_id_hex": "d74f8b69aec418254d455fc1960a5932",
            "uptime": 8285,
            "has_disappeared": false,
            "has_responded_to_getinfo": true,
            "name": "org.dontpanic.pycyphal.examples.publisher_si",
            "software_version": {
                "major": 1,
                "minor": 0
            },
            "publishers": [1235, 1236, 7509, 7510],
            "subscribers": [7509],
            "clients": [],
            "servers": [384, 385, 430],
            "last_seen": ["2026-05-04T12:30:45.123456"]
        },
        "74": {
            ...
        },
        "uid:ab12cd34ef56789000000000deadbeef": {
            "node_id": null,
            "last_node_id": 37,
            "unique_id": [171, 18, 205, 52, 239, 86, 120, 144, 0, 0, 0, 0, 222, 173, 190, 239],
            "unique_id_hex": "ab12cd34ef56789000000000deadbeef",
            "uptime": 1200,
            "has_disappeared": true,
            "has_responded_to_getinfo": true,
            "name": "org.example.displaced_node",
            "software_version": {"major": 1, "minor": 0},
            "publishers": [1235],
            "subscribers": [],
            "clients": [],
            "servers": [384, 430],
            "last_seen": ["2026-05-04T11:00:00.000000"],
            "_ghost": true
        }
    }
}
```

Ghost entries (key `uid:<hex>`) represent devices whose `unique_id` is known but whose node_id slot was taken by another device. `last_node_id` is the most recent node_id the device held. Ghost data comes from the last snapshot before displacement.

**Get identity map (unique_id to node_id mappings):**
```bash
curl http://localhost:8080/api/identity-map
```

Response:
```json
{
    "mappings": {
        "d74f8b69aec418254d455fc1960a5932": {
            "current_node_id": 37,
            "previous_node_ids": [42, 99],
            "last_seen": 1741949445.123,
            "name": "org.example.my_node"
        },
        "ab12cd34ef56789000000000deadbeef": {
            "current_node_id": null,
            "previous_node_ids": [5, 10],
            "last_seen": 1741940000.0,
            "name": null
        }
    }
}
```

Each key is a hardware `unique_id` hex string. `current_node_id` is `null` if the device is offline and its slot was taken by another device. `previous_node_ids` lists all past node_id assignments. `last_seen` is a Unix timestamp of the last identity registration. `name` is the node name from GetInfo (if available). The identity map is persisted to SQLite and restored across server restarts.

**Delete a ghost identity (remove detached node data):**
```bash
curl -X DELETE http://localhost:8080/api/identity/d74f8b69aec418254d455fc1960a5932
```

Response:
```json
{"status": "ok"}
```

Removes the identity from the in-memory map and deletes its persisted snapshot from SQLite. Returns `503` if CAN is not connected.

**Health check:**
```bash
curl http://localhost:8080/api/health
```

Response:
```json
{
    "status": "healthy",
    "can_status": "running",
    "can_interface": "vcan0",
    "connected_clients": 3,
    "server": {
        "host": "127.0.0.1",
        "port": 8080
    }
}
```

**Get recent application logs (all levels):**
```bash
curl "http://localhost:8080/api/logs?limit=200"
```

**Filter logs by exact level:**
```bash
curl "http://localhost:8080/api/logs?level=ERROR&limit=100"
```

**Filter logs by minimum level:**
```bash
curl "http://localhost:8080/api/logs?min_level=WARNING&limit=100"
```

Response:
```json
{
    "count": 2,
    "logs": [
        {
            "timestamp": "2026-03-14T15:52:01.123456+00:00",
            "logger": "websocket_server",
            "level": "ERROR",
            "message": "Error in GET /api/latest/node: ..."
        }
    ]
}
```

**Get service schema for a node (types, request fields):**
```bash
curl http://localhost:8080/api/services/37
```

Response:
```json
{
    "node_id": 37,
    "services": [
        {
            "service_id": 430,
            "name": "GetInfo_1_0",
            "namespace": "uavcan.node",
            "full_type": "uavcan.node.GetInfo_1_0",
            "callable": true,
            "request_fields": []
        },
        {
            "service_id": 384,
            "name": "Access_1_0",
            "namespace": "uavcan.register",
            "full_type": "uavcan.register.Access_1_0",
            "callable": true,
            "request_fields": [
                {"name": "name", "type": "uavcan.register.Name_1_0", "kind": "composite", "fields": [...]},
                {"name": "value", "type": "uavcan.register.Value_1_0", "kind": "composite", "fields": [...]}
            ]
        }
    ]
}
```

Returns `404` if the node is not found, `503` if CAN is not connected.

**Invoke a service on a node:**
```bash
curl -X POST http://localhost:8080/api/services/37/430/call \
  -H 'Content-Type: application/json' \
  -d '{"attributes": {}}'
```

Response (success):
```json
{
    "status": "ok",
    "latency_ms": 23,
    "response": "{\n  \"protocol_version\": {\n    \"major\": 1,\n    \"minor\": 0\n  },\n  ...\n}"
}
```

`response` is the node's answer as JSON text (pycyphal's `to_builtin` form: numbers, strings, and objects for composite fields), so it reads well and a script can `json.loads` it.

Request attributes are keyed by field name: `{"value": <v>}` for a plain field, and for a composite field `{"type": "<its type>", "value": {"<sub-field>": <v>, ...}}` with each sub-field to set; a single `value` there sets the composite's first field, as single-field wrappers such as `uavcan.primitive.String.1.0` need.

`uavcan.node.ExecuteCommand` (service 435) is callable on every node that advertises it, registers or not. Restarting a node, for example (65535 is `COMMAND_RESTART`; the dashboard offers Restart and Factory reset as buttons):

```bash
curl -X POST http://localhost:8080/api/services/37/435/call \
  -H 'Content-Type: application/json' \
  -d '{"attributes": {"command": {"value": 65535}}}'
```

Response (timeout, HTTP `504`):
```json
{
    "status": "timeout",
    "latency_ms": 5000,
    "error": "Service 430 on node 37 timed out"
}
```

Response (service not found, HTTP `404`):
```json
{
    "status": "error",
    "error": "Service 430 not found on node 37"
}
```

Response (generic backend exception, HTTP `500`):
```json
{
    "status": "error",
    "latency_ms": 42,
    "error": "<exception message>"
}
```

Returns `400` for invalid request body or attribute validation errors, `404` if the service is not found on the node, `500` if the backend hit an unexpected error invoking the service, `503` if CAN is not connected, `504` for timeouts. The body always carries a `status` field whose values are one of `"ok"`, `"timeout"`, or `"error"`, so a client can switch on the body shape regardless of HTTP status. `latency_ms` is omitted only in the `404` case (no call was attempted).

**Get client ports for a node (with type names and server cross-references):**
```bash
curl http://localhost:8080/api/clients/37
```

Response:
```json
{
    "node_id": 37,
    "clients": [
        {
            "service_id": 430,
            "full_type": "uavcan.node.GetInfo_1_0",
            "server_nodes": [74, 99]
        }
    ]
}
```

Returns `404` if the node is not found, `503` if CAN is not connected.

**Get all registers for a node:**
```bash
curl http://localhost:8080/api/registers/37
```

Response:
```json
{
    "node_id": 37,
    "registers": [
        {
            "register_name": "uavcan.node.id",
            "value": "37",
            "type": "natural16",
            "access": "read-write"
        },
        {
            "register_name": "uavcan.node.description",
            "value": "My node",
            "type": "string",
            "access": "read-only"
        }
    ]
}
```

Returns `503` if CAN is not connected.

**Get node lifecycle history:**
```bash
curl "http://localhost:8080/api/nodes/37/history?range=1h&limit=500"
```

Query params:
- `range` — time window: `5m`, `15m`, `1h`, `6h`, `24h`, `7d` (default `1h`)
- `types` — comma-separated event types to filter, e.g. `service_call,health_change`
- `unique_id` — filter by hardware unique ID hex string (preferred over node_id for stable identity)
- `limit` — max entries, 1–2000 (default `500`)

Response:
```json
{
    "node_id": 37,
    "events": [
        {
            "id": 12,
            "node_id": 37,
            "timestamp_unix": 1741949445.123,
            "event_type": "health_change",
            "detail": {"old": "NOMINAL", "new": "CAUTION"}
        }
    ]
}
```

Event types: `first_seen`, `reappeared`, `disappeared`, `restart_suspected` (the uptime dropped, then counted on), `health_change`, `mode_change`, `port_change`, `service_call`, `got_node_id`, `lost_node_id`, `node_id_migration`, `node_id_conflict` (heartbeats on this node-ID alternate between two uptimes: two nodes share it; reported at most once a minute) and `type_conflict` (the node publishes a subject with another type than the one it is decoded as, which an earlier publisher advertised).

Returns `400` for invalid node_id or limit, `503` if the event logger is not available.

**Get node subject summary (aggregated telemetry stats per subject):**
```bash
curl http://localhost:8080/api/nodes/37/history/subjects
```

Query params:
- `unique_id` — filter by hardware unique ID hex string (preferred over node_id for stable identity)

Response:
```json
{
    "node_id": 37,
    "subjects": [
        {
            "subject_id": 1235,
            "message_type": "Temperature_1_0",
            "total_events": 4200,
            "avg_rate": 10.0,
            "first_seen_unix": 1741940000.0,
            "last_seen_unix": 1741949445.0
        }
    ]
}
```

Returns `400` for invalid node_id, `503` if the event logger is not available.

**Get service call history:**
```bash
curl "http://localhost:8080/api/services/430/history?range=7d&limit=50"
```

Query params:
- `range` — time window: `5m`, `15m`, `1h`, `6h`, `24h`, `7d` (default `7d`)
- `node_id` — filter history to a specific node
- `unique_id` — filter by hardware unique ID hex string (preferred over node_id for stable identity)
- `limit` — max entries, 1–200 (default `50`)

Response:
```json
{
    "service_id": 430,
    "history": [
        {
            "node_id": 37,
            "timestamp_unix": 1741949445.123,
            "service_id": 430,
            "service_type": "uavcan.node.GetInfo_1_0",
            "status": "ok",
            "latency_ms": 23,
            "response": "{\n  \"protocol_version\": {\n    \"major\": 1,\n    \"minor\": 0\n  },\n  ...\n}",
            "node_name": "org.example.my_node",
            "node_unique_id": [215, 79, 139, ...]
        }
    ]
}
```

`status` is one of `"ok"`, `"timeout"`, or `"error"`. The `response` field is present only for successful calls. `node_name` and `node_unique_id` are enriched from live telemetry when available.

Returns `400` for invalid service_id or limit, `503` if the event logger is not available.

**Set a register value on a node:**
```bash
curl -X POST http://localhost:8080/api/registers/37/set \
  -H 'Content-Type: application/json' \
  -d '{"name": "uavcan.node.id", "value": "42", "type": "natural16"}'
```

Response (success):
```json
{"status": "ok", "name": "uavcan.node.id", "value": "42"}
```

Returns `400` if required fields (`name`, `value`, `type`) are missing, the register is read-only, or the type is incompatible. Returns `503` if CAN is not connected.

### Recordings (`/api/recordings*`)

Named captures with optional length/event-count limits. Two storage modes:

- **`dedicated`** (default for new recordings) — matching subject events and service calls stream into a per-recording table (`recording_events`) from the moment the recording starts. Survives the global buffer's retention. Quick-save snapshots matching events from the global buffer into the same table at creation time.
- **`global`** — Phase 1 bookmarks. Metadata only; export reads from the shared `events` table within the recording's time-range × filter. Subject to global retention.

All recording endpoints return `503` if no `event_logger` is initialized (no CAN session yet), **except** `GET /api/recordings`, which returns `200 { recordings: [] }` so the frontend's startup poll doesn't error before CAN is connected.

#### List, create, inspect

```http
GET    /api/recordings                       → { recordings: [...] }
GET    /api/recordings/{rec_id}              → { recording: { ...stats } }
GET    /api/recordings/buffer                → { buffer: { ...global buffer stats } }
POST   /api/recordings                       body: { name, filter?, notes?, max_length_seconds?, max_events?, stop_on_limit? } → 201 { recording }
POST   /api/recordings/{rec_id}/stop         → { recording }
PATCH  /api/recordings/{rec_id}              body: { name?, notes? } → { recording }
DELETE /api/recordings/{rec_id}[?purge=true] → { deleted, purged }
```

A POST without `end_unix` starts a live recording; `end_unix` stays `null` until stopped (manually or by hitting a limit). Stats use `now()` for the upper bound.

A recording row contains: `id`, `name`, `start_unix`, `end_unix`, `filter`, `notes`, `created_at`, `max_length_seconds`, `max_events`, `stop_on_limit`, `auto_stopped`, `event_count`, `events_source`, plus computed `subjects`, `duration_seconds`.

#### Limits and auto-stop

`max_length_seconds` and `max_events` are optional caps. With `stop_on_limit: true` (default off — opt in per recording), the recording auto-stops on the first limit breach: `end_unix` is set, `auto_stopped` becomes `true`, and the recording disappears from the active-routing registry. With `stop_on_limit: false`, the limits are soft targets — the recording keeps capturing past 100%, useful for showing progress bars in the UI without enforcing a cap.

Auto-stop is checked both on each matching event (during ingest) and via a 5-second background sweep (catches time-based limits when the bus is silent).

#### Retroactive "Quick save"

```http
POST   /api/recordings/quick   body: { name, last_seconds, filter?, notes? }
```

Snapshots matching events from the global buffer in `[now - last_seconds, now]` into the recording's dedicated store at creation time. Useful workflow: "the bus just glitched, save the last 30 seconds." Returns `201` with the full recording row.

#### Filter shape (OR-across-dimensions)

```json
{
  "subject_ids":   [7509, 7510],
  "service_ids":   [384],
  "node_ids":      [42],
  "message_types": ["Heartbeat_1_0"]
}
```

An event matches if it satisfies **any** dimension (subject in `subject_ids` OR node in `node_ids` OR service in `service_ids` OR type in `message_types`). Empty/missing keys impose no restriction on that dimension. Empty filter overall = capture everything. Unknown keys are ignored. Invalid types are dropped silently (non-integer ids, non-string types). `service_ids` matches `service_call` rows; it has no effect against the global `events` table for legacy bookmarks.

#### Export

```http
GET    /api/recordings/{rec_id}/export?format=csv      → text/csv stream
GET    /api/recordings/{rec_id}/export?format=jsonl    → application/x-ndjson stream
```

CSV columns: `recording_id, timestamp_unix, timestamp, subject_id, publisher_node_id, unique_id, message_type, rate, attribute, value, unit`. One row per attribute (events with N attributes → N rows). Service-call rows currently appear with their `service_id` in the `subject_id` column and service metadata in `attributes_json` — a future CSV revision may add a dedicated `kind`/`service_id` column.

JSONL (JSON Lines) streams one JSON object per line. The first line is a header: `{ "recording": {...}, "exported_at_unix": float }`. Every subsequent line is a single event object with `kind`, `subject_id`/`service_id`, `timestamp_unix`, `attributes`, etc. Streamed with the same pagination as CSV — no hard event cap.

Both formats set `Content-Disposition: attachment; filename="<sanitized-name>.<ext>"`.

#### Global buffer (`GET /api/recordings/buffer`)

Stats about the shared `events` ring used by legacy bookmarks and quick-save:

```json
{
  "buffer": {
    "retention_seconds": 86400,
    "max_events": 5000000,
    "event_count": 142057,
    "oldest_event_unix": 1748742543.21,
    "newest_event_unix": 1748828943.18,
    "db_size_bytes": 38420480
  }
}
```

Use this to surface "what's available for quick-save" and to estimate observed message rate (count / (newest - oldest)).

### Replay (`/api/replay/*`)

A recording stored under `recording_events` can be streamed back through the same WebSocket the live telemetry flows on. Replay events carry the `replay: true` field but otherwise look identical to live events — frontends consume them through their normal cache path.

**Invariant:** replay is mutually exclusive with an active CAN session. Starting replay while CAN is connected returns `409`; calling `/api/can/connect` while a replay is running returns `409` with the same body shape. Only one replay session at a time; multiple browser tabs all watch the same one.

**Start playback:**
```bash
curl -X POST http://localhost:8080/api/replay/start \
  -H 'Content-Type: application/json' \
  -d '{"recording_id": 42, "speed": 2.0, "start_offset_s": 0}'
```
- `recording_id` (required, integer)
- `speed` (optional, 0.1–50.0, default `1.0`)
- `start_offset_s` (optional, seconds from the recording's first event)

Response on success — current replay status:
```json
{
  "active": true,
  "recording_id": 42,
  "position_s": 0.0,
  "duration_s": 32.4,
  "speed": 2.0,
  "paused": false,
  "events_emitted": 0,
  "total_events": 65,
  "finished": false
}
```

Returns `404` if the recording has no `kind='subject'` rows to replay, `409` if CAN is connected or another replay is in progress.

**Control playback (pause / resume / stop):**
```bash
curl -X POST http://localhost:8080/api/replay/control \
  -H 'Content-Type: application/json' \
  -d '{"action": "pause"}'
```
`action` is one of `"pause"`, `"resume"`, `"stop"`. Returns the latest status (or `{"active": false}` after stop). `404` if no replay is running and the action is not `stop`; `400` for an unknown action.

**Seek to a position:**
```bash
curl -X POST http://localhost:8080/api/replay/seek \
  -H 'Content-Type: application/json' \
  -d '{"position_s": 12.5}'
```
`position_s` is clamped to `[0, duration_s]`. Returns 404 if no replay is running.

**Change speed without re-seeking:**
```bash
curl -X POST http://localhost:8080/api/replay/speed \
  -H 'Content-Type: application/json' \
  -d '{"speed": 5.0}'
```
`speed` is clamped to `[0.1, 50.0]`. Returns 404 if no replay is running.

**Query current status (poll-friendly):**
```bash
curl http://localhost:8080/api/replay/status
```
Returns the same shape as the start response; `{"active": false}` when no replay is running.

**Replay-ended WebSocket sentinel:**

When replay terminates — naturally at the end of the recording, or because a client called `stop` — the backend pushes one frame to every subscribed client before tearing the WS down:
```json
{
  "type": "replay_ended",
  "recording_id": 42,
  "finished": true
}
```
`finished` distinguishes the two paths: `true` means playback reached the end, `false` means a client stopped it. Frontends can use this to switch back to an idle state without polling.

**MVP scope notes:**
- Only `kind='subject'` rows are replayed. `service_call` rows stay in storage for export but aren't played.
- `/api/nodes` during replay is synthesised from the recording's publisher list — no GetInfo / health / mode / uptime / client port lists. The placeholder payload carries `_replay: true` on each node so consumers can flag the view as approximate.

### Event Logger (SQLite)

Events are automatically logged to `telemetry_events.db`. The `events` table has fields:
- `id` (auto-increment)
- `subject_id`
- `timestamp`, `timestamp_unix`
- `rate`, `message_type`
- `publisher_node_id`, `unique_id`
- `attributes` (JSON)
- `created_at` (database timestamp)

**Retention.** The global `events` table is pruned by **time-based retention** (default 24 hours). A hard event-count cap (`max_events`, default 5,000,000) acts as a safety net only — it bounds disk if rate × retention would otherwise blow past it. Events are written in transactions of up to 500 (about 20,000 events/s on an SSD). Pruning runs every 1000 writes; configure both via `EventLogger(retention_seconds=..., max_events=...)`.

Per-recording event stores (`recording_events`) are **not** subject to retention — they only grow until the recording is deleted (with `?purge=true`) or stopped. Recording rows survive global retention by definition.

**Query logged events programmatically:**
```python
from event_logger import EventLogger

logger = EventLogger("telemetry_events.db")

# Get last 100 Heartbeat events from node 42
events = logger.get_events(
    node_id=42,
    message_type="Heartbeat_1_0",
    limit=100
)

# Clear old data
logger.clear_events()

# Check event count
count = logger.get_event_count()
```

## Configuration

### Port and Host

The host is controlled by the `--bind` CLI flag (default `127.0.0.1`):

```bash
python3 main.py --can vcan0 --bind 0.0.0.0
```

For non-default ports, edit `main.py` directly:
```python
ws_server = WebSocketServer(
    session=session,
    host=bind,
    port=9000,          # Change port
    log_store=_log_store,
)
```

### Database Path

Edit `main.py`:
```python
event_logger = EventLogger(
    db_path="./logs/telemetry.db",  # Custom location
    max_events=500000                # Retention limit
)
```

### Event Queue Size

Edit `telemetry_manager.py`:
```python
queue = telemetry.subscribe(max_queue=200)  # Increase from 100
```

## Architecture Components

### 1. ScannerNode (`scanner_node.py`)
- Subscribes to standard UAVCAN messages: Heartbeat (7509), port.List (7510), PnP NodeIDAllocation v1/v2 (8166/8165)
- Discovers nodes and their publishers/services
- Decodes and extracts message attributes
- Forwards all standard messages as telemetry events to the queue
- Emits standardized JSON events to queue

### 2. TelemetryManager (`telemetry_manager.py`)
- Consumption loop for message_queue
- Maintains latest-state cache (by subject and node)
- Normalizes events into JSON-safe payloads before caching/broadcasting
- Broadcasts events to all subscribers
- Supports multiple concurrent consumers

### 3. WebSocketServer (`websocket_server.py`)
- Async HTTP/WebSocket server (aiohttp)
- Client connection management
- Event filtering (subject_id, node_id, message_type)
- REST API for queries

### 4. EventLogger (`event_logger.py`)
- SQLite persistence layer
- Async batch writing for efficiency
- Flushes queued events during shutdown
- Query API for historical analysis
- Node lifecycle history (health/mode changes, service calls) with 30-day retention
- Service call history with response body storage
- Configurable retention limits

## Performance Considerations

| Component | Capacity | Bottleneck |
|---|---|---|
| ScannerNode | 128 nodes | UAVCAN bus saturation |
| TelemetryManager | 100K events/s | RAM (cache) |
| WebSocketServer | 1000 clients | Network bandwidth |
| EventLogger | 100K events/s | Disk I/O |

Use `max_events` in EventLogger to prevent database bloat.

## Troubleshooting

**WebSocket connection refused:**
- Ensure `main.py` is running
- Check firewall (port 8080)
- The server binds to `127.0.0.1` by default; if you need to reach it from another host, restart with `--bind 0.0.0.0` (no auth is enforced — only do this on a trusted network)

**High memory usage:**
- Reduce TelemetryManager queue size
- Reduce EventLogger max_events
- Check number of WebSocket clients

**No events appearing:**
- Verify you started with `python3 main.py --can <iface>` or completed startup selection API
- Check nodes are broadcasting (start other nodes)
- Remember `/api/latest/node/{node_id}` only returns cached publisher traffic; a discovered node with no cached telemetry returns `404` with `known_node: true`
- Look for errors in logs: `ERROR` or `WARNING` messages

**Database locked errors:**
- Reduce batch write size
- Check for other processes accessing the DB
- Use WAL mode: `sqlite3 telemetry_events.db "PRAGMA journal_mode=WAL;"`

## Development

Run with debug logging:
```bash
PYTHONPATH=/path/to/workspace python3 -c "
import logging
logging.basicConfig(level=logging.DEBUG)
import asyncio
from main import main
asyncio.run(main())
"
```

## Files

| File | Purpose |
|---|---|
| `scanner_node.py` | UAVCAN network scanner |
| `telemetry_manager.py` | Event pub-sub router |
| `node_info.py` | Per-node state (lifecycle, ports, getInfo) |
| `node_identity_map.py` | Bidirectional unique_id ↔ node_id mapping with migration detection |
| `websocket_server.py` | WebSocket/REST server |
| `log_store.py` | In-memory API log buffer + log handler |
| `event_logger.py` | SQLite persistence |
| `allocator.py` | Allocator detection + fallback allocator manager |
| `startup_setup.py` | DSDL compilation via nnvg, env setup |
| `main.py` | System orchestration |

## License

PyCyphal (UAVCAN) - MIT License
