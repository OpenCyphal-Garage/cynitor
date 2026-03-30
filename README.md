# Cynitor - Cyphal Network Monitor

Standalone GUI for monitoring and interacting with Cyphal (UAVCAN) nodes over CAN bus.

## Quick Start

```bash
# Backend (direct mode - CAN interface known)
cd server && python3 main.py --can vcan0

# Backend (selection mode - pick CAN interface from the UI)
cd server && python3 main.py

# Force DSDL recompilation
cd server && python3 main.py --can vcan0 --recompile

# Frontend
cd website && python3 -m http.server 5500
```

Open `http://localhost:5500`, click **Connect** to reach the backend at `http://localhost:8080`.

---

## Architecture Overview

```
 CAN Bus (vcan0, slcan0, ...)
       |
       v
 +------------------+     asyncio.Queue     +--------------------+
 |   ScannerNode    | -------------------->  | TelemetryManager   |
 |                  |     (JSON events)      |                    |
 | - heartbeat sub  |                        | - caches latest    |
 | - port list sub  |                        |   by subject_id    |
 | - per-publisher  |                        |   and node_id      |
 |   subscribers    |                        | - broadcasts to    |
 | - service clients|                        |   all subscribers  |
 +------------------+                        +--------------------+
                                                |       |       |
                                    +-----------+   +---+   +---+-----------+
                                    v               v       v               v
                             WebSocketServer   EventLogger  telemetry_printer
                             (aiohttp :8080)   (SQLite)     (debug logging)
                              |          |
                     REST API |          | WebSocket /ws
                              v          v
                         +----------------------+
                         |  Frontend (browser)  |
                         |  website/ on :5500   |
                         +----------------------+
```

---

## Backend Components

All backend code lives in `server/`.

### main.py - Entry Point

Orchestrates the full lifecycle. Two startup modes:

**Direct mode** (`python3 main.py --can vcan0`): Immediately initializes the CAN transport.

**Selection mode** (`python3 main.py`): Starts the HTTP server on port 8080 immediately without CAN. Use the REST API to connect:
- `GET /api/status` - lists available CAN interfaces and connection state
- `POST /api/can/connect` - connect to a CAN interface (`{"interface":"vcan0"}`)
- `POST /api/can/disconnect` - disconnect from CAN

Once connected, the full CAN pipeline starts:

```
prepare_runtime()          Set env vars, compile DSDL
        |
AllocatorManager.start()   Detect or start node-ID allocator
        |
ScannerNode()              Begin CAN network discovery
        |
TelemetryManager.start()   Consume scanner events, cache + broadcast
        |
WebSocketServer.start()    REST + WebSocket API on :8080
        |
EventLogger.start()        SQLite persistence
        |
Background loops:          register_nodes, event_logger, telemetry_printer
```

### scanner_node.py - Network Discovery

The core CAN interface. Uses pycyphal to:

1. **Discover nodes** - Subscribes to heartbeat (subject 7509) and port list (subject 7510). When a node appears, reads its registers to learn what it publishes and serves.

2. **Subscribe to publishers** - For each discovered publisher subject, dynamically imports the DSDL message class and creates a subscriber. Incoming messages are converted to JSON events and pushed to `message_queue`.

3. **Create service clients** - For each discovered service, creates a pycyphal client. Service calls are made on demand via `make_service_call()`.

Data structures:
- `all_nodes: dict[int, NodeInfo]` - state for all 128 possible node IDs
- `publishers_subscribers: dict[int, Subscriber]` - active subscriptions by subject_id
- `service_clients: dict[(node_id, service_id), Client]` - cached service clients
- `message_queue: asyncio.Queue` - output to TelemetryManager

### node_info.py - Per-Node State

Tracks each node's lifecycle: appearance, heartbeat timestamps, disappearance (offline > 1.1s), port lists (publisher/subscriber/client/server subject and service IDs), and GetInfo response (name, software version, unique ID).

### telemetry_manager.py - Event Router

Consumes `message_queue` from ScannerNode in a loop. For each event:
1. Updates `latest_by_subject[subject_id]` and `latest_by_node[node_id][subject_id]` caches
2. Broadcasts to all subscriber queues (WebSocket server, EventLogger, debug printer)

Any component can call `subscribe()` to get a queue that receives all events.

### websocket_server.py - HTTP + WebSocket API

Single aiohttp server on port 8080 serving both REST and WebSocket.

