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
                                          |   |   |
                            static files  |   |   | WebSocket /ws
                                 + REST   |   |   |
                                          v   v   v
                                         +-----------+
                                         |  Browser  |
                                         +-----------+
```

One process serves everything: the REST API, the WebSocket stream, and the dashboard's own files. A deployment is that binary plus a browser.

The pipeline is unidirectional from bus to browser. The backend is event-pushed; the frontend pulls structural data (`/api/nodes`, `/api/status`) on a poll interval and consumes the live event stream over WebSocket.

During frontend development the dashboard is often served separately on `:5500` so it can be reloaded without restarting the backend. That is the only case where two ports are involved, and it is why `website/config.js` exists: served from the backend it is replaced by a generated version naming the API origin, while the static-server copy is an empty placeholder that leaves the built-in default in place.

## Backend

All backend code lives in `server/`. Python 3.10+, asyncio, aiohttp, pycyphal.

### Lifecycle

```
CANHub.start()             Non-SocketCAN only: open the adapter once, pick a free node-ID
        |
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
| `websocket_server.py` | aiohttp HTTP+WS server, REST endpoints, per-client WebSocket filtering, periodic metrics broadcast, node history and service call history endpoints; also serves `website/` so one binary hosts both the API and the dashboard |
| `event_logger.py` | SQLite persistence with batch writes, `asyncio.to_thread` for non-blocking I/O, configurable retention, node lifecycle history (30-day), service call history with response bodies |
| `allocator.py` | Node-ID allocator detection / fallback (CentralizedAllocator), 10s re-check |
| `can_config.py` | Interface-spec and bitrate rules: bare names mean SocketCAN, `--bitrate` is required for anything else, `UAVCAN__CAN__BITRATE` is published as `"<n> <n>"` |
| `can_discovery.py` | Lists the adapters the dashboard offers besides SocketCAN: python-can vendor detection (PEAK, Kvaser, Vector, IXXAT), and off Linux a gs_usb USB scan and slcan serial ports by USB ID; cached for 10 s in `AdapterCatalog` |
| `can_hub.py` | Opens a non-SocketCAN adapter once and bridges it to an in-process python-can `virtual` channel that the allocator probe, allocator and scanner all open instead; also picks Cynitor's node-ID from heartbeats on that channel |
| `startup_setup.py` | DSDL compilation via `nnvg`, sets `UAVCAN__CAN__IFACE` / `UAVCAN__CAN__MTU`, calls `yakut accommodate` for node ID |
| `node_identity_map.py` | Bidirectional `unique_id ↔ node_id` mapping with displacement detection, snapshot storage, and SQLite-backed persistence |
| `log_store.py` | In-memory deque (max 5000) fed by a `logging.Handler`; exposed via `/api/logs` |
| `dsdl_manager.py` | DSDL discovery, namespace tree, source/compiled state, custom-type CRUD under `dsdl_messages/custom/`, recompile orchestration |
| `replay.py` | Recording-replay engine: streams `recording_events` rows back through subscriber queues at controlled speed; mirrors `TelemetryManager`'s broadcast shape so the WS handler picks one source per session (telemetry XOR replay) |

### CLI flags

```
--can <iface>     CAN interface: a SocketCAN name (vcan0, slcan0, can0, ...) or a python-can spec
                  (gs_usb:0, pcan:PCAN_USBBUS1, slcan:COM5@115200, ...). Required for direct mode.
--bitrate <n>     Bus speed in bit/s. Required with --can for non-SocketCAN adapters; no default. Every component that
                  opens the bus reads it from UAVCAN__CAN__BITRATE, which prepare_runtime sets.
--recompile       Force `nnvg` to regenerate Python from DSDL even if outputs exist.
--bind <host>     Host/IP to bind the HTTP server to (default 127.0.0.1; use 0.0.0.0 to expose on the network).
--port <n>        TCP port to listen on (default 8080).
--version         Print the version and exit.
--no-frontend     Serve only the REST API and WebSocket, not the dashboard.
```

Without `--can`, the backend starts in selection mode. Use `POST /api/can/connect` with `{"interface": "..."}` to attach.

By default the dashboard is served from the same port as the API, so a deployment is one binary. `--no-frontend` turns that off for API-only deployments, where something other than the browser dashboard is consuming the data. A checkout with no `website/` directory is API-only regardless. The startup banner says which mode is active.

