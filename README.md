# Cynitor

[![Tests](https://github.com/OpenCyphal-Garage/cynitor/actions/workflows/test.yml/badge.svg?branch=dev)](https://github.com/OpenCyphal-Garage/cynitor/actions/workflows/test.yml)

A standalone dashboard for monitoring [Cyphal](https://opencyphal.org/) (UAVCAN) networks over CAN bus.

Cynitor watches the bus in real time, lists every node it discovers, and lets you drill into individual subjects to inspect message attributes and plot numeric values over time. GUI alternative to [yakut monitor](https://github.com/OpenCyphal/yakut).

![Cynitor dashboard](cynitor.png)

## Quick Start

Cynitor has two parts that run independently: a Python backend that talks to the CAN interface, and a static-file frontend that runs in any modern browser.

### 0. Clone with submodules

The regulated UAVCAN DSDL types live in a git submodule. Without them the backend starts but cannot decode any traffic.

```bash
git clone --recurse-submodules https://github.com/OpenCyphal-Garage/cynitor.git
# or, if you already cloned:
git submodule update --init --recursive
```

### 1. Backend

Requires **Python 3.10 or newer**.

```bash
cd cynitor/server
pip install -r requirements.txt   # pycyphal + nunavut (nnvg) + aiohttp + numpy
python3 main.py --can vcan0       # connect to a known interface
# or
python3 main.py                   # pick the interface from the UI later
```

The HTTP server starts on `http://localhost:8080`, and serves the dashboard
there as well as the API. Opening that address in a browser is enough; the
separate frontend server below is only needed if you are editing the frontend
and want to reload without restarting the backend.

#### Optional tools

These extend functionality but are not required to run the dashboard — the code degrades gracefully when each is absent.

- `pip install yakut` — needed for `yakut accommodate` (automatic node-ID assignment). Without it the backend logs a warning and starts with no auto-assigned ID.
- **Linux:** `sudo apt install can-utils` — provides `canbusload` for the bus-utilization sparkline. Without it utilization stays at 0%; everything else works.
- **Windows / macOS:** install the `python-can` backend your CAN adapter needs (PCAN, Kvaser, Vector, SLCAN-over-USB, …) — see [Platforms](#platforms) for the transport-spec syntax.
- `pip install -r server/requirements-dev.txt` — only if you want to run the backend test suite (adds `pytest` and `pytest-asyncio` on top of the runtime requirements).

### 2. Frontend

```bash
cd website
python3 -m http.server 5500
```

Open `http://localhost:5500` and click **Connect** in the sidebar.

## Features

- **Live node table** — sortable, filterable, with health, message rate, uptime, and per-row publisher/subscriber/server/client port lists. Pin favourites to the top with a star, hide offline nodes you don't care about.
- **Subject browser** — a second view (toggle via sidebar tabs) that lists every subject and service on the network. Expand any service inline to send requests to specific nodes without leaving the subject-centric view.
- **Per-subject inspection** — click a node, then a subject card in the detail panel, to see live message attributes and a 60-second history.
- **Service interaction** — invoke services on remote nodes with auto-discovered request schemas, expandable composite fields, and a persistent call history (stored in SQLite, survives restarts).
- **Node history** — lifecycle tracking with health/mode changes, service calls, and per-subject telemetry summaries. Retained for 30 days.
- **Network topology** — D3 force-directed graph of device and subject nodes with directional pub/sub links and animated live-traffic. Three view modes (nodes only / node-centric / subject-centric), drag-to-pin with persistent positions, adjacency highlighting, hide-system / hide-offline / per-node-or-subject hide with a restore badge, inline device rename, gravity bias by total links / channels / rate / payload, and per-link rate/payload overlays.
- **Multi-attribute plots** — each numeric attribute gets its own panel with its own y-axis, so a fast-growing uptime doesn't squash a small voltage reading. Interactive three-zone legend pills for color, line style, and visibility.
- **Hover crosshair + tooltip** with timestamp and per-series values that update in real time as data scrolls under the cursor. Click to pause, drag to pan, scroll to zoom, double-click to reset.
- **Compare view** — independent graphs for side-by-side multi-series comparison with derived series (delta, ratio, moving average, min/max, rate of change), thresholds, timeline markers (Shift+click), freehand drawing (Alt+drag), crosshair sync across graphs, and workspace export/import.
- **DSDL Inspector** — searchable tree of all loaded DSDL types with bus-activity indicators (which types are actually being seen on the wire), field-level search, and dependency navigation. Create, edit, and delete custom DSDL types under `dsdl_messages/custom/` with a compile-state lock.
- **Recordings** — capture filtered events into per-recording SQLite stores with `max_length` / `max_events` limits and `stop_on_limit`. Quick-save the last N seconds from the global buffer, duplicate a configuration with "New like this", edit limits on live recordings without stopping them, and export per recording as CSV or JSONL. Replay any recording through the live UI with play/pause/seek/speed controls.
- **Right log panel** — hidden by default, resizable; merges live `uavcan.diagnostic.Record` (subject 8184), any user-added text-bearing subject, and the backend's Python logs (polled from `/api/logs`) into one timeline. Per-source toggle pills with live count badges, severity floor across all sources, amber disconnect indicator when the backend is unreachable.
- **Dark / light theme**, sidebar collapse, resizable detail panel.
- **Auto-reconnect** on transient backend or frontend-server outages.
- **Persisted layout** — connection state, table sort, column widths, filters, theme, panel sizes all restored on reload from `localStorage`.

## Setup Modes

| Mode | Command | When to use |
|------|---------|-------------|
| Direct | `python3 main.py --can vcan0` | You know the interface and want CAN running on launch |
| Selection | `python3 main.py` | You want to pick the interface from the UI dropdown |

In selection mode the HTTP server starts immediately, but pycyphal is not initialized until the user posts to `/api/can/connect`. The same UI flow lets you disconnect and reconnect to a different interface without restarting the backend. Pass `--recompile` to force `nnvg` to regenerate compiled DSDL Python from `dsdl_messages/`.

## Platforms

**Linux** is the primary platform — SocketCAN (`vcan0`, `can0`, `slcan0`, …) is auto-discovered and the bus-load monitor uses `canbusload` from `can-utils`.

**Windows / macOS** are supported with reduced introspection. The interface dropdown will be empty (no SocketCAN equivalent), so connect by passing a full pycyphal transport spec — any string that contains `:` is forwarded to pycyphal unchanged:

```bash
# Windows example: PCAN USB via python-can
curl -X POST http://localhost:8080/api/can/connect \
  -H 'Content-Type: application/json' \
  -d '{"interface":"pythoncan:pcan:PCAN_USBBUS1"}'

# Or run the backend directly with the same string
python3 main.py --can pythoncan:pcan:PCAN_USBBUS1
```

The bus-load monitor self-disables when `canbusload` is not on PATH (utilization stays at 0%); the rest of the stack — REST/WebSocket server, DSDL Inspector, recordings, log panel, telemetry — works the same on all three OSes. Install whichever `python-can` backend your CAN adapter needs (PCAN, Kvaser, Vector, SLCAN-over-USB, …) and pass its transport string.

## Deploying to a Server

The backend serves the dashboard as well as the API, so a deployment is one
binary and clients need nothing but a browser.

Grab a build from the [latest release](https://github.com/OpenCyphal-Garage/cynitor/releases):
a `.deb`, an `.AppImage`, or the bare binary. All three contain the same
server and none of them open a window.

```bash
sudo dpkg -i cynitor-server_*_amd64.deb    # installs to /usr/bin, adds a systemd unit
# or just run it
chmod +x cynitor-server-*-x86_64.AppImage && ./cynitor-server-*-x86_64.AppImage
```

Nothing else is needed on that machine: no Python, no pip, no web server.

```bash
CYNITOR_AUTH_TOKEN=$(openssl rand -hex 24) ./cynitor-server --bind 0.0.0.0
```

The server prints the token at startup when run from a terminal, so you can
copy it straight out. It is written directly to the terminal rather than
logged, so supervised runs do not leak it into the system journal.

Point a browser at `http://<that-machine>:8080`. The dashboard loads, prompts
once for the token, and remembers it. It works out its own API address from
the page it was served from, so nothing needs configuring per client.

**Set a token whenever you bind beyond loopback.** Without one the API is open
to anyone who can reach the port, and that API commands nodes on the bus. The
server logs a warning if you bind to `0.0.0.0` with no token.

The dashboard's own files are served without a token, since a browser has to
load the page before it can ask for one. Only the API and the event stream are
protected.

### API only

If you are consuming the data with your own tooling rather than the dashboard,
`--no-frontend` serves just the REST API and the WebSocket stream:

```bash
CYNITOR_AUTH_TOKEN=... ./cynitor-server --bind 0.0.0.0 --no-frontend
```

Requests for the dashboard then return 404 while the API is unchanged. The
startup banner states which mode is running. See
[WEBSOCKET_README.md](WEBSOCKET_README.md) for the full API reference.

## Building It Yourself

Tagged releases attach prebuilt artifacts, so this is only needed for
unreleased changes or another architecture.

```bash
pip install -r server/requirements.txt pyinstaller
python3 server/startup_setup.py --recompile   # generate compiled DSDL first
cd packaging && ./build.sh                    # binary + .deb + .AppImage
```

`./build.sh binary` stops after the executable, which is all you need for a
plain copy-and-run deployment. The full run also needs `fakeroot` for the
package and downloads `appimagetool` on first use.

Everything lands in `packaging/out/`, with the bare executable at
`packaging/dist/cynitor-server`.

### Running it as a service

The `.deb` installs a systemd unit, disabled by default. Configure it in
`/etc/default/cynitor-server`, then:

```bash
sudo systemctl enable --now cynitor-server
```

Set `CYNITOR_AUTH_TOKEN` there before binding beyond loopback. The unit runs
as root because raw SocketCAN sockets need `CAP_NET_RAW`; the file comments
show how to narrow that to a dedicated user.

## Troubleshooting

**"Frontend Server Unavailable" overlay appears.** The static-file server on port 5500 stopped responding. Restart it with `cd website && python3 -m http.server 5500`. Cynitor reloads automatically once it's back.

**Backend won't connect to CAN.** Check that the interface exists (`ip link show vcan0`) and that you have permission to open it. If `yakut accommodate` fails, the backend logs a warning but still starts — the node ID just won't be auto-assigned. Check `/api/logs` or stderr for the full error.

**No nodes appearing.** Confirm there are publishers on the bus (`yakut sub uavcan.node.Heartbeat.1.0`). On a virtual interface (`vcan0`) you also need a publisher on the same `vcan` interface — the backend doesn't generate traffic on its own.


**Port 5500 in use.** Run `python3 -m http.server 8088` (or any free port) and open the matching URL.

## Documentation

- **[TECHNICAL.md](TECHNICAL.md)** — Architecture, component breakdown, data flow, project structure, extension points.
- **[WEBSOCKET_README.md](WEBSOCKET_README.md)** — Full REST + WebSocket API contract.
- **[RELEASE_NOTES.md](RELEASE_NOTES.md)** — Per-version changelog.