**REST endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api` | API documentation and available endpoints |
| GET | `/api/status` | Connection state, available interfaces, bus utilization, last error |
| GET | `/api/health` | Server health check and connected client count |
| GET | `/api/nodes` | All discovered nodes with full state |
| GET | `/api/latest/subject/{id}` | Latest telemetry event for a subject |
| GET | `/api/latest/node/{id}` | Latest events from a specific node |
| GET | `/api/logs` | Recent application logs (query: `limit`, `level`, `min_level`) |
| POST | `/api/can/connect` | Connect to a CAN interface (`{"interface":"vcan0"}`) |
| POST | `/api/can/disconnect` | Disconnect from CAN interface |

**WebSocket (`/ws`):**

Each connected client gets a dedicated telemetry queue. The server filters events per-client before sending.

Client sends:
```json
{"type": "filter", "subject_ids": [7509], "node_ids": [1], "message_types": ["Heartbeat_1_0"]}
{"type": "ping"}
```

Server sends:
```json
{"type": "filter_updated", "filter": {...}}
{"type": "pong"}
{"type": "metrics", "bus_utilization": 3.0}
```
Plus a stream of telemetry events matching the client's filter. Metrics messages are sent every 1 second to all connected clients.

### event_logger.py - SQLite Persistence

Subscribes to TelemetryManager and batch-writes events to `telemetry_events.db`. Batches up to 10 events per write, uses `asyncio.to_thread()` for non-blocking I/O. Auto-prunes when `max_events` (default 100,000) is exceeded.

### allocator.py - Node-ID Allocator

On startup, checks if an external allocator exists on node-ID 1 (listens for heartbeat for 3 seconds). If not found, starts a local CentralizedAllocator. Monitors periodically (every 10s) in case the external allocator disappears.

### startup_setup.py - DSDL Compilation & Runtime

`prepare_runtime()` handles:
1. Compiles DSDL definitions from `dsdl_messages/` to Python classes in `python_compiled_messages/` using `nnvg`
2. Sets pycyphal environment variables (`UAVCAN__CAN__IFACE`, `UAVCAN__CAN__MTU`, `UAVCAN__NODE__ID`)
3. Auto-assigns a node ID via `yakut accommodate` if none is set

### log_store.py - In-Memory Log Buffer

Thread-safe deque (max 5000 entries) fed by a `logging.Handler`. Exposed via `/api/logs` for the frontend to display backend logs.

---

## Frontend

All frontend code lives in `website/`. Plain HTML/CSS/JS with D3 for graph visualization and real-time plots. No build step.

### Connection Flow

```
Page load
    |
    v
[Restore settings from localStorage]
    |
    v  (if previously connected)
connectDashboard()
    |-- GET /api/health
    |   |-- fail: stay disconnected
    |   |-- ok: dashboardConnected = true
    |         |
    |         v
    |   pollStatus()
    |   |-- GET /api/status (available interfaces, CAN state, bus load)
    |   |-- populate dropdown
    |
    v  (user clicks CAN Connect)
connectCan()
    |-- POST /api/can/connect {interface: "vcan0"}
    |-- canConnected = true
    |-- startNodesPolling() (every N seconds)
    |-- connectWs()
    |       |
    |       v
    | WebSocket /ws
    |   |-- onmessage: cache events + metrics, update table
    |   |-- onclose: auto-reconnect with backoff
    |   |-- receives {"type": "metrics"} every 1s
```

### State Persistence

Settings saved to `localStorage` under `pycyphal.dashboard.settings.v2`:
- API base URL, selected CAN interface
- Dashboard and CAN connection state (restored with health check on reload)
- Column filter values, sort state, refresh interval, selected tab
- Theme (dark/light), sidebar collapsed state
- Custom column widths (user-resizable by dragging)
- Selected node ID and detail panel split ratio

### Key Files

- `index.html` - Layout: sidebar (connection controls, refresh slider, theme toggle) + main area (nodes table + detail panel with subject cards and D3 plots)
- `app.js` - All application logic: connection management, data caching, rendering, D3 real-time plots, column resize, server heartbeat
- `styles.css` - Dark/light theme with CSS variables, responsive column layout, card and plot styling

---

## Telemetry Event Format

Every telemetry event flowing through the system uses this JSON structure:

```json
{
  "subject_id": 7509,
  "timestamp": "2026-03-13T10:30:45",
  "timestamp_unix": 1741949445.123,
  "rate": 10,
  "message_type": "Heartbeat_1_0",
  "publisher_node_id": 42,
  "attributes": [
    {"attribute": "uptime", "value": 12345, "unit": "microsecond"},
    {"attribute": "health", "value": "NOMINAL"}
  ]
}
```

---

## DSDL Messages

DSDL definitions live in `dsdl_messages/`:

```
dsdl_messages/
  public_regulated_data_types/
    uavcan/       # Standard: Heartbeat, PortList, GetInfo, registers
    reg/          # UDRAL: physics, electricity, kinematics
  dontpanic/      # Custom application messages
```

Compiled at startup to `python_compiled_messages/` via `nnvg`. Use `--recompile` to force recompilation.

---

## Project Structure

```
cynitor/
  server/
    main.py                 Entry point, lifecycle orchestration
    scanner_node.py         CAN network discovery and subscriptions
    node_info.py            Per-node state tracking
    telemetry_manager.py    Event routing and caching
    websocket_server.py     REST API + WebSocket server
    event_logger.py         SQLite event persistence
    allocator.py            Node-ID allocation management
    startup_setup.py        DSDL compilation, env setup
    log_store.py            In-memory log buffer for API
  website/
    index.html              Dashboard layout
    app.js                  Frontend application logic
    styles.css              Dark theme styling
  dsdl_messages/            DSDL type definitions
  WEBSOCKET_README.md       Detailed API contract reference
```
