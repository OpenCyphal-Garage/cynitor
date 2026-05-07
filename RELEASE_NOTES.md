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
