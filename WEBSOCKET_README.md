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
✅ **CORS Support** - Ready for web dashboard integration  
✅ **Clean Shutdown** - Graceful WebSocket disconnect and logger queue flush  
✅ **Allocator Guard** - Reuses external allocator if present, otherwise starts local allocator and re-checks every 10s
✅ **CAN Health Monitoring** - Detects CAN bus faults (BUS-OFF, ERROR-PASSIVE, interface disappearance) and auto-disconnects
✅ **Bus Load Monitoring** - Real-time CAN bus utilization via `canbusload` subprocess, streamed to clients via WebSocket  
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
- CORS is handled via built-in middleware (no extra dependency)

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

### 3. Runtime Environment

At startup, Python setup runs automatically and configures:
- `UAVCAN__CAN__IFACE=socketcan:<selected_iface>`
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
REST API:    http://localhost:8080/api/
Health:      http://localhost:8080/api/health
Status:      http://localhost:8080/api/status
============================================================
```

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
```

**Server Messages:**

Telemetry events (filtered per client):
```json
{
    "subject_id": 7509,
    "timestamp": "2026-03-13T10:30:45",
    "timestamp_unix": 1741949445.123,
    "rate": 10,
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

`payload_bytes` is the size, in bytes, of the received transfer's serialized payload (sum of `transfer.fragmented_payload` fragment lengths). It is `null` if the transport did not expose the fragmented payload (best-effort field).

Metrics (sent to all clients every 1 second):
```json
{
    "type": "metrics",
    "bus_utilization": 3.0
}
```

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
    "available_interfaces": ["vcan0", "can0"],
    "bus_utilization": 3.0,
    "last_error": null
}
```

`status` is `"running"` when connected to CAN, `"idle"` otherwise. `last_error` contains the error message if CAN was auto-disconnected due to a bus fault.

**Connect to a CAN interface:**
```bash
curl -X POST http://localhost:8080/api/can/connect \
  -H 'Content-Type: application/json' \
  -d '{"interface":"vcan0"}'
```

Response (success):
```json
{"status": "running", "can_interface": "vcan0"}
```

Returns `409` if already connected, `400` if interface is unknown.

**Disconnect from CAN:**
```bash
curl -X POST http://localhost:8080/api/can/disconnect
```

Response:
```json
{"status": "idle", "available_interfaces": ["vcan0", "can0"]}
```

Returns `409` if not connected.

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

Pass `"overwrite": true` to replace an existing custom type's source. Only allowed while the type is **not compiled** — the server returns `409` if a compiled `.py` already exists in `python_compiled_messages/` for this type.

**Delete custom DSDL type:**
```bash
curl -X DELETE http://localhost:8080/api/dsdl/custom/type/myapp.sensors.Temperature.1.0
```
Returns `200` with `{"full_name": "myapp.sensors.Temperature.1.0", "deleted": true}`.
Returns `404` if the source file is missing, `409` if the type is already compiled (delete the corresponding entry in `python_compiled_messages/` first if you really need to remove it), or `400` on a malformed name.

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
        "host": "0.0.0.0",
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
    "response": "GetInfo_1_0.Response(...)"
}
```

Response (timeout):
```json
{
    "status": "timeout",
    "latency_ms": 5000,
    "error": "Service 430 on node 37 timed out"
}
```

Returns `400` for invalid request body or attribute validation errors, `404` if the service is not found on the node, `503` if CAN is not connected.

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
            "detail": {"old_health": 0, "new_health": 2}
        }
    ]
}
```

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
            "response": "GetInfo_1_0.Response(...)",
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

All recording endpoints return `503` if no `event_logger` is initialized (no CAN session yet).

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
GET    /api/recordings/{rec_id}/export?format=csv     → text/csv stream
GET    /api/recordings/{rec_id}/export?format=json    → application/json
```

CSV columns: `recording_id, timestamp_unix, timestamp, subject_id, publisher_node_id, unique_id, message_type, rate, attribute, value, unit`. One row per attribute (events with N attributes → N rows). Service-call rows currently appear with their `service_id` in the `subject_id` column and service metadata in `attributes_json` — a future CSV revision may add a dedicated `kind`/`service_id` column.

JSON returns `{ recording, events: [...], truncated, exported_at_unix }`. Each event carries a `kind` field (`'subject'` or `'service_call'`) and either `subject_id` or `service_id`. Buffered with a hard cap of 200,000 events; set `truncated=true` if reached. Use CSV for larger windows.

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

### Event Logger (SQLite)

Events are automatically logged to `telemetry_events.db`. The `events` table has fields:
- `id` (auto-increment)
- `subject_id`
- `timestamp`, `timestamp_unix`
- `rate`, `message_type`
- `publisher_node_id`, `unique_id`
- `attributes` (JSON)
- `created_at` (database timestamp)

**Retention.** The global `events` table is pruned by **time-based retention** (default 24 hours). A hard event-count cap (`max_events`, default 5,000,000) acts as a safety net only — it bounds disk if rate × retention would otherwise blow past it. Pruning runs every 1000 writes; configure both via `EventLogger(retention_seconds=..., max_events=...)`.

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

Edit `main.py`:
```python
ws_server = WebSocketServer(
    session=session,
    host="127.0.0.1",  # Change host
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
- Verify `0.0.0.0` binding or change to `127.0.0.1` for local-only

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
