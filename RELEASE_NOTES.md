## Cynitor v0.3.0

### Right log panel (new)

- **Collapsible/resizable right sidebar** — hidden by default, mirrors left-sidebar collapse + persistence; drag the left edge to resize, double-click to reset.
- **Cyphal log feed** — `uavcan.diagnostic.Record` (subject 8184) auto-streams; row layout is `time · n<id> · s<id> · MessageType · text`. Severity drives row tinting; alert/critical get a red wash.
- **Add text subjects** — `+` button opens a picker listing every subject whose payload carries a string field (excluding 8184). Pick any to feed into the log alongside diagnostics. Selection persists.
- **Server log toggle** — `SERVER` pill pulls Python `logging` records from `GET /api/logs` every 2 s. Pill turns amber with a corner dot when the backend is unreachable.
- **Per-source counters** — small live badge on each pill shows how many entries from that source are in the buffer.
- **Severity floor** — `Min: TRACE…ALERT` dropdown filters across all sources via mapped levels; user-added text subjects always show.
- **Buffer cap** — 2000-entry in-memory ring; never persisted.

### Record tab (new)

- **Per-recording event stores** — every recording streams matching events into its own SQLite-backed `recording_events` table, independent of the global buffer.
- **Picker UI** — select subjects, services, and nodes to filter what each recording captures.
- **Limits** — per-recording `max_length_seconds`, `max_events`, and `stop_on_limit` caps with progress bars. Unlimited recordings now show "· no limit" with a dashed bar so empty progress doesn't read as a stuck recording.
- **Quick-save** — snapshot the last N seconds from the global buffer into a dedicated recording.
- **Edit-limits modal** — change caps on a live recording without stopping it.
- **Duplicate** — "New like this" starts a fresh recording with the same filter + limits.
- **CSV / JSON export** per recording.
- New API: `GET/POST /api/recordings`, `GET/PATCH/DELETE /api/recordings/{id}`, `POST /api/recordings/{id}/stop`, `POST /api/recordings/quick`, `GET /api/recordings/{id}/export?format={csv,json}`, `GET /api/recordings/buffer`.

### DSDL Inspector (new)

- **DSDL tab** — searchable tree of all loaded DSDL types with bus-activity indicators (which types are actually being seen on the wire), field-level search, and dependency navigation.
- **Custom DSDL types** — create, edit, and delete user types under `dsdl_messages/custom/`; compile-state lock prevents edits while a recompile is in flight.
- New API: `GET /api/dsdl/status`, `GET /api/dsdl/namespaces`, `GET /api/dsdl/type/{full_name}`, `POST /api/dsdl/custom/namespace`, `GET /api/dsdl/custom/namespaces`, `POST /api/dsdl/custom/type`, `DELETE /api/dsdl/custom/type/{full_name}`, `POST /api/dsdl/compile`.

### Compare view

- **Compare view** — new sidebar tab with independent graphs for side-by-side multi-series comparison. Add any subject + attribute from the network to any graph.
- **Derived series** — computed from raw series at render time: delta (A−B), ratio (A/B), moving average (windowed), min/max envelope (rolling), and rate of change (Δv/Δt).
- **Thresholds** — horizontal reference lines with labels on compare graphs.
- **Timeline markers** — Shift+click to place a named vertical marker with label, optional note, color, and line style. Click an existing marker to edit or delete. Markers persist and export with graph config.
- **Freehand drawing** — Alt+drag to draw annotations directly on plots. Configurable color, line style, and stroke size via toolbar controls. Alt+double-click to clear all drawings.
- **Crosshair sync** — hovering over one compare graph shows synchronized crosshairs with live value readouts on all other graphs.
- **Workspace export/import** — serialize the full compare workspace (all graphs, presets, markers, drawings) as a JSON file.
- **Presets** — save and load named graph configurations in compare view.

### Plot improvements

- **Live tooltip updates** — tooltip values update in real time as data scrolls under a stationary cursor, instead of requiring mouse movement.
- **Click-to-pause** — single click pauses/resumes the plot. Double-click resets zoom and pan.
- **Drag-to-pan** — hold and drag to navigate the X axis. Scroll wheel to zoom centered on cursor.
- **Interactive three-zone legend** — color picker swatch (click to change), center label (click to toggle visibility), style indicator (click to cycle through 9 line styles: 5 dash patterns + 4 marker shapes), and remove button.
- **Marker shapes** — circle, square, triangle, and diamond markers for distinguishing overlapping series.

