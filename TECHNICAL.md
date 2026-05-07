# Cynitor — Technical Reference

For developers working on Cynitor itself. User-facing intro: [README.md](README.md). API specifics: [WEBSOCKET_README.md](WEBSOCKET_README.md).

## Architecture

```
 CAN Bus (vcan0, slcan0, ...)
       |
       v
 +------------------+     asyncio.Queue     +--------------------+
 |   ScannerNode    | ---------------------> | TelemetryManager   |
 |                  |     (JSON events)      |                    |
 | - heartbeat sub  |                        | - caches latest    |
 | - port list sub  |                        |   by subject_id    |
 | - per-publisher  |                        |   and node_id      |
 |   subscribers    |                        | - broadcasts to    |
 | - service clients|                        |   all subscribers  |
 +------------------+                        +--------------------+
                                                |       |
                                                v       v
                                       WebSocketServer  EventLogger
                                       (aiohttp :8080)  (SQLite)
                                              |  |
                                       REST   |  | WebSocket /ws
                                              v  v
                                         +----------+
                                         | Frontend |
                                         | :5500    |
                                         +----------+
```

The pipeline is unidirectional from bus to browser. The backend is event-pushed; the frontend pulls structural data (`/api/nodes`, `/api/status`) on a poll interval and consumes the live event stream over WebSocket.

## Backend

All backend code lives in `server/`. Python 3.10+, asyncio, aiohttp, pycyphal.

### Lifecycle

```
prepare_runtime()          Set env vars, compile DSDL via nnvg
        |
AllocatorManager.start()   Detect external allocator on node-ID 1, or start a local one
        |
ScannerNode()              Begin CAN network discovery
        |
TelemetryManager.start()   Consume scanner events, cache + broadcast
        |
WebSocketServer.start()    REST + WebSocket on :8080
        |
EventLogger.start()        SQLite persistence
```

### Components

| File | Role |
|------|------|
| `main.py` | Entry point, lifecycle orchestration, two startup modes (direct vs selection) |
| `scanner_node.py` | CAN network discovery: heartbeat + port list subscriptions, dynamic per-subject subscribers, per-service clients, service schema introspection with STANDARD_SERVICES fallback |
| `node_info.py` | Per-node lifecycle state: appearance, last-heartbeat timestamp, disappearance threshold (>1.1s), port lists, GetInfo response |
| `telemetry_manager.py` | Event router: maintains `latest_by_subject` and `latest_by_node` caches, broadcasts to subscriber queues |
| `websocket_server.py` | aiohttp HTTP+WS server, REST endpoints, per-client WebSocket filtering, periodic metrics broadcast, node history and service call history endpoints |
| `event_logger.py` | SQLite persistence with batch writes, `asyncio.to_thread` for non-blocking I/O, configurable retention, node lifecycle history (30-day), service call history with response bodies |
| `allocator.py` | Node-ID allocator detection / fallback (CentralizedAllocator), 10s re-check |
| `startup_setup.py` | DSDL compilation via `nnvg`, sets `UAVCAN__CAN__IFACE` / `UAVCAN__CAN__MTU`, calls `yakut accommodate` for node ID |
| `log_store.py` | In-memory deque (max 5000) fed by a `logging.Handler`; exposed via `/api/logs` |

### CLI flags

```
--can <iface>     CAN interface name (vcan0, slcan0, can0, ...). Required for direct mode.
--recompile       Force `nnvg` to regenerate Python from DSDL even if outputs exist.
```

Without `--can`, the backend starts in selection mode. Use `POST /api/can/connect` with `{"interface": "..."}` to attach.

### DSDL

DSDL definitions live in `dsdl_messages/`:

```
dsdl_messages/
  public_regulated_data_types/    git submodule — uavcan/, reg/
```

`startup_setup.prepare_runtime()` runs `nnvg` to compile DSDL → Python under `python_compiled_messages/` (gitignored). The compiled output is cached; `--recompile` forces regeneration.

## Frontend

All frontend code lives in `website/`. Plain HTML/CSS/JS. No build step. D3 (CDN) for plots; Tabulator (CDN) for the node table. Six concern-focused script files load in order:

