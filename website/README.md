# Website Frontend (PyCyphal Network Dashboard)

This folder contains the browser dashboard for your telemetry backend.

The current UI is network-centric: it emphasizes topology, live traffic, and selected-node inspection instead of a generic table-first layout.

## Files

- `index.html` – main UI layout
- `styles.css` – theme and component styling
- `state.js` – global state, settings persistence, API helpers
- `cache.js` – telemetry cache and per-node accessors
- `plot.js` – multi-panel D3 time-series plot with crosshair, interactive legend, zoom/pan, markers, freehand drawing
- `compare-view.js` – independent multi-graph compare view: graph cards, series picker, derived series, each graph's drawing, presets, export/import
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
  - Sortable columns (ID, Name, State, Rate, Uptime); Health and State sort by severity, and offline nodes that lost their node-ID stay at the bottom in either direction. When a sorted value changes, the table sorts again (every 3 s at most), but not while the pointer is over it
  - Rate: messages per second the node sends now. A subject counts while its messages flow, by the Graph's rule (three message periods, two seconds at least); one that stopped counts for nothing
  - Inline filter fields per column; ID and port filters take whole IDs, comma-separated (`10, 21`)
  - Resizable columns (drag to resize)
  - Port ID lists (Publishers, Subscribers, Servers, Clients): the node's own ports first, the standard ones muted after; a long list shows its first IDs and how many more (`1004, 1005 +4`), and its tooltip has it whole. The Subjects table's node lists read the same way
  - Node state: active, idle, offline (with "last seen" for offline nodes)
  - Health indicators: NOMINAL, ADVISORY, CAUTION, WARNING
  - VSSC: the heartbeat's vendor-specific status code (0, the usual, muted; hex in its tooltip)
  - Responsive — hides less-important columns on narrow screens
  - Compact (at the end of either table's strip): both tables' rows at about 70% height, remembered
  - Keyboard: Tab stops at the table; ↓/↑ go from row to row (into the rows at the selected one, else the first), Home/End to the ends, Enter or Space acts as a click (selects a node; in Subjects, opens a plot or call card), F2 renames a node, Escape leaves the rows. Same in the Subjects table, F2 aside
- View tabs (bottom of sidebar):
  - **Nodes** — node-centric table with per-node detail panel; ghost rows for displaced identities pinned to bottom. A strip over the table counts what needs a look, as the Graph's does (offline, displaced, unusual health or mode, and nodes that answer no request); clicking a count lists only those rows, clicking it again lists all. A ghost row keeps the alias given to its device, and its ✕ forgets the device only after asking. Types are named one way throughout (`uavcan.node.Heartbeat.1.0`), whether the backend gives a type's name or its Python class's
  - **Subjects** — subject-centric table listing all subjects and services across the network, with inline service expansion. Its strip lists all, subjects only or services only (remembered), and counts the silent, those with no publisher and those of unknown type, each count picking those rows out; the Kind column marks services only. Bytes/s is a subject's payload per second (message size × rate); Last seen says how long ago it was heard (`now` while messages come, `12s ago` in amber once silent), the time itself in its tooltip. A subject that stopped publishing says `silent` (amber) instead of its last rate; the table refreshes with the node poll every second, so it does even when nothing arrives. Both tables keep their scroll position across tab switches
  - **Graph** — D3 force-directed bipartite topology showing device and subject nodes with directional pub/sub links. Only what is unusual is coloured, as in the Nodes table: a node's ring and icon show ADVISORY/CAUTION (amber, `~`/`!`) and WARNING (red, `!!`); an offline node has a red dashed ring and says since when; a mode other than OPERATIONAL is named under the node. Edges carrying traffic are plain grey, their width following the message rate; an edge goes silent (amber, dashed) after three message periods (two seconds at least) without a message. A publisher's edge shows that publisher's own rate, and nothing flows to or from an offline device. A device whose node-ID another device took is drawn offline under the node-ID it last had. By default the layout is layered (**Display → Layout**): devices in one band, subjects in the other, ordered to cut crossings and wrapped to the canvas width, the same on every open; "force" is the free layout, which "Nodes only" always uses. Subjects are labelled by their ID; hovering a node shows its full name or type, and hovering an edge says what it carries (subject, publisher or receiver, rate or silent; for a service edge, the services). Enter in the filter goes to the best match (an ID typed in full first) and centres it; nodes can be reached with Tab and selected with Enter or Space. **Display → Export SVG / PNG** saves the drawing as it stands, framed on its content. A node heard only in Cyphal v1.1 (see `cyphal_v11` in `/api/status`) is drawn with a dotted amber ring, "v1.1 · not decoded", and counted in the strip; nothing about its ports or health is known. Edge widths and arrowheads keep one size on screen at any zoom, and the legend opens from **?**. A click selects a node; a drag moves and pins it (pinned nodes are filled). The view fits itself on first open; **Fit** does it again. **Display** holds the grid, link stats, traffic animation (off by default) and gravity. System subjects (Heartbeat, port.List, …) are hidden by default. A status strip counts what needs a look (offline, displaced, unusual health or mode, silent subjects, subjects with no publisher or of unknown type); clicking a count picks those nodes out. Selecting a node opens the inspector, docked to the right of the canvas (Esc closes it): a device's node-ID, status, health, mode, uptime and VSSC, and each port's own rate with a sparkline of its last minute (recorded while the tab is open), each port row selecting that subject; it hands over to the Nodes tab (**Open in Nodes**) or a subject's plot (**Plot in Subjects**). Every 5 s while the tab is open it asks the backend for recent lifecycle events (`GET /api/nodes/events`): a node that restarted in the last 15 minutes says so (amber), one sharing its node-ID with another says so (red), and the strip counts both. **Display → Service calls** (on by default) draws a dotted edge from a service's client to each online node serving it; standard services follow Hide system
  - **Compare** — independent graphs for side-by-side multi-series comparison with derived series, thresholds, markers, and freehand drawing
  - **DSDL** — searchable tree of loaded DSDL types with bus-activity badges, field-level search, dependency navigation, and a custom-type editor under `dsdl_messages/custom/`
  - **Record** — capture filtered events into per-recording SQLite stores with `max_length` / `max_events` caps, quick-save the last N seconds, edit limits on live recordings, duplicate, and CSV/JSON export
- Detail panel (below nodes table, nodes view):
  - Tabbed view: Publishers, Subscribers, Servers, Clients, Registers, History (with count badges)
  - Subject cards: each subject displayed as an individual card with subject ID, message type, rate, live dot indicator, and key-value metrics. A publisher's card shows its own rate, a subscriber's the subject's over all publishers; once messages stop, the card says `silent` and keeps the last values
  - Servers tab: expandable service cards with request forms, response display, and persistent call history fetched from backend
  - History tab: node lifecycle events (health/mode changes, service calls) with time-range filtering
  - Real-time D3 line plot: click a subject card to plot its numeric attributes over time. Its controls (pause, time window, Fill Rate, line width, points, grid) are the Subjects tab's plot's, and the two share their settings. A publisher's card plots that node's own messages; elsewhere (Subscribers tab, Subjects view) a subject several nodes publish plots one series per publisher (`value · n20`). A numeric array plots one series per element (`velocity[0]`, …, up to 16). At most 8 series show at first; the legend shows the others
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

- **Multi-series overlay** — a graph's Series row lists every series heard, a numeric field of a subject from one node (`S1300 · value · n21`), with its type and node name, by subject-ID. Type any words of them in its filter (`temp`, `1300 n21`, `imu`) to narrow the list, and tick or untick series: several at once, one click each. A series keeps to its node, so a node that starts publishing the subject later does not mix in. Series saved before publishers were told apart keep plotting what any publisher sends
- **From a plot** — the Nodes and Subjects plots have a Compare button: the series they show (a field per node) open in a new Compare graph named after the subject
- **Status strip** — over the graphs: how many graphs and series, then what needs a look: series gone quiet (by the rule the tables call a subject silent) and paused graphs; a count, clicked, brings the first such graph into view. With no graph, the tab says how to start
- **Derived series** — computed from raw series: delta, ratio, moving average and min/max envelope (each over the last N samples, so the envelope follows the signal), rate of change. Add, here and for a threshold, says what is still missing
- **Thresholds** — horizontal reference lines with labels, in the theme's red until a colour is picked from the legend. The y-axis fits what is in view (zooming in rescales it) and keeps the thresholds on it
- **Interactive legend** — a table, a row a series: its colour (click to change), line style (click to cycle: solid/dashed/dotted/dashdot/longdash + circle/square/triangle/diamond markers), name and unit (click to toggle visibility), its last, lowest and highest value over the time in view in columns that line up, and × to remove. A series with nothing to plot (not heard yet, or its history cleared) stays listed, muted, with – for its values, so it can be removed; each series keeps its colour. The name, style and × are buttons, so Tab and Enter work them too
- **Timeline markers** — Shift+click to place a named marker with optional note, color (the theme's accent until one is picked), and line style. Click on an existing marker to edit. Markers persist and export with the graph config. A graph's **Markers** row (under Edit) lists its markers by time, so one gone off screen is found: a click shows it (the graph pauses with it in the middle), × deletes it. The row counts the drawings too, with Clear, and while there are none of either, says how to add them. Removing a graph that has markers or drawings asks first.
- **Freehand drawing** — Alt+drag to draw annotations directly on the plot. Controls for color (the theme's red until one is picked), line style, and size are in the toolbar. Alt+double-click to clear all drawings.
- **Zoom and pan** — Ctrl+wheel (or a trackpad pinch) to zoom, drag to pan the X axis; the wheel alone scrolls the page. Double-click to reset zoom. A click on the plot pauses/resumes only with the graph's **Click pauses** on (Display; off by default, as a click by accident would pause it); the ⏸ button and Pause All always do. A paused graph keeps the points it showed, in a replay too.
- **Sync** — a graph's **Sync** button puts it in step with the other synced graphs, so they show the same time: pausing or resuming one, its time window, Ctrl+wheel zoom, drag pan, double-click reset, and showing a marker from its Markers row take them all. A graph that joins takes the group's view; Sync is kept with the graph. Synced graphs show each other's markers too: on hover, one says which graph it was marked on, where it is edited
- **Crosshair sync** — hovering over one graph shows synchronized crosshairs with live value readouts on all graphs. Values keep six significant digits; a series with no sample near the cursor (in a gap, or gone quiet) reads –
- **Presets** — save/load named graph configurations, whole: series, derived series, thresholds, markers, drawings, time window and display settings (Clone copies the same). A graph saved without a name gets a free one ("Untitled 2"), so it does not replace another
- **Export/Import** — full workspace export/import as JSON files. Import reads and checks the whole file before it changes anything, skips what is not valid, and asks before it replaces the graphs here
- **Per-graph controls** — time window, fill rate interpolation, stroke size, disconnected points, grid. A field's history keeps its newest 3600 points (36 s at 100 Hz, 6 min at 10 Hz): a window longer than a graph's series keep is dashed, and while the chosen one is, "kept 36 s" beside it says how much there is
- **Collapse** — ▴ in a graph's header collapses it to its plot, so graphs sit close together: the header and editing rows go, the legend is one line of names, and the pause, time window and Sync move to a column at the plot's right, with ▾ to expand it again. Kept with the graph
- **Edit** — a graph's header holds its name, time window and actions; its editing rows (Series, Derived, Thresholds, Display) fold away under its Edit button, so the plot gets the card. A new graph starts on its editing rows, a graph that has series on its plot
- **Says why a plot is empty** — not connected to the backend, CAN bus not connected, waiting for data, or no data in view; a live graph's time axis goes on when data stops

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

- Verify you are calling the correct backend URL.
- The backend answers browser pages only from its own address and from `localhost` / `127.0.0.1` (any port); anything else gets `403 cross-origin request refused`. Serve this frontend from the same machine, or open the dashboard the backend serves itself (see `WEBSOCKET_README.md`, "Browser origin policy").

### Port conflict on 5500

Run with another port:

```bash
python3 -m http.server 8088
```

Then open `http://localhost:8088`.
