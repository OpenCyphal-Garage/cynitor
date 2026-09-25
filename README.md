# Cynitor

[![Tests](https://github.com/OpenCyphal-Garage/cynitor/actions/workflows/test.yml/badge.svg?branch=dev)](https://github.com/OpenCyphal-Garage/cynitor/actions/workflows/test.yml)

A standalone dashboard for monitoring [Cyphal](https://opencyphal.org/) (UAVCAN) networks over CAN bus.

Cynitor watches the bus in real time, lists every node it discovers, and lets you drill into individual subjects to inspect message attributes and plot numeric values over time. GUI alternative to [yakut monitor](https://github.com/OpenCyphal/yakut).

![Cynitor dashboard](cynitor.png)

## Quick Start

Cynitor is a single server: it talks to the CAN interface and serves the dashboard to any modern browser. Run it, open the address it prints, and you are done.

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
pip install -r requirements.txt   # pycyphal + python-can + nunavut (nnvg) + aiohttp + numpy
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
- **Linux:** `sudo apt install can-utils` — provides `canbusload` for the bus-utilization sparkline on SocketCAN interfaces. Without it utilization stays at 0%; everything else works. Other adapters (see [Platforms](#platforms)) need nothing: Cynitor measures their load itself.
- **Windows / macOS:** install the `python-can` backend your CAN adapter needs (PCAN, Kvaser, Vector, SLCAN-over-USB, …) — see [Platforms](#platforms) for the transport-spec syntax.
- `pip install -r server/requirements-dev.txt` — only if you want to run the backend test suite (adds `pytest` and `pytest-asyncio` on top of the runtime requirements).

### 2. Open the dashboard

Point a browser at `http://localhost:8080` and click **Connect** in the
sidebar. That is the whole frontend: nothing to install, nothing to build.

Then pick the CAN interface from the list and click the second **Connect**.
For anything but SocketCAN, also choose the bus bitrate: the list starts
unselected, because a wrong guess disrupts the bus, and remembers the
bitrate you last used on each adapter. An adapter that is not listed can be
typed in under **Other…** (see [Platforms](#platforms)).

#### Editing the frontend

Only if you are changing `website/` and want reloads without restarting the
backend, serve it separately:

```bash
cd website && python3 -m http.server 5500
```

Then open `http://localhost:5500` instead. The address field defaults to
`http://localhost:8080`, which is where the backend is listening.

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
- **DSDL Inspector** — searchable tree of all loaded DSDL types with bus-activity indicators (which types are actually being seen on the wire), field-level search, and dependency navigation. Create, edit, compile and delete custom DSDL types, kept in the data folder (`dsdl/custom`, compiled into `dsdl/compiled`), with a compile-state lock. Compiling runs inside Cynitor, so it works in the packaged binaries too.
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

In selection mode the HTTP server starts immediately, but pycyphal is not initialized until the user posts to `/api/can/connect`. The same UI flow lets you disconnect and reconnect to a different interface without restarting the backend. Naming an interface that does not exist is not fatal: the server warns, lists what is available, and falls back to selection mode.

Other options:

| Flag | Effect |
|------|--------|
| `--bitrate <n>` | Bus speed in bit/s. Required with `--can` for every adapter Cynitor opens itself (PCAN, gs_usb, slcan, …); there is no default, because a wrong guess disrupts the bus. Ignored for SocketCAN, whose bitrate is set with `ip link` |
| `--bind <host>` | Listen on `<host>` (default `127.0.0.1`; `0.0.0.0` to expose on the network) |
| `--port <n>` | Listen on `<n>` instead of 8080 |
| `--data-dir <dir>` | Keep history, recordings and the allocator's node-ID table in `<dir>` instead of the default data folder (see [Where data is kept](#where-data-is-kept)) |
| `--no-frontend` | Serve only the API and WebSocket, not the dashboard |
| `--recompile` | Force `nnvg` to regenerate compiled DSDL from `dsdl_messages/` |
| `--version` | Print the version and exit |

## Where data is kept

Bus history (24 h), node history (30 days), service-call history, recordings,
remembered device names, and the allocator's table of which device got which
node-ID are kept in SQLite files in one data folder, whatever folder Cynitor
is started from:

| OS | Data folder |
|----|-------------|
| Windows | `%LOCALAPPDATA%\Cynitor` |
| Linux | `~/.local/share/cynitor` (`$XDG_DATA_HOME/cynitor`); `/var/lib/cynitor` for the systemd service |
| macOS | `~/Library/Application Support/Cynitor` |

`--data-dir <dir>` or the `CYNITOR_DATA_DIR` environment variable chooses
another. The startup banner shows which one is in use.

Custom DSDL types created in the DSDL Inspector live there too, in `dsdl/`.
Custom types from the source tree's `dsdl_messages/custom/` are copied (not
moved) into it on the first start.

Earlier versions wrote these files to the folder Cynitor was started from
(for the systemd service, `/`). If they are found there, they are moved into
the data folder once, on the first start; data already in the data folder is
never overwritten.

## Platforms

**Linux** is the primary platform — SocketCAN (`vcan0`, `can0`, `slcan0`, …) is auto-discovered and the bus-load monitor uses `canbusload` from `can-utils`.

**Windows / macOS** are supported with reduced introspection. The dashboard lists the adapters it can find — PEAK, Kvaser, Vector and IXXAT through their vendor drivers, CANable/candleLight adapters over USB, and CANable adapters with slcan firmware by their serial port. Anything else can be named as `<python-can interface>:<channel>`, under **Other…** in the dashboard or with `--can`; any string that contains `:` is passed to pycyphal as is, without being checked against the list. Unlike SocketCAN, these adapters run at whatever bitrate Cynitor opens them with, so you have to give it, and it has to match the bus. There is no default: a node joining at the wrong speed floods the bus with error frames.

```bash
# PEAK PCAN-USB
python3 main.py --can pcan:PCAN_USBBUS1 --bitrate 500000
# CANable / candleLight firmware
python3 main.py --can gs_usb:0 --bitrate 500000
# CANable / slcan firmware
python3 main.py --can slcan:COM5@115200 --bitrate 500000

# Or connect a running backend
curl -X POST http://localhost:8080/api/can/connect \
  -H 'Content-Type: application/json' \
  -d '{"interface":"pcan:PCAN_USBBUS1","bitrate":500000}'
```

On Windows, `pip install -r requirements.txt` also installs what candleLight adapters (`gs_usb`) need, including the libusb DLL. The older `pythoncan:pcan:PCAN_USBBUS1` spelling is still accepted.

Most such adapters can be opened by only one program at a time. Cynitor opens the adapter once and shares it between its own parts internally, so it works with them — but nothing else can use the adapter while Cynitor is connected. Stop other CAN tools first.

The node-ID Cynitor uses for itself is picked the way `yakut accommodate` does it (listen to heartbeats, choose a free one), without needing yakut.

For these adapters Cynitor measures bus load itself, from the frames passing through it, counted the way `canbusload` counts them (no stuffing bits). It also notices a CANable being unplugged and disconnects, as it does when SocketCAN reports the interface gone; other adapters' drivers report that themselves. What it cannot see off SocketCAN are the controller's error counters and error-passive/bus-off state. The rest of the stack — REST/WebSocket server, DSDL Inspector, recordings, log panel, telemetry — works the same on all three OSes. Install whichever `python-can` backend your CAN adapter needs (PCAN, Kvaser, Vector, SLCAN-over-USB, …) and pass its transport string.

## Deploying to a Server

The backend serves the dashboard as well as the API, so a deployment is one
binary and clients need nothing but a browser.

Grab a build from the [latest release](https://github.com/OpenCyphal-Garage/cynitor/releases):
a `.deb`, an `.AppImage`, or the bare binary for Linux, and a single `.exe`
for Windows 10 and 11 (x64). All of them contain the same server and none of
them open a window.

```bash
sudo dpkg -i cynitor-server_*_amd64.deb    # installs to /usr/bin, adds a systemd unit
# or just run it
chmod +x cynitor-server-*-x86_64.AppImage && ./cynitor-server-*-x86_64.AppImage
```

On Windows, run the `.exe` from a terminal, e.g.
`.\cynitor-server-0.8.0-windows-x86_64.exe`. It includes what candleLight
adapters (CANable) need; other adapters need their vendor's driver
installed (PEAK, Kvaser, Vector, IXXAT). Windows may warn about an
unrecognised app the first time, as the executable is not code-signed.

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

On Windows there is no `.deb` or AppImage, only the executable. From the
repository root, in PowerShell:

```powershell
pip install -r server/requirements.txt pyinstaller
python server/startup_setup.py --recompile
cd packaging; python -m PyInstaller cynitor-server.spec --noconfirm
.\smoke-test.ps1          # optional: what CI checks on every build
```

The result is `packaging\dist\cynitor-server.exe`.

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

**"Frontend Server Unavailable" overlay appears.** Only happens when the dashboard is served separately for frontend work: the static-file server on port 5500 stopped responding. Restart it with `cd website && python3 -m http.server 5500`. Cynitor reloads automatically once it's back. Served from the backend, this cannot occur.

**Backend won't connect to CAN.** Check that the interface exists (`ip link show vcan0`) and that you have permission to open it. If `yakut accommodate` fails, the backend logs a warning but still starts — the node ID just won't be auto-assigned. Check `/api/logs` or stderr for the full error.

**No nodes appearing.** Confirm there are publishers on the bus (`yakut sub uavcan.node.Heartbeat.1.0`). On a virtual interface (`vcan0`) you also need a publisher on the same `vcan` interface — the backend doesn't generate traffic on its own.


**Port already in use.** For the backend, pass `--port 9099` (or any free port). For the separate frontend server used during development, run `python3 -m http.server 8088` and open the matching URL.

## Documentation

- **[TECHNICAL.md](TECHNICAL.md)** — Architecture, component breakdown, data flow, project structure, extension points.
- **[WEBSOCKET_README.md](WEBSOCKET_README.md)** — Full REST + WebSocket API contract.
- **[Releases](https://github.com/OpenCyphal-Garage/cynitor/releases)** — Per-version notes and prebuilt downloads.