### Cross-platform

- **Windows and macOS support** for the backend. Any interface string containing `:` is passed through to pycyphal verbatim (e.g. `pythoncan:pcan:PCAN_USBBUS1` on Windows, `socketcan:vcan0` cross-platform); bare names like `vcan0` still implicitly prefix `socketcan:` to preserve the original Linux UX.
- **Graceful degradation** when Linux-only tools are absent: `BusLoadMonitor` self-disables when `canbusload` is not on `PATH` (utilization stays at 0%, no spurious "monitor exited" fatal disconnect); `_check_can_health` returns healthy on non-Linux instead of tripping the watchdog 3 seconds in.
- **Correct `PATH` separator** when extending `CYPHAL_PATH` / `PYTHONPATH` (`os.pathsep` instead of hardcoded `:`).
- Interface dropdown still populates from SocketCAN sysfs on Linux; on Windows/macOS it stays empty — connect by passing the full transport string via `POST /api/can/connect` or `--can <transport-spec>`.

### Fixes

- DSDL tree readability and sidebar tab alignment.
- Compare view smooth/pause interaction.
- Brush styling consistency in plots.
- Plot drawing controls now gated behind `opts.includeDraw` so views without drawing intent don't render unused toolbar buttons.

---

## Cynitor v0.2.2

### New Features

- **Network topology graph** — new Graph tab with D3 force-directed bipartite view showing device nodes (circles) and subject nodes (diamonds) with directional pub/sub links. Supports drag-to-pin with persistent positions, zoom/pan, adjacency highlighting, and a toggle to collapse subjects into direct device-to-device edges. Selection shows an info panel overlay with publishers, subscribers, and services.
- **Stable node identity** — nodes are tracked by hardware `unique_id` (16-byte ID) instead of ephemeral `node_id`. When a device loses its node_id slot (displaced by another device), it appears as a ghost entry showing last-known data. Ghost nodes are pinned to the bottom of the table with a delete button.
- **Node data persistence** — node snapshots (name, version, ports) are stored in SQLite and restored across server restarts, so ghost nodes retain their data.
- **Identity lifecycle events** — node history shows "Got node ID #X" and "Lost node ID #X" events when devices are assigned or displaced from a node_id slot.
- **Offline service data** — servers and clients tabs show last-known port data for offline and ghost nodes with a stale banner, instead of showing an empty "offline" message.

### UI Changes

- **Segmented view tabs** — Nodes/Subjects use a segmented control with sliding indicator; Graph is a separate outlined button below.
- **Ghost node display** — ghost rows show "last seen" time instead of blank uptime, and the ID column shows a delete button instead of a dash.

### API Changes

- `GET /api/nodes` response now includes ghost entries keyed as `uid:<hex>` with `node_id: null`, `last_node_id`, and `_ghost: true`
- `DELETE /api/identity/{unique_id}` — remove a ghost identity and its persisted data

### Bug Fixes

- Fix plot state leaking between nodes when switching selected node
- Fix servers tab showing nothing for offline nodes that had no cached schema
- Fix spurious "Ports changed" history events when a new device takes an existing node_id
- Fix node snapshots silently failing due to `deque.isoformat()` call on a deque instead of its last element
- Fix empty snapshots overwriting good data during identity displacement
- Fix ghost rows swapping positions with live nodes during table sort
- Fix ghost delete button color using nonexistent CSS variable

---

## Cynitor v0.2.1

### Improvements

- **Sidebar branding** — footer with Cynitor version, Cordicor logo, and Cyphal logo
- **Collapse bar on inline service detail** — collapsible header for expanded service rows in the subjects table
- **Row selection highlighting** — blue border highlight on selected rows in both nodes and subjects tables, with independent subject + service selection
- **Node history uses unique_id** — history and subject summary queries now filter by hardware unique ID instead of ephemeral node ID, so data follows the physical device across reboots

### API Changes