### DSDL

DSDL definitions live in `dsdl_messages/`:

```
dsdl_messages/
  public_regulated_data_types/    git submodule — uavcan/, reg/
```

`startup_setup.prepare_runtime()` runs `nnvg` to compile DSDL → Python under `python_compiled_messages/` (gitignored). The compiled output is cached; `--recompile` forces regeneration.

## Frontend

All frontend code lives in `website/`. Plain HTML/CSS/JS. No build step. D3 (CDN) for plots and force graph; Tabulator (CDN) for data tables. Concern-focused script files load in order:

| File | Role |
|------|------|
| `state.js` | Global `state` object, constants (`PLOT_COLORS`, `PLOT_TICK_MS`), basic helpers (`el`, `escapeHtml`, formatters), API helpers (`apiBase`, `requestJson`), settings load/save (debounced 250ms, flushed on `beforeunload`) |
| `cache.js` | Telemetry cache and per-node accessors: `cacheEvent`, `getNodeRate`, `getNodeHealthValue`, `getNodeVisualState`, `pruneNodeCache`, `buildSubjectDetailData` |
| `plot.js` | Multi-panel D3 time-series plot: axis setup, line rendering, hover crosshair + tooltip, interactive three-zone legend (color picker, line style cycling, visibility, remove), zoom/pan, timeline markers, freehand drawing, animation loop, resize handling |
| `compare-view.js` | Independent multi-graph compare view: multi-series overlay, derived series (delta, ratio, moving avg, min/max, rate), thresholds, presets, export/import workspace, per-graph controls, crosshair sync across graphs |
| `detail-panel.js` | Per-node detail panel: subject cards, tab rendering (publishers, subscribers, servers, clients, registers, history), plot integration, selection helpers |
| `nodes-table.js` | Tabulator init, formatters, row build (port arrays joined to strings to avoid spurious cell re-renders), favourite + ghost delete actions, ghost rows pinned to bottom |
| `services-panel.js` | Service interaction: schema fetch, request form rendering, send/repeat/copy, persistent call history from backend, view-isolated state (`forSubjects` parameter), offline fallback for stale schemas |
| `registers-panel.js` | Register read/write UI: renders register list, type validation, edit controls, offline caching |
| `history-panel.js` | Node lifecycle history: timeline rendering, event labels/badges, subject activity summary, time-range filtering |
| `subjects-panel.js` | Subject browser: Tabulator-based table of all subjects and services, inline service expansion with node selector, integrated persistent history. Uses a stash/unstash pattern to protect the inline service detail DOM node from Tabulator's virtual re-renders |
| `graph-view.js` | Network topology: D3 force-directed graph with three view modes (nodes only / node-centric / subject-centric), live link traffic, drag-to-pin with persistent positions, adjacency highlighting, info panel that follows the selected node, hide-system / hide-offline / per-node-or-subject hide with restore badge, inline device rename, gravity bias by metric |
| `dsdl-view.js` | DSDL Inspector tab: namespace tree with bus-activity badges, field-level search, dependency navigation, custom-type editor with compile-state lock |
| `record-view.js` | Record tab: subject/service/node pickers, per-recording cards with progress bars (with "no limit" rendering for unbounded recordings), live polling, edit-limits modal, duplicate, CSV/JSON export, Play-replay button on completed recordings |
| `replay-strip.js` | Replay playback strip above the main view: position scrub, speed selector, pause/resume/stop, MM:SS/MM:SS time display; polls `/api/replay/status` every 1 s; transitions to a "Finished" mode (Replay-again / Close) when the backend's `replay_ended` sentinel arrives with `finished: true` |
| `log-panel.js` | Right log panel: collapsible/resizable shell, ring buffer (cap 2000, not persisted), Cyphal feed (diagnostic.Record + user-added text subjects), Server poller (`/api/logs` every 2s), severity floor across sources, per-source toggle pills with count badges, disconnect indicator on the Server pill |
| `connection.js` | WebSocket lifecycle, REST polling (status, nodes, interfaces), throughput meter, semaphores, `disconnectAll` shared teardown; forwards every event to `ingestLogEvent` for the log panel |
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