| File | Role |
|------|------|
| `state.js` | Global `state` object, constants (`PLOT_COLORS`, `PLOT_TICK_MS`), basic helpers (`el`, `escapeHtml`, formatters), API helpers (`apiBase`, `requestJson`), settings load/save (debounced 250ms, flushed on `beforeunload`) |
| `cache.js` | Telemetry cache and per-node accessors: `cacheEvent`, `getNodeRate`, `getNodeHealthValue`, `getNodeVisualState`, `pruneNodeCache`, `buildSubjectDetailData` |
| `detail-panel.js` | Per-node subject cards, multi-panel D3 plot, hover crosshair + tooltip, toggle-pill legend, selection helpers |
| `nodes-table.js` | Tabulator init, formatters, row build (port arrays joined to strings to avoid spurious cell re-renders), favourite + delete actions |
| `services-panel.js` | Service interaction: schema fetch, request form rendering, send/repeat/copy, persistent call history from backend, view-isolated state (`forSubjects` parameter) |
| `subjects-panel.js` | Subject browser: Tabulator-based table of all subjects and services, inline service expansion with node selector, integrated persistent history. Uses a stash/unstash pattern to protect the inline service detail DOM node from Tabulator's virtual re-renders |
| `connection.js` | WebSocket lifecycle, REST polling (status, nodes, interfaces), throughput meter, semaphores, `disconnectAll` shared teardown |
| `app.js` | Boot file: DOM event wiring (`bind`), settings restore, frontend-server heartbeat, sidebar view tab switching |

Top-level `const`/`let` declarations are shared globals across script tags (no modules). Cross-file references resolve at function-call time, not at load time, so script order matters: any dependent file must come after the one declaring its symbols.

### Data flow

```
Page load → loadSettings() → bind() → updateSemaphores()
            ↓ (auto-reconnect from saved state)
            connectDashboard() → /api/status
                ↓
            startStatusPolling() (5s) + startInterfacePolling() (3s while not yet on CAN)
                ↓
            User clicks CAN Connect (or another client did) → connectCan()
                ↓
            schedulePostCanStartup(5s) — wait for backend pipeline to come up
                ↓
            getAllNodes() (one-shot) + startNodesPolling() (slider 1-60s)
            startThroughputTimer() (1s) + connectWs()
                ↓
            ws.onmessage → cacheEvent() → scheduleDetailRefresh() (100ms debounce)
                                        + scheduleTableRefresh() (1s debounce)
```

The frontend never blocks on a single source. WebSocket is for live events; REST is for structural snapshots and connection metadata.

Two views share the same WebSocket and REST data:
- **Nodes view** — Tabulator table of nodes, detail panel below with tabs (Publishers, Subscribers, Servers, Clients, Registers, History).
- **Subjects view** — Tabulator table of all subjects and services across the network. Services can be expanded inline with a node selector and request form. Both views use `services-panel.js` for service interaction but maintain isolated state via the `forSubjects` parameter pattern.

### State

The global `state` object holds everything mutable: connection flags, timer IDs, latest-payloads, subject history (per `subject_id:attr_name` key), selection IDs, per-subject hidden plot series (`Map<sid, Set<attr>>`), and UI prefs. Mutations are direct; rendering reads `state` synchronously.

Service interaction state is duplicated per view to prevent cross-contamination: `serviceCallState` / `_subjectServiceCallState`, `expandedServiceId` / `_subjectExpandedServiceId`, and `_subjectServiceNodeId`. Shared rendering functions in `services-panel.js` accept a `forSubjects` boolean to read/write the correct slot. Plot subject selection is similarly isolated: `_nodesPlotSubject` and `_subjectsPlotSubject` are saved/restored on view switch so closing a plot in one view doesn't affect the other. Detail panel height and collapsed state are stored per-view (`_nodesDetailHeight`/`_nodesDetailCollapsed`, `_subjectsDetailHeight`/`_subjectsDetailCollapsed`) and swapped on view switch.

`localStorage` persistence (key `pycyphal.dashboard.settings.v2`) is debounced 250ms and flushed on `beforeunload`. Heavy or transient data (telemetry payloads, full table rows) is *not* persisted — only layout/preferences.

### Cache pruning

`pruneNodeCache()` runs after each `/api/nodes` response and drops entries from `state.latestByNode`, `state.latestBySubject`, `state.subjectHistory`, and `metricMaxLen` whose subject/node is no longer advertised by any node. Lag is bounded by the polling interval (1–60s).

### Plot