- `GET /api/nodes/{node_id}/history` accepts optional `unique_id` query parameter
- `GET /api/nodes/{node_id}/history/subjects` accepts optional `unique_id` query parameter
- `GET /api/services/{service_id}/history` accepts optional `node_id` and `unique_id` query parameters
- SQLite `events` and `node_history` tables gain a `unique_id TEXT` column (auto-migrated)

### Bug Fixes

- Fix view switching layout flash and detail panel height leaking between nodes and subjects views
- Fix plot state leaking between views (opening/closing a subject plot in one view no longer affects the other)
- Fix inline service detail blinking during 1s table refresh, plot open, and view switch (stash/unstash pattern)
- Fix plot time axis freezing when data stream pauses
- Fix rate display showing "0 Hz" instead of "<1 Hz" for slow-rate subjects
- Fix DSDL schema error permanently cached per-node, poisoning all services — now expires after 5 seconds
- Fix subject and service row selections being interdependent (unclicking one no longer clears the other)
- Fix nodes table dense layout issues with Tabulator virtual DOM
- Fix node history persistence to use unique_id for stable identity across node ID reassignments

---

## Cynitor v0.2.0

### New Features

- **Subject browser** — new sidebar view that lists every subject and service across the network in a single table. Expand any service inline to send requests to specific nodes without leaving the subject-centric view.
- **Persistent service call history** — service call results (including response bodies) are stored in SQLite and survive restarts. History is displayed as expandable entries showing timestamp, target node, latency, and full response.
- **Node lifecycle history** — tracks health changes, mode changes, and service calls per node with 30-day retention. Accessible via the History tab in the node detail panel.
- **Service schema race condition fix** — standard services (register access, GetInfo) now resolve immediately via on-the-fly import even before full node registration completes. The frontend auto-invalidates cached schemas that contain unresolved services.

### API Additions

- `GET /api/nodes/{node_id}/history` — node lifecycle events with time-range and event-type filtering
- `GET /api/nodes/{node_id}/history/subjects` — aggregated telemetry statistics per subject for a node
- `GET /api/services/{service_id}/history` — persistent service call history with response bodies and node enrichment

### Bug Fixes

- Fix SQL LIMIT-before-filter bug in service call history queries — `service_id` filtering now happens in SQL via `json_extract` instead of post-query Python filtering
- Fix `get_all_nodes_info()` called inside enrichment loop instead of once before it
- Fix "Waiting for response from node" message showing wrong node ID in subjects view
- Fix cross-view state contamination between nodes and subjects service panels (`.map()` index leak, `state.activeView` read during background renders)
- Fix Tabulator `tableBuilt` race condition causing JS errors on subjects view init
- Fix node selector button using stale closure reference in subjects view
- Add input validation for `limit` query parameter on history endpoints
- Add `_subjectServiceNodeId` to global state with proper disconnect cleanup

---

## Cynitor v0.1.0

**First public release** of Cynitor — a standalone GUI for monitoring and interacting with Cyphal (UAVCAN v1) nodes over CAN bus.

### Features

- **Node discovery** — automatic detection of nodes via Heartbeat and port.List, with health/mode/uptime tracking
- **Real-time telemetry** — WebSocket streaming of decoded UAVCAN messages with per-client filtering
- **Data visualization** — stacked time-series plots per subject with hover crosshairs, toggle-able series, and shared time axes
- **Register access** — read and write Cyphal node registers with type validation
- **Service calls** — invoke services on remote nodes with schema introspection and collapsible composite fields
- **Offline detection** — tracks disappeared nodes with stale banners and disabled controls
- **CAN health monitoring** — detects BUS-OFF, ERROR-PASSIVE, and interface disappearance with auto-disconnect
- **Bus load monitoring** — real-time utilization via `canbusload`
- **Event logging** — SQLite persistence with configurable retention
- **Plug-and-play allocator** — detects external allocator or starts a local one with periodic fallback checks
- **Light/dark themes** — light default with a refined dark option
- **Keyboard accessible** — ARIA semantics, focus-visible outlines, keyboard-navigable cards and tabs

### Architecture

- **Backend**: Python, asyncio, aiohttp, pycyphal
- **Frontend**: Static HTML/CSS/JS with D3 plots and Tabulator data grid
- **API**: REST + WebSocket, documented in `WEBSOCKET_README.md`

### License

MIT