Six views share the same WebSocket and REST data:
- **Nodes view** — Tabulator table of nodes (with ghost rows for displaced identities pinned to bottom), detail panel below with tabs (Publishers, Subscribers, Servers, Clients, Registers, History).
- **Subjects view** — Tabulator table of all subjects and services across the network. Services can be expanded inline with a node selector and request form. Both views use `services-panel.js` for service interaction but maintain isolated state via the `forSubjects` parameter pattern.
- **Graph view** — D3 force-directed graph showing device nodes (circles) and subject nodes (diamonds) with directional pub/sub links and animated live-traffic indicators. Three view modes (nodes only / node-centric / subject-centric), drag-to-pin with persistent positions, zoom/pan, adjacency highlighting, hide-system / hide-offline / per-node-or-subject hide with a restore badge, inline device rename, gravity bias by total links / channels / rate / payload.
- **Compare view** — independent graphs for side-by-side multi-series comparison.
- **DSDL view** — namespace tree of loaded types with bus-activity badges, custom-type editor.
- **Record view** — capture filtered events into per-recording SQLite stores with limits.

Independent of the views, the **Right log panel** (toggled from the right edge) is a unified timeline of Cyphal diagnostic messages, user-picked text subjects, and the backend's `/api/logs` stream.

### State

The global `state` object holds everything mutable: connection flags, timer IDs, latest-payloads, subject history (per `subject_id:attr_name` key), selection IDs, per-subject hidden plot series (`Map<sid, Set<attr>>`), and UI prefs. Mutations are direct; rendering reads `state` synchronously.

Service interaction state is duplicated per view to prevent cross-contamination: `serviceCallState` / `_subjectServiceCallState`, `expandedServiceId` / `_subjectExpandedServiceId`, and `_subjectServiceNodeId`. Shared rendering functions in `services-panel.js` accept a `forSubjects` boolean to read/write the correct slot. Plot subject selection is similarly isolated: `_nodesPlotSubject` and `_subjectsPlotSubject` are saved/restored on view switch so closing a plot in one view doesn't affect the other. Detail panel height and collapsed state are stored per-view (`_nodesDetailHeight`/`_nodesDetailCollapsed`, `_subjectsDetailHeight`/`_subjectsDetailCollapsed`) and swapped on view switch.

`localStorage` persistence (key `cynitor.dashboard.settings.v1`, with `pycyphal.dashboard.settings.v2` read once as a legacy fallback for users upgrading from the pre-rename build) is debounced 250ms and flushed on `beforeunload`. Heavy or transient data (telemetry payloads, full table rows) is *not* persisted — only layout/preferences.

### Cache pruning

`pruneNodeCache()` runs after each `/api/nodes` response and drops entries from `state.latestByNode`, `state.latestBySubject`, `state.subjectHistory`, and `metricMaxLen` whose subject/node is no longer advertised by any node. Lag is bounded by the polling interval (1–60s).

### Plot

The detail panel's plot in `detail-panel.js#renderPlot` is a stack of one mini-panel per visible numeric attribute, each with its own y-axis scale. Panels share a single x-axis at the bottom and a single hover crosshair that spans the full stack. Series are joined by attribute name (`(d) => d.name`) so a panel persists across renders and only its line/axis updates. Each panel has its own clipPath keyed by `subject_id` + attribute name to keep lines confined when values spike.

The plot redraws every `PLOT_TICK_MS` (100ms) while data is live; otherwise the loop pauses until the next user interaction or new event. The tooltip re-evaluates on each render tick at the stored cursor pixel position, so values update in real time as data scrolls under a stationary cursor. Synced crosshairs (in compare view) store a timestamp instead, which is re-broadcast from the source plot each tick.

The legend uses three-zone `<div>` pills: left swatch (click to pick color via native `<input type="color">`), center label (click to toggle visibility), and optional style indicator (click to cycle through 9 line styles: 5 stroke-dasharray + 4 marker shapes) and remove button. Legend items for derived series, thresholds, and compare series carry all four zones. The legend's full DOM is only rebuilt when the underlying series set changes — within a stable set, only classes update.

### Compare view

`compare-view.js` provides independent graphs for side-by-side multi-series comparison. Each graph has its own series list, derived series, thresholds, timeline markers, freehand drawings, time window, and animation loop. Graphs are stored in `state.compareGraphs` and persisted in localStorage.