The detail panel's plot in `detail-panel.js#renderPlot` is a stack of one mini-panel per visible numeric attribute, each with its own y-axis scale. Panels share a single x-axis at the bottom and a single hover crosshair that spans the full stack. Series are joined by attribute name (`(d) => d.name`) so a panel persists across renders and only its line/axis updates. Each panel has its own clipPath keyed by `subject_id` + attribute name to keep lines confined when values spike.

The plot redraws every `PLOT_TICK_MS` (100ms) while data is live; otherwise the loop pauses until the next user interaction or new event.

The legend is a row of toggle-pill `<button>` elements. Clicks update `state.hiddenPlotSeries[sid]` and flip the button's `.active` class immediately. The legend's full DOM is only rebuilt when the underlying series set changes (different attribute names appear) — within a stable set, only classes update, so click targets don't get destroyed under the cursor.

## Telemetry event format

Every event flowing from `ScannerNode` through `TelemetryManager` to consumers uses this JSON shape:

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

Consumers identify a series by `(subject_id, attribute)`. The frontend keeps a per-attribute time-series under `state.subjectHistory["{subject_id}:{attr_name}"]` capped at 3600 samples per key.

## Project structure

```
cynitor/
  server/
    main.py                 Entry point, lifecycle orchestration
    scanner_node.py         CAN network discovery and subscriptions
    node_info.py            Per-node state tracking
    telemetry_manager.py    Event routing and caching
    websocket_server.py     REST + WebSocket server
    event_logger.py         SQLite event persistence
    allocator.py            Node-ID allocation management
    startup_setup.py        DSDL compilation, env setup
    log_store.py            In-memory log buffer for /api/logs
    requirements.txt        Python deps
    tests/                  pytest unit tests
  website/
    index.html              Layout
    state.js                Global state, settings, API helpers
    cache.js                Telemetry cache + per-node accessors
    detail-panel.js         Detail panel + multi-panel plot
    nodes-table.js          Tabulator (nodes view)
    services-panel.js       Service interaction UI and persistent history
    subjects-panel.js       Subject browser (subjects view) with inline service expansion
    connection.js           WS + polling + lifecycle
    app.js                  Boot, bindings, heartbeat, view switching
    styles.css              Theme and layout
  dsdl_messages/            DSDL type definitions (public_regulated_data_types submodule)
  python_compiled_messages/ nnvg output (gitignored)
  README.md                 User-facing intro
  TECHNICAL.md              This file
  WEBSOCKET_README.md       API contract reference
  CLAUDE.md                 Project instructions for Claude Code
```

## Extending

### Adding a new REST endpoint

1. Add a route in `server/websocket_server.py:_setup_routes`.
2. Implement the handler. Use `self.session` for backend access.
3. Document it in `WEBSOCKET_README.md`.
4. (Optional) Wire it from the frontend in `connection.js`.

### Adding a new sidebar control

1. Add HTML in `website/index.html` under `.sidebar`.
2. Bind it in `website/app.js`'s `bind()` function.
3. If it's a setting that should persist, add it to `saveSettings`/`loadSettings` in `state.js`.

### Adding a new detail-panel tab

1. Add `<button class="detail-tab" data-tab="...">...</button>` in `index.html`.
2. Add the tab name to `validTabs` in `state.js#loadSettings`.
3. Handle the tab in `detail-panel.js#renderSelectedNodeContent`.

### Adding a new plot derivative

The plot in `detail-panel.js#renderPlot` operates on `state.subjectHistory["{subject_id}:{attr}"]` time series. Per-attribute panels are joined by attribute name; new attributes appear automatically once they arrive in cached events.

## Testing

Backend unit tests live in `server/tests/`:

```bash
cd server && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/ -v -p pytest_asyncio.plugin
```

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` prevents ROS2 plugin conflicts in some environments. Required: `pip install pytest pytest-asyncio aiohttp`.

Frontend e2e tests live in `.claude/tests/` and require the frontend running on port 5500:

```bash
cd .claude/tests/01_landing_page && python test.py
```

## Conventions

- `rem` units only (no pixels) in CSS.
- `aria-label` on every icon-only button.
- Sanitize all dynamic HTML with `escapeHtml()` before insertion.
- Mutations go through the global `state` object; no per-component state.
- No new external dependencies beyond D3 and Tabulator (both via CDN).
- All `.md` files in this repo are written in English.
