# Website Frontend (PyCyphal Network Dashboard)

This folder contains the browser dashboard for your telemetry backend.

The current UI is network-centric: it emphasizes topology, live traffic, and selected-node inspection instead of a generic table-first layout.

## Files

- `index.html` – main UI layout
- `styles.css` – theme and component styling
- `state.js` – global state, settings persistence, API helpers
- `cache.js` – telemetry cache and per-node accessors
- `plot.js` – multi-panel D3 time-series plot with crosshair and legend
- `detail-panel.js` – per-node detail panel with subject cards and tab rendering
- `nodes-table.js` – Tabulator-based nodes table with ghost row support
- `services-panel.js` – service interaction UI, schema fetch, persistent history
- `registers-panel.js` – register read/write UI with type validation
- `history-panel.js` – node lifecycle history timeline with time-range filtering
- `subjects-panel.js` – subject browser with inline service expansion
- `graph-view.js` – D3 force-directed network topology with drag-to-pin
- `connection.js` – WebSocket lifecycle, REST polling, reconnect
- `app.js` – boot, DOM bindings, view switching

## Prerequisites

1. Backend running from `server/`:

```bash
cd server && python3 main.py --can vcan0
```

Or (selection mode — pick CAN interface from the UI):

```bash
cd server && python3 main.py
```

2. Python available to serve static files.

## Start the Website

From project root:

```bash
cd website
python3 -m http.server 5500
```

Open in browser:

- `http://localhost:5500`

## Dashboard Layout

- Sidebar:
  - Server connection (API base URL + Connect button)
  - CAN interface dropdown + Connect button
  - Refresh interval slider
  - Node/event counters
  - Theme toggle (dark/light)
  - Collapsible (half-hidden toggle button)
- Main area (nodes table):
  - Sortable columns (ID, Name, State, Rate, Uptime)
  - Inline filter fields per column
  - Resizable columns (drag to resize)
  - Port ID lists (Publishers, Subscribers, Servers, Clients)
  - Node state: active, idle, offline (with "last seen" for offline nodes)
  - Health indicators: NOMINAL, ADVISORY, CAUTION, WARNING
  - Responsive — hides less-important columns on narrow screens
- View tabs (bottom of sidebar):
  - **Nodes** — node-centric table with per-node detail panel; ghost rows for displaced identities pinned to bottom
  - **Subjects** — subject-centric table listing all subjects and services across the network, with inline service expansion
  - **Graph** — D3 force-directed bipartite topology showing device and subject nodes with directional pub/sub links
- Detail panel (below nodes table, nodes view):
  - Tabbed view: Publishers, Subscribers, Servers, Clients, Registers, History (with count badges)
  - Subject cards: each subject displayed as an individual card with subject ID, message type, rate, live dot indicator, and key-value metrics
  - Servers tab: expandable service cards with request forms, response display, and persistent call history fetched from backend
  - History tab: node lifecycle events (health/mode changes, service calls) with time-range filtering
  - Real-time D3 line plot: click a subject card to plot its numeric attributes over time (60-second scrolling window)
  - Plot legend with per-attribute checkboxes to show/hide individual series
  - Resizable split between card list and plot area (drag handle)
  - Vertical resize handle between nodes table and detail panel
- Server down overlay:
  - Detects when the frontend web server (port 5500) becomes unreachable
  - Shows full-screen overlay with reconnection status and startup instructions
  - Automatically recovers when the server comes back online

## What the Website Supports

- CAN lifecycle via REST:
  - `GET /api/status` (connection state, available interfaces, bus utilization)
  - `POST /api/can/connect` / `POST /api/can/disconnect`
- Live telemetry over WebSocket:
  - `ws://localhost:8080/ws`
  - Optional filters: `subject_ids`, `node_ids`, `message_types`
  - Real-time metrics: bus utilization streamed every 1s (`{"type": "metrics"}`)
  - Automatic reconnect with backoff after unexpected disconnects
- REST queries:
  - `GET /api/health`
  - `GET /api/nodes`
  - `GET /api/latest/subject/{subject_id}`
  - `GET /api/latest/node/{node_id}`
  - `GET /api/logs?limit=100&level=ERROR&min_level=WARNING`
  - `GET /api/services/{node_id}` — service schema with request fields
  - `POST /api/services/{node_id}/{service_id}/call` — invoke a service
  - `GET /api/clients/{node_id}` — client ports with server cross-references
  - `GET /api/registers/{node_id}` — read all registers
  - `POST /api/registers/{node_id}/set` — write a register
  - `GET /api/nodes/{node_id}/history` — node lifecycle events
  - `GET /api/nodes/{node_id}/history/subjects` — per-subject telemetry summary
  - `GET /api/services/{service_id}/history` — service call history
  - `GET /api/identity-map` — unique_id to node_id mappings
  - `DELETE /api/identity/{unique_id}` — remove a ghost identity
- CAN error handling:
  - Auto-disconnect on bus faults (BUS-OFF, ERROR-PASSIVE, interface disappearance)
  - Alert shown to user with error details

## Configuration

- API base URL is editable in the UI (default: `http://localhost:8080`).
- WebSocket URL is derived automatically from API base (`ws://.../ws`).
- All settings persisted in localStorage: API URL, CAN interface, filters, sort state, column widths, theme, sidebar state, refresh interval, selected node, detail panel split ratio, detail panel height, active view, favourite/hidden subjects.
- Node list refresh interval is configurable via sidebar slider (1–60 seconds, default: 3s).
- Selected node, active view, and detail panel layout are restored on page reload.

## Troubleshooting

### Page loads but no data

- Ensure backend is running on port `8080`.
- Click **Check Health** in the dashboard.
- If backend started without `--can`, connect to a CAN interface via the sidebar dropdown.

### WebSocket stays disconnected

- Verify `ws://localhost:8080/ws` is reachable.
- Check backend logs via `GET /api/logs`.

### Graph shows isolated nodes

- The topology graph is logical, not physical CAN wiring.
- Links are inferred from publisher/subscriber subject overlap.
- If nodes do not advertise complementary ports yet, they may appear without edges.

### CORS/browser issues

- Backend already enables CORS; verify you are calling the correct backend URL.

### Port conflict on 5500

Run with another port:

```bash
python3 -m http.server 8088
```

Then open `http://localhost:8088`.