**Derived series** are computed from raw series at render time via `_computeDerived()`: delta (A−B), ratio (A/B), moving average (windowed), min/max envelope (rolling), and rate of change (Δv/Δt). Each type declares its source count and optional window parameter.

**Zoom/pan** is implemented in `bindPlotTooltip`: wheel zoom scales `cfg._zoom` centered on cursor, drag translates `cfg._panOffset`. `computePlotScales` applies zoom and pan to the base X domain. Click toggles pause (with 250ms delay to distinguish from double-click); double-click resets zoom and pan.

**Timeline markers** (`cfg.markers[]`) are placed with Shift+click and rendered as vertical dashed lines with labels by `_renderMarkers`. Each marker has a timestamp, label, optional note, color, and line style. Clicking near an existing marker opens an inline edit form (`_openMarkerForm`) with save/delete.

**Freehand drawings** (`cfg.drawings[]`) are captured with Alt+drag. Points are stored as `{t, y}` (timestamp + normalized 0–1 panel height) so they scroll with the timeline. Drawing color, width, and dash style are configurable per-graph via toolbar controls. Alt+double-click clears all drawings.

**Crosshair sync** uses custom DOM events (`crosshair-sync`, `crosshair-hide`) dispatched on the `.compare-cards` container. Each plot stores both `_cursorMx` (local pixel) and `_syncedT` (received timestamp) and re-evaluates on every render tick.

**Fingerprint-based re-rendering**: `_renderCompareGraphNow` computes a string fingerprint from all render-affecting state (series data, time window, zoom, pan, styles, markers, drawings). If the fingerprint matches the previous render, the function returns early. Any state change invalidates the fingerprint via `cfg._fingerprint = ''`.

**Presets** save/load named graph configurations. **Export/Import** serializes the full workspace (all graphs + saved configs) as a JSON file.

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
    node_identity_map.py    Stable unique_id ↔ node_id mapping
    telemetry_manager.py    Event routing and caching
    websocket_server.py     REST + WebSocket server, serves the dashboard
    event_logger.py         SQLite event persistence
    allocator.py            Node-ID allocation management
    startup_setup.py        DSDL compilation, env setup
    log_store.py            In-memory log buffer for /api/logs
    dsdl_manager.py         DSDL discovery, namespace tree, custom-type CRUD
    replay.py               Recording replay engine (subscriber queues + timing)
    frame_capture.py        Raw CAN frame capture (transport-level tap)
    requirements.txt        Python runtime deps
    requirements-dev.txt    Adds pytest + pytest-asyncio for the test suite
    tests/                  pytest unit tests
  website/
    index.html              Layout
    state.js                Global state, settings, API helpers
    cache.js                Telemetry cache + per-node accessors
    plot.js                 Multi-panel D3 time-series plot
    compare-view.js         Multi-graph compare view
    detail-panel.js         Detail panel, tab rendering, subject cards
    nodes-table.js          Tabulator (nodes view) with ghost row support
    services-panel.js       Service interaction UI and persistent history
    registers-panel.js      Register read/write UI
    history-panel.js        Node lifecycle history timeline
    subjects-panel.js       Subject browser (subjects view) with inline service expansion
    graph-view.js           D3 force-directed network topology
    dsdl-view.js            DSDL Inspector view + custom-type editor
    record-view.js          Record tab: pickers, per-recording cards, export, replay launcher
    replay-strip.js         Replay playback strip: scrub, speed, pause/stop/finish-mode
    debug-view.js           Raw CAN frame debugging view (opt-in capture)
    log-panel.js            Right log panel: Cyphal + Server feeds, picker, filters
    connection.js           WS + polling + lifecycle
    app.js                  Boot, bindings, heartbeat, view switching
    config.js               Empty placeholder; the backend serves its own
    styles.css              Theme and layout
  dsdl_messages/
    public_regulated_data_types/   git submodule (uavcan/, reg/)
    custom/                        user-created DSDL types (gitignored content)
  packaging/
    build.sh                One-command build: binary, .deb, .AppImage
    cynitor-server.spec     PyInstaller spec (single-file binary)
    deb/                    control template, systemd unit, /etc/default file
    appimage/               AppRun, desktop entry, icon
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

## Packaging

