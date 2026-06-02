# Website Frontend (PyCyphal Network Dashboard)

This folder contains the browser dashboard for your telemetry backend.

The current UI is network-centric: it emphasizes topology, live traffic, and selected-node inspection instead of a generic table-first layout.

## Files

- `index.html` – main UI layout
- `styles.css` – theme and component styling
- `state.js` – global state, settings persistence, API helpers
- `cache.js` – telemetry cache and per-node accessors
- `plot.js` – multi-panel D3 time-series plot with crosshair, interactive legend, zoom/pan, markers, freehand drawing
- `compare-view.js` – independent multi-graph compare view with derived series, presets, export/import
- `detail-panel.js` – per-node detail panel with subject cards and tab rendering
- `nodes-table.js` – Tabulator-based nodes table with ghost row support
- `services-panel.js` – service interaction UI, schema fetch, persistent history
- `registers-panel.js` – register read/write UI with type validation
- `history-panel.js` – node lifecycle history timeline with time-range filtering
- `subjects-panel.js` – subject browser with inline service expansion
- `graph-view.js` – D3 force-directed network topology with drag-to-pin
- `dsdl-view.js` – DSDL Inspector: namespace tree, field search, dependency navigation, custom-type editor
- `record-view.js` – Record tab: subject/service/node pickers, per-recording cards with progress bars, limits, edit-limits modal, duplicate, CSV/JSON export
- `log-panel.js` – Right log panel: Cyphal + Server feeds, subject picker, severity floor, per-source toggle pills with count badges
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
  - **Compare** — independent graphs for side-by-side multi-series comparison with derived series, thresholds, markers, and freehand drawing
  - **DSDL** — searchable tree of loaded DSDL types with bus-activity badges, field-level search, dependency navigation, and a custom-type editor under `dsdl_messages/custom/`
  - **Record** — capture filtered events into per-recording SQLite stores with `max_length` / `max_events` caps, quick-save the last N seconds, edit limits on live recordings, duplicate, and CSV/JSON export
- Detail panel (below nodes table, nodes view):
  - Tabbed view: Publishers, Subscribers, Servers, Clients, Registers, History (with count badges)
  - Subject cards: each subject displayed as an individual card with subject ID, message type, rate, live dot indicator, and key-value metrics
  - Servers tab: expandable service cards with request forms, response display, and persistent call history fetched from backend
  - History tab: node lifecycle events (health/mode changes, service calls) with time-range filtering
  - Real-time D3 line plot: click a subject card to plot its numeric attributes over time (60-second scrolling window)
  - Plot legend: three-zone pills with color picker, line style cycling, visibility toggle, and remove
  - Resizable split between card list and plot area (drag handle)
  - Vertical resize handle between nodes table and detail panel
- Right log panel (hidden by default, toggled from the right edge):
  - Unified timeline of Cyphal diagnostic messages, user-picked text subjects, and the backend's `/api/logs` stream
  - Per-source toggle pills (`CYPHAL`, `SERVER`) with live count badges
  - `Min: TRACE…ALERT` severity floor applies across all sources via mapped levels; user-added subjects always show
  - `+` button opens a subject picker listing every subject whose payload carries a string field
  - Cyphal row layout: `time · n<id> · s<id> · MessageType · text`; Server: `time · LEVEL · logger · message`
  - SERVER pill turns amber with a corner dot when polling without backend connection
  - Buffer cap: 2000 entries in memory, never persisted
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
  - `GET /api/dsdl/status` / `GET /api/dsdl/namespaces` / `GET /api/dsdl/type/{full_name}` — DSDL Inspector data
  - `POST /api/dsdl/custom/namespace` / `GET /api/dsdl/custom/namespaces` / `POST /api/dsdl/custom/type` / `DELETE /api/dsdl/custom/type/{full_name}` — custom DSDL CRUD (POST handles both create and save)
  - `POST /api/dsdl/compile` — force regenerate Python from DSDL
  - `GET /api/recordings` / `POST /api/recordings` / `PATCH /api/recordings/{id}` / `DELETE /api/recordings/{id}` — recording CRUD
  - `POST /api/recordings/{id}/stop` — stop a live recording
  - `POST /api/recordings/quick` — snapshot the last N seconds from the global buffer
  - `GET /api/recordings/{id}/export?format={csv,json}` — export a recording
  - `GET /api/recordings/buffer` — global buffer stats (size, byte estimate, oldest-event timestamp)
- CAN error handling:
  - Auto-disconnect on bus faults (BUS-OFF, ERROR-PASSIVE, interface disappearance)
  - Alert shown to user with error details

## Configuration

- API base URL is editable in the UI (default: `http://localhost:8080`).
- WebSocket URL is derived automatically from API base (`ws://.../ws`).
- All settings persisted in localStorage: API URL, CAN interface, filters, sort state, column widths, theme, sidebar state, refresh interval, selected node, detail panel split ratio, detail panel height, active view, favourite/hidden subjects, compare graphs with markers and drawings.
- Node list refresh interval is configurable via sidebar slider (1–60 seconds, default: 3s).
- Selected node, active view, and detail panel layout are restored on page reload.

## Compare View

The compare view provides independent graphs for multi-series comparison.

### Features

- **Multi-series overlay** — add any subject + attribute from the network to a graph
- **Derived series** — computed from raw series: delta, ratio, moving average, min/max envelope, rate of change
- **Thresholds** — horizontal reference lines with labels
- **Interactive legend** — three-zone pills: click swatch to change color, click style indicator to cycle line style (solid/dashed/dotted/dashdot/longdash + circle/square/triangle/diamond markers), click label to toggle visibility, click × to remove
- **Timeline markers** — Shift+click to place a named marker with optional note, color, and line style. Click on an existing marker to edit. Markers persist and export with the graph config.
- **Freehand drawing** — Alt+drag to draw annotations directly on the plot. Controls for color, line style, and size are in the toolbar. Alt+double-click to clear all drawings.
- **Zoom and pan** — mouse wheel to zoom, drag to pan the X axis. Click to pause/resume, double-click to reset zoom.
- **Crosshair sync** — hovering over one graph shows synchronized crosshairs with live value readouts on all graphs
- **Presets** — save/load named graph configurations
- **Export/Import** — full workspace export/import as JSON files
- **Per-graph controls** — time window, fill rate interpolation, stroke size, disconnected points, grid

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
