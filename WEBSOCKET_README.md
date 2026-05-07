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

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

Key dependencies:
- `pycyphal` - UAVCAN protocol library
- `aiohttp` - Async HTTP/WebSocket server
- `aiohttp-cors` - CORS support

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
SYSTEM RUNNING
============================================================
WebSocket:   ws://localhost:8080/ws
REST API:    http://localhost:8080/api/
Health:      http://localhost:8080/api/health
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
    "attributes": [
        {"attribute": "uptime", "value": 12345, "unit": "s"},
        {"attribute": "health", "value": "NOMINAL"},
        {"attribute": "mode", "value": "OPERATIONAL"},
        {"attribute": "vssc", "value": 42}
    ]
}
```

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
        }
    }
}
```

**Health check:**
```bash
curl http://localhost:8080/api/health
```

Response:
```json
{
    "status": "healthy",
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

### Event Logger (SQLite)

Events are automatically logged to `telemetry_events.db` with fields:
- `id` (auto-increment)
- `subject_id`
- `timestamp`, `timestamp_unix`
- `rate`, `message_type`
- `publisher_node_id`
- `attributes` (JSON)
- `created_at` (database timestamp)

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
| ScannerNode | 1000 nodes | UAVCAN bus saturation |
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
| `websocket_server.py` | WebSocket/REST server |
| `log_store.py` | In-memory API log buffer + log handler |
| `event_logger.py` | SQLite persistence |
| `allocator.py` | Allocator detection + fallback allocator manager |
| `main.py` | System orchestration |

## License

PyCyphal (UAVCAN) - MIT License