`packaging/` freezes the server into one self-contained executable. A
deployment is that file plus a browser: the binary serves the REST API, the
WebSocket stream and the dashboard from the same port.

`build.sh` produces three artifacts from one executable: the bare binary, a
`.deb` and an `.AppImage`. None of them open a window — the dashboard is
served to a browser, so there is nothing to display locally.

The `.deb` is the one that earns its keep: it puts the binary on `PATH`,
ships a systemd unit with an `/etc/default` file for options, and declares
`Conflicts`/`Replaces` against the old `cynitor` package so upgrading from the
windowed builds is clean. The `.AppImage` is a convenience wrapper; the
PyInstaller binary is already self-contained and needs only libc, so the
AppImage adds packaging rather than portability.

`cynitor-server.spec` drives PyInstaller. Key concerns:

- **Hidden imports** — pycyphal and python-can use dynamic imports extensively. The spec enumerates every submodule our code touches.
- **Bundled data** — `python_compiled_messages/` (pre-compiled DSDL), `dsdl_messages/` (source definitions) and `website/` (the dashboard) are packed into the binary.
- **pydsdl's vendored parser** — pydsdl reaches `parsimonious` by prepending its own `third_party` directory to `sys.path`. That directory is not a package, so static analysis cannot follow the import; the spec ships the tree as data at the same relative path and aborts if pydsdl ever moves it.
- **Compiled DSDL is mandatory** — the spec aborts if `python_compiled_messages/` is absent, because `nnvg` is not bundled and the frozen binary cannot regenerate it. Run `python3 server/startup_setup.py --recompile` before building.
- **Project root detection** — `startup_setup.resolve_project_root()` returns `sys._MEIPASS` when frozen. It is the only frozen-aware code: `prepare_runtime()` derives `sys.path`, `PYCYPHAL_PATH` and `CYPHAL_PATH` from it, and runs before anything imports the generated `uavcan.*` packages, so no PyInstaller runtime hook is needed.

### Versioning and releases

The version lives in exactly one place, `server/version.py`, and the binary
reports it with `--version`.

To cut a release, bump that version first, then tag to match:

```bash
# edit server/version.py -> __version__ = "0.8.0"
git commit -am "Release 0.8.0" && git push
git tag v0.8.0 && git push origin v0.8.0
```

The tag triggers `build-server.yml`, which refuses to build when the tag and
the module disagree, so a `v0.8.0` tag cannot publish a binary named `0.7.0`.
The build is done on the oldest supported distribution on purpose: glibc is
forward compatible, so the artifact runs on newer systems but not the reverse.

Every release build runs the binary against a CAN interface that does not
exist. Direct-attach mode reaches the pycyphal, pydsdl and python-can imports
before it touches any device, so that exercises the whole chain and fails if
anything is missing from the bundle. Two releases shipped unusable before this
check existed, because the binary started and served the dashboard perfectly
and only failed on connect.

### Process lifetime

The frozen binary is a bootloader parent with the interpreter as its child. A
`SIGKILL` to the bootloader cannot be forwarded, so `main.py` arms
`PR_SET_PDEATHSIG` at startup to be signalled when its parent dies; otherwise
the server can outlive whatever started it and keep holding port 8080.
`SIGTERM` is routed into the existing interrupt path so the CAN session and
HTTP server shut down in order.

The orphan guard compares against the parent recorded at startup rather than
against pid 1, because an orphan is reparented to the nearest subreaper, which
is only init when no other one is registered.

## Testing

Backend unit tests live in `server/tests/`:

```bash
cd server && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/ -v -p pytest_asyncio.plugin
```

`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` prevents ROS2 plugin conflicts in some environments. Required: `pip install pytest pytest-asyncio aiohttp`.

Frontend e2e tests live in `tests/e2e/` and serve `website/` themselves (or reuse a server already on port 5500):

```bash
pip install -r tests/e2e/requirements.txt && playwright install chromium
cd tests/e2e && python3 test_landing_page.py
```

## Conventions

- `rem` units only (no pixels) in CSS.
- `aria-label` on every icon-only button.
- Sanitize all dynamic HTML with `escapeHtml()` before insertion.
- Mutations go through the global `state` object; no per-component state.
- No new external dependencies beyond D3 and Tabulator (both via CDN).
- All `.md` files in this repo are written in English.
