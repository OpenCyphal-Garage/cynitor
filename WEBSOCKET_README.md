# Cynitor server: REST and WebSocket API

The API reference of Cynitor's server: the REST endpoints and the WebSocket stream the dashboard uses, for scripts and other clients.

## Architecture

```
UAVCAN Network
      ↓
ScannerNode (reads CAN messages)
      ↓
message_queue (event buffer)
      ↓
TelemetryManager (pub-sub router)
      ├→ WebSocket Server (live streaming)
      ├→ EventLogger (SQLite persistence)
      └→ CLI/Debug Tools
```

## Features

✅ **Real-time Streaming** - WebSocket support for multiple concurrent clients  
✅ **Event Filtering** - Filter by subject_id, node_id, or message_type  
✅ **REST API** - Query latest events via HTTP  
✅ **Log API** - Retrieve recent application logs with log-level filtering  
✅ **Optional Persistence** - SQLite logging for historical replay  
✅ **Browser Origin Policy** - The API accepts browser requests only from its own dashboard and from pages served on this machine  
✅ **Clean Shutdown** - Graceful WebSocket disconnect and logger queue flush  
✅ **Decode on Demand, a switch** - For a bus that carries more than can be decoded: only the subjects a dashboard shows or a recording takes are decoded, the rest counted from their frames, so every rate still shows  
✅ **Node-ID Allocator, on request** - Off by default. Turned on, Cynitor gives plug-and-play nodes node-IDs nothing uses on the bus, from its own node-ID, and stops answering if another allocator answers too
✅ **CAN Health Monitoring** - Detects CAN bus faults (BUS-OFF, interface disappearance on SocketCAN; a failing or unplugged adapter otherwise) and auto-disconnects; errors on a bus still in use (ERROR-WARNING, ERROR-PASSIVE, error counters growing) are shown and logged once instead
✅ **Bus Load Monitoring** - Real-time CAN bus utilization via `canbusload` subprocess on SocketCAN, or counted from the forwarded frames for other adapters (Classic CAN and CAN FD alike), streamed to clients via WebSocket  
✅ **Register Access** - Read and write Cyphal node registers via REST API  
✅ **Offline Node Detection** - Tracks node disappearance with `last_seen` timestamps and stale state handling  
✅ **Node History** - Lifecycle event tracking (health changes, mode changes, service calls) with 30-day retention  
✅ **Service Call History** - Persistent service call log with response bodies, queryable by service ID  
✅ **Node Identity Map** - Tracks stable hardware identity (unique_id) across node_id re-allocations, with state migration  

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

Key dependencies:
- `pycyphal` - UAVCAN protocol library
- `aiohttp` - Async HTTP/WebSocket server
- CORS and the origin policy are handled by built-in middleware (no extra dependency)

### 2. Run the System

Start directly:

```bash
python3 main.py --can vcan0
```

Or omit `--can` and select via startup API:

```bash
python3 main.py
```

Output:
```
============================================================
SERVER RUNNING
============================================================
Bound to:    http://127.0.0.1:8080/
REST API:    http://localhost:8080/api/
Health:      http://localhost:8080/api/health
Status:      http://localhost:8080/api/status
Mode:        selection  (waiting for the UI or POST /api/can/connect)
------------------------------------------------------------
Options:     --help lists them
============================================================
```

In direct mode (`--can <iface>`) the `Mode:` line reads `direct (attached to <iface> at startup)` and the block ends with a hint pointing back at selection mode. A bind beyond loopback is refused without a token unless `--insecure-no-auth` is given, and then a `WARNING` line says so. HTTP requests are not logged unless the server runs with `--access-log`.

In this mode, the server starts on port 8080 immediately. Use the REST API to connect:
- `GET /api/status` (lists available CAN interfaces and connection state)
- `POST /api/can/connect` with JSON body `{"interface":"vcan0"}`

Once connected, CAN telemetry begins. Disconnect with `POST /api/can/disconnect`.

By default the server binds to `127.0.0.1` (localhost only). To reach it from another host, pass `--bind` with a bearer token set in the `CYNITOR_AUTH_TOKEN` environment variable; a bind beyond loopback without one is refused at start (exit 2) unless `--insecure-no-auth` is given:

```bash
CYNITOR_AUTH_TOKEN=$(openssl rand -hex 24) python3 main.py --can vcan0 --bind 0.0.0.0
```

With the variable set, every REST and WebSocket request outside `/api/health` must present the token. Static dashboard assets are exempt: a browser has to load the page before it can prompt for a token, and the markup is not secret. Concretely, `/ws`, `/api` and everything under `/api/` are protected; every other path is served open.

Protected requests must present the token as:
- REST: `Authorization: Bearer <token>` header
- WebSocket and downloads: `?token=<token>` query parameter (browsers can't attach custom headers on WS handshakes or links); it is taken on `GET` requests only

A wrong token waits a quarter of a second for its `401`. The server warns at start if the token is shorter than 16 characters.

Unauthenticated requests get `HTTP 401 {"error": "missing or invalid token"}`. With the variable unset the server runs open — same behaviour as before this option existed. The frontend prompts the user to paste the token on the first 401 and stores it in `localStorage` under `cynitor.auth.token`.

When a terminal is attached, the server prints the token once at startup so it can be copied into the dashboard. It is written straight to stderr rather than logged: the log buffer is served through `/api/logs` and rendered in the dashboard's log panel, and a service manager captures stdout into the system journal, so logging it would scatter copies. Runs without a terminal, which is every supervised run, print nothing.

#### Browser origin policy

The API commands nodes on the bus, and any website a user visits could otherwise send requests to `localhost:8080` through their browser. So requests to `/ws`, `/api` and `/api/...` are refused with `HTTP 403 {"error": "cross-origin request refused"}` when their `Origin` header names any page other than:

- the dashboard this server serves itself (the `Origin` matches the `Host` it was reached at), or
- the frontend dev server on this machine (`localhost`, `127.0.0.1` or `[::1]`, port 5500), and any other origin listed in `CYNITOR_DEV_ORIGINS` (comma-separated, e.g. `http://localhost:3000`).

A request the browser marks `Sec-Fetch-Site: cross-site` (sent by a page on another site, even one with no `Origin`, such as an `<img>`) is refused with `403`.

Requests without an `Origin` header (curl, scripts, other non-browser clients) are not affected. `Origin: null`, which sandboxed frames and `file://` pages send, is refused. Allowed browser origins get `Access-Control-Allow-Origin` echoed back; nobody gets `*`.

While the server listens on loopback only (the default `--bind 127.0.0.1`), the `Host` header must also name loopback. This stops DNS rebinding, where a site re-points its own name at `127.0.0.1` so its requests look same-origin. Behind a reverse proxy, the proxy must pass the browser's original `Host` on (nginx: `proxy_set_header Host $host;`) and the server must not be bound to loopback only; set a token.

This works alongside the token, not instead of it: set `CYNITOR_AUTH_TOKEN` whenever the server is reachable from other machines.


### Serving the dashboard

When a `website/` directory is present next to the server (a source checkout, or bundled inside the frozen binary), the dashboard is served from the same port as the API:

| Route | Serves |
|-------|--------|
| `GET /` | `website/index.html` |
| `GET /config.js` | Generated: `window.__CYNITOR = {"apiBase": "<this request's origin>", "version": "<server version>"}` |
| `GET /<path>` | Any other file under `website/` |

These are registered after the API routes, so `/api/*` and `/ws` always win over the catch-all static mount.

They are sent with `Cache-Control: no-cache`, so a browser checks for a newer version before reusing a cached file (an unchanged file costs a `304`). After an upgrade the dashboard is never a stale mix of old and new files. API responses are unaffected.

Start the server with `--no-frontend` to omit them entirely, for deployments where something other than the dashboard consumes the API. Those three routes then return `404` and everything else is unchanged.

`config.js` is how a browser-served dashboard learns its API address. The checked-in `website/config.js` is an empty placeholder, which is what a separate static file server on port 5500 delivers, leaving the address field at its built-in default. Served from the backend, the generated version wins and points the page at the origin it was fetched from, so no per-client configuration is needed. It also names the server's version, which the sidebar footer shows; with the placeholder the footer shows none.

### 3. Runtime Environment

At startup, Python setup compiles the DSDL if needed and sets `CYPHAL_PATH` and `PYCYPHAL_PATH` (DSDL locations). A CAN session's interface, rates and node-ID are the session's own and set no environment variable. Cynitor reads `UAVCAN__NODE__ID` once at start, as a node-ID the user fixed; without it each session picks a free one on its bus (see `own_node_id` in `GET /api/status`).

### 4. Allocator Behavior

Cynitor has a plug-and-play node-ID allocator (`uavcan.pnp.NodeIDAllocationData`, versions 1 and 2), which is **off unless asked for**: a bus may have one allocator only, and one cannot be told from any other node until it answers a request, so Cynitor does not guess. Turn it on for a bus that has none of its own:
- `--allocator` on the command line, for sessions started with `--can` and for connect requests that do not say;
- `"allocator": true` in `POST /api/can/connect`, for that session (`false` turns it off for a server started with `--allocator`);
- the **Node-ID allocator** box in the dashboard's connect form, which always says one or the other.

Turned on:
- It answers from Cynitor's own node-ID, the scanner's, so Cynitor still uses one node-ID. No node-ID is reserved for it; node-ID `1` is like any other.
- A node that asks is given a node-ID no node has used on the bus in this session, online or not, and the same one if it asks again. Without a preference (version 1, Classic CAN) that is the highest free one, from 125 down; a version 2 request (CAN FD) names the one it would like, and gets the nearest free one. 126 and 127 are left to diagnostic tools.
- If another node answers a request too, Cynitor's allocator answers no more for the rest of the session, says so in the log, and `GET /api/status` names that node.
- What it gave is forgotten when CAN disconnects: the table belongs to the bus, and the next one may be another.
- It never runs while a raw log plays, and not when Cynitor has no node-ID of its own.

### 5. Decode on Demand

By default every subject whose type is known is decoded, fanned out to the dashboards and written to the 24 h history, whether anyone looks at it or not. That takes one core, and a busy bus carries more than one core decodes (see [Performance Considerations](#performance-considerations)). **Decode on demand** is the switch for such a bus. It is off unless turned on:
- `--decode-on-demand` on the command line;
- `POST /api/decoding` with `{"on_demand": true}`, at any time, connected to CAN or not (`false` turns it off);
- the **Decode on demand** box in the dashboard's Server section.

While it is on:
- A subject is decoded only while a dashboard shows its values (see the `watch` message) or a recording in progress takes it (by its subject-ID, a publisher's node-ID or its type; a recording of everything keeps everything decoded). Heartbeats, port lists, plug-and-play allocation and `uavcan.diagnostic.Record` are decoded whatever happens.
- Every other subject is counted from its CAN frames as they pass, by the subject-ID and node-ID of each transfer's last frame, whether its type is known or not. Once a second each of its publishers gets an event with `"undecoded": true`, an empty `attributes`, and the `rate`, `subject_rate` and `timestamp_unix` (of its last message) counted, so that rates, last-seen times and the graph show as before.
- The 24 h history, "save the last N seconds" and `GET /api/latest/*` hold values only for what was decoded at the time. Start a recording, or leave the switch off, for what must be kept.
- Switching it off decodes every subject again at once.

## Usage

### WebSocket Client (JavaScript/Browser)

At most 64 streams (`/ws`) are open at once; another is refused with `503`.

**Connect:**
```javascript
const ws = new WebSocket('ws://localhost:8080/ws');

ws.onmessage = (event) => {
    const telemetry = JSON.parse(event.data);
    console.log(`Subject ${telemetry.subject_id}: ${telemetry.message_type}`);
    console.log(`Rate: ${telemetry.rate}Hz`);
    console.log(`Attributes:`, telemetry.attributes);
};
```

**Filter Events:**
```javascript
// Receive only Heartbeat messages from nodes 1 and 2
ws.send(JSON.stringify({
    type: 'filter',
    node_ids: [1, 2],
    message_types: ['Heartbeat_1_0']
}));

// Or filter by subjects only
ws.send(JSON.stringify({
    type: 'filter',
    subject_ids: [7509, 7510]
}));

// Ping to keep connection alive
ws.send(JSON.stringify({ type: 'ping' }));

// Subscribe to the raw frame-capture stream (Debugging view). Off by default.
// The first connection to enable it starts capture. Send enabled:false to stop
// receiving frames on this connection; the last one to stop ends capture.
ws.send(JSON.stringify({ type: 'capture', enabled: true }));
```

> **Frame capture only listens.** Its frames come from a listen-only tap: on
> SocketCAN a socket of its own, for any other adapter a listener on the CAN
> hub's forwarding. It changes nothing Cynitor sends or receives, and runs only
> while at least one connection captures. Every frame on the bus comes through,
> error frames included, which the Cyphal stack never sees.

**Server Messages:**

Telemetry events (filtered per client):
```json
{
    "subject_id": 7509,
    "timestamp": "2026-03-13T10:30:45+01:00",
    "timestamp_unix": 1741949445.123,
    "rate": 1.0,
    "subject_rate": 5.0,
    "message_type": "Heartbeat_1_0",
    "publisher_node_id": 42,
    "payload_bytes": 7,
    "attributes": [
        {"attribute": "uptime", "value": 12345, "unit": "s"},
        {"attribute": "health", "value": "NOMINAL"},
        {"attribute": "mode", "value": "OPERATIONAL"},
        {"attribute": "vssc", "value": 42}
    ]
}
```

`rate` is this publisher's message rate on the subject, in Hz (one decimal, over the last 10 s). `subject_rate` is the subject's total over its publishers still sending: with five nodes publishing Heartbeat at 1 Hz, each event carries `rate` 1.0 and `subject_rate` 5.0. A publisher whose last message is older than three of its periods (two seconds at least) no longer counts in it, the rule by which the dashboard calls a subject silent. Both are computed when a message arrives, so a subject that stops altogether sends no new value: the dashboard tells that from the time of its last message. Events recorded before `subject_rate` existed lack it, and there `rate` was the subject total.

A service-ID mask, as `uavcan.node.port.List`'s `servers` and `clients` carry (512 bits), is the list of the service-IDs it sets: `{"attribute": "servers", "value": [430, 435]}`; a subject-ID list sent as a mask (8192 bits) likewise, as `publishers.mask`.

A float that is NaN or infinite (sensors often send NaN for "not available") is `null` in `attributes`, in an array element by element: JSON has no NaN, and a browser cannot read a message that holds one. The same `null` reaches the REST API, history, recordings and their exports; replaying a recording made before this sends `null` too.

`timestamp`, and a node's `last_seen` in `/api/nodes`, are the server's local time with its UTC offset, so a viewer in another timezone reads the same instant. `timestamp_unix` is when the transfer was received as stamped by the transport (for SocketCAN, the kernel's receive timestamp), not when the backend got round to processing it.

`payload_bytes` is the size, in bytes, of the received transfer's serialized payload (sum of `transfer.fragmented_payload` fragment lengths). It is `null` if the transport did not expose the fragmented payload (best-effort field).

`undecoded` is present, and `true`, only in the events the server sends once a second for a subject it does not decode while it [decodes on demand](#5-decode-on-demand): `attributes` is then empty, `payload_bytes` `null`, `message_type` `null` if the subject's type is not known, and `rate`, `subject_rate` and `timestamp_unix` are counted from the subject's CAN frames. Such events are not written to history or recordings.

Metrics (sent to all clients every 1 second):
```json
{
    "type": "metrics",
    "bus_utilization": 3.0,
    "server_cpu": 12.5
}
```

`server_cpu` is the server process's CPU use over the last second, in percent of one core (`null` in its first second). The server takes the bus's traffic on one core, so near 100 it cannot keep up; its other threads count too, so it can pass 100. The dashboard shows it under the server's data rate.

**Say which subjects are on screen:**
```javascript
// The subjects whose values this client shows now; send again whenever they change
ws.send(JSON.stringify({ type: 'watch', subject_ids: [1620, 2000] }));
```

While the server [decodes on demand](#5-decode-on-demand), it decodes the subjects some client watches (and those a recording takes) and no others. A client's list lasts until it sends another or its socket closes; `subject_ids` is a list of subject-IDs (0–8191), otherwise the reply is `{"error": ...}`. There is no acknowledgement. The dashboard sends the subjects of the selected node, of its plots and Compare graphs, and of its log panel. While every subject is decoded the list changes nothing.

Filter acknowledgement (sent in response to a client `filter` message):
```json
{
    "type": "filter_updated",
    "subject_ids": [7509, 7510],
    "node_ids": [1, 2],
    "message_types": ["Heartbeat_1_0"]
}
```

Each field echoes the active filter for the client. Empty / omitted dimensions mean "match anything in that dimension".

Pong (sent in response to a client `ping` message):
```json
{ "type": "pong" }
```

Capture status (sent in response to a client `capture` message):
```json
{ "type": "capture_status", "active": true, "capturing": true, "forwarding": true, "stats": { "captured": 0, "rx": 0, "tx": 0, "cyphal": 0, "foreign": 0, "errors": 0, "dropped": 0 }, "frames": [] }
```
`capturing` says whether capture runs, for any connection. `forwarding` says
whether this connection is sent the frames. A disable reply carries
`"forwarding": false`, and `capturing` stays `true` only while another
connection captures. When enabling fails, both are `false` and an `"error"`
field says why: no CAN session, or the tap could not open. `active` is kept for
dashboards from before these two: `capturing` in an enable reply, `false` in
the others.

An enable reply's `frames` holds up to the 500 most recent frames captured
before this connection subscribed, while another one captured (oldest first,
shaped as in `can_frame`). It is taken in the same step as subscribing: the
`can_frame` stream that follows carries the frames after them, none repeated
and none skipped. It is empty when the connection starts capture, or was
subscribed already.

Raw frame batch (sent only to clients that opted into capture; batched ~every
120 ms to bound message rate, each with every frame queued since the last, up
to 2000):
```json
{
    "type": "can_frame",
    "stats": { "captured": 1024, "rx": 1000, "tx": 24, "cyphal": 1010, "foreign": 13, "errors": 1, "dropped": 0 },
    "frames": [
        {
            "t": 1741949445.123451, "ts": 1741949445.123451, "dir": "rx",
            "id": "0x107D552A", "ext": true, "dlc": 8, "data": "01 02 03 04 05 06 07 E5",
            "cyphal": true, "priority": "NOMINAL", "src": 42, "dst": null,
            "kind": "msg", "port": 7509, "transfer_id": 5, "start": true, "end": true, "toggle": true
        },
        { "t": 1741949445.124002, "ts": 1741949445.124002, "dir": "rx", "id": "0x00000123", "ext": false, "dlc": 2, "data": "AA BB", "cyphal": false },
        { "t": 1741949445.125130, "ts": 1741949445.125130, "dir": "rx", "id": "0x000000A8", "ext": false, "dlc": 8,
          "data": "00 00 04 00 00 00 00 00", "cyphal": false, "error": ["protocol violation: stuff", "no ACK", "bus error"] }
    ]
}
```
`t` and `ts` are the time the frame was received, in Unix seconds (on SocketCAN
the kernel's timestamp; from an adapter whose clock counts from its power-up,
the time the frame reached Cynitor). `dir` is `tx` for a frame Cynitor sent: one
sent from this computer that, if it is a Cyphal frame, comes from Cynitor's
node-ID (on SocketCAN the tap also hears the computer's other
programs, such as every node on a vcan, which stay `rx`). `dlc` is the data
length in bytes (0–64), not the DLC code. An error frame has `"cyphal": false`
and `error`: what it reports, in words, on SocketCAN, candleLight (`gs_usb`)
adapters and raw logs (its `id` holds the error classes and `data` the
details, as `linux/can/error.h` lays them out), else `["error frame"]`. A
remote frame has `"rtr": true`, an empty `data` and the length it asks for as
`dlc`. For Cyphal frames,
`kind` is `msg`/`req`/`resp` and `port` is the subject- or service-ID, and
`start`, `end` and `toggle` are the tail byte's start-of-transfer,
end-of-transfer and toggle bits; non-Cyphal ("foreign") frames carry only the
raw fields with `cyphal: false`.

Protocol errors (sent when the client sends a frame the server cannot parse):
```json
{ "error": "Invalid JSON" }
```

These do not include a `type` field; the bare `error` key signals a protocol-level problem rather than a domain event. Subsequent frames are still accepted on the same connection.

### REST API

Every endpoint answers an error with a `4xx` or `5xx` status and `{"error": "<message>"}`, sometimes with more fields that say more (such as `available_interfaces`).

**Get server status (connection state, available interfaces, bus load):**
```bash
curl http://localhost:8080/api/status
```

Response:
```json
{
    "status": "running",
    "can_interface": "vcan0",
    "can_bitrate": null,
    "can_data_bitrate": null,
    "can_fd": false,
    "available_interfaces": ["vcan0", "can0"],
    "available_adapters": [
        {"interface": "vcan0", "label": "vcan0 (SocketCAN)", "needs_bitrate": false, "supports_fd": false,
         "data_bitrates": null},
        {"interface": "pcan:PCAN_USBBUS1", "label": "PEAK PCAN_USBBUS1", "needs_bitrate": true, "supports_fd": true,
         "data_bitrates": null},
        {"interface": "slcan:COM5", "label": "CANable (slcan) COM5", "needs_bitrate": true, "supports_fd": true,
         "data_bitrates": [2000000, 5000000]}
    ],
    "bus_utilization": 3.0,
    "dropped": {"scanner": 0, "logger": 0, "clients": 12},
    "last_error": null,
    "cyphal_v11": null,
    "bus_errors": null,
    "allocator": null,
    "server_cpu": 12.5,
    "decode_on_demand": false
}
```

`decode_on_demand` is whether the server decodes only the subjects watched or recorded (see [Decode on Demand](#5-decode-on-demand)); `POST /api/decoding` switches it.

`server_cpu` is as in the `metrics` message: the server's own CPU use, in percent of one core.

`allocator` is `null` unless the CAN session runs Cynitor's node-ID allocator (see [Allocator Behavior](#4-allocator-behavior)). Then it is `{"other_allocator", "allocated"}`: `other_allocator` is `null` while Cynitor's allocator answers requests, and the node-ID of another allocator once one has answered a request too, from when Cynitor's stays silent; `allocated` is how many nodes it gave a node-ID in this session. The dashboard shows a silenced allocator under the CAN status.

`bus_errors` is `null` while the bus has no errors, and when not connected to CAN. While it has some, it is `{"state", "tx_errors", "rx_errors", "since_unix"}`: the CAN controller's state (`"ERROR-WARNING"`, `"ERROR-PASSIVE"`) and its transmit and receive error counters, all three `null` where the adapter does not report them (only SocketCAN does), and when the errors began. The bus has errors while its controller is in one of those states, or while an error counter grew in the last 10 s: SocketCAN's bus-error and state-change counters, or, for an adapter behind the hub, the error frames it reports and the sends it refused. The session's health check samples them every 3 s. Most often the bus is not terminated (120 Ω at each end) or a node runs another bitrate; the dashboard shows the errors under the CAN status, and the server logs a line when they start and one when they stop, in place of pycyphal's line per error frame. A controller in ERROR-WARNING or ERROR-PASSIVE still sends and receives, so the session goes on; BUS-OFF ends it, `last_error` saying so.

`cyphal_v11` is `null` until Cyphal v1.1 traffic is seen on the bus, which Cynitor, a Cyphal v1.0 monitor, does not decode. Then it is `{"transfers", "nodes": [node-IDs], "subject_count", "subject_ids": [the first 16, sorted], "last_seen_unix"}`, counted over the CAN session; the dashboard shows it under the CAN status. A v1.1 transfer is recognised by its first frame: an extended CAN ID with bit 25 = 0, bit 24 = 0 and bit 7 = 1 (a 16-bit subject-ID; v1.0 keeps bit 7 at 0), and a tail byte with start-of-transfer and toggle set (which tells it from a DroneCAN service frame), every frame but a transfer's last being full. On SocketCAN a listen-only socket watches for it, filtered by the kernel to frames with those ID bits; behind the hub, the hub does. A v1.1 topic pinned to a v1.0 subject-ID (`name#1234`) travels as v1.0 and is decoded as usual.

`history_error` says why history and recordings are not being written, such as `"disk I/O error"` on a full disk, until a write works again; otherwise `null`. What such a write loses counts in `dropped.logger`.

`own_node_id` is Cynitor's own node-ID on the bus, `null` when not connected or listening only (`listen_only`). `no_commands` is whether the session that runs refuses register writes, service calls, firmware updates and test nodes on a real bus (see `POST /api/can/connect`).

`update` is a newer Cynitor release, `{"version": "1.4.0", "url": "https://github.com/OpenCyphal-Garage/cynitor/releases/tag/v1.4.0"}`, when the server's update check (GitHub, at start and once a day) found one; otherwise `null`, also when the check is off (`--no-update-check`, `CYNITOR_NO_UPDATE_CHECK=1`) or GitHub cannot be reached.

`bus_utilization` is the bus load in percent, here and in the `metrics` message and `/api/can/transport`. It is `null` when not connected to CAN, and when nothing can measure it: on SocketCAN it comes from `canbusload` (Linux can-utils), so a host without it reports `null`, not an idle 0. So does a CAN FD bus when the host's `canbusload` counts Classic frames only (can-utils before mid-2021, such as Ubuntu 22.04's 2020.11), as it would read CAN FD traffic as an idle bus.

`dropped` counts what was discarded since the CAN connection opened because a queue was full: `scanner` before reaching anything, `logger` messages missing from the 24 h history and from recordings (the queue full, or a write to the database failed), `clients` messages missing from some dashboard's live view (each open dashboard has its own 100-event queue). `scanner` counts decoded messages, and CAN frames let go before decoding: when a subject's messages come faster than they can be decoded, its frames wait, and once a message is more than a second old by the time it is decoded, all that waits behind it is let go at once and counted here, rather than put the live view ever further behind. A burst that is decoded within the second loses nothing. A raw log being played is decoded whole. It also counts, as one each, the overflows an adapter reports, frames it lost itself before Cynitor saw them (at least one each; an adapter does not say how many): behind the hub its overflow notices, on SocketCAN the growth of the interface's `rx_over_errors`. On SocketCAN (Linux) it also counts the frames the kernel let go because the scanner's socket's receive queue was full, which is where most are lost past the decode ceiling; the server then logs a warning, at most once a minute, suggesting decode on demand. While it loses frames there, and for 3 s after, no node is marked offline for silence: its heartbeats may be among the frames lost, and a node marked offline would have its subjects unsubscribed and their frames discarded uncounted. Behind the hub nothing inside Cynitor is lost uncounted; an adapter's own overflow is not seen. An adapter's driver can still lose frames uncounted in its own queue, when the hub reads slower than the bus delivers: frames that waited there are counted in `adapter_frames_read_late` of `/api/can/transport`, not here, warned of at most once a minute, and keep nodes from being marked offline in the same way. `null` when not connected to CAN. The dashboard shows the total next to the CAN message rate.

`status` is `"running"` when connected to CAN, `"idle"` otherwise. `last_error` contains the error message if CAN was auto-disconnected due to a bus fault. `can_bitrate` is the bitrate Cynitor opened the adapter at, or `null` for SocketCAN, whose bitrate the kernel sets. `can_data_bitrate` is the CAN FD data-phase bitrate it opened the adapter with, or `null` (Classic CAN, or SocketCAN). `can_fd` says whether the session runs Cyphal/CAN FD; for SocketCAN that follows the interface's own setup when it connects. A SocketCAN interface switched between CAN FD and Classic CAN while connected ends the session within a few seconds, `last_error` saying so ("Interface can0 was switched to Classic CAN; connect again to use it"); connecting again takes the new setup.

`available_interfaces` lists SocketCAN names only, as before. `available_adapters` lists everything the dashboard can offer: SocketCAN interfaces, adapters of vendor drivers python-can can enumerate (PEAK, Kvaser, Vector, IXXAT) and, off Linux, candleLight (`gs_usb`) and known slcan adapters; and on every OS the simulated buses, `sim:classic` and `sim:fd`. Pass an entry's `interface` to `POST /api/can/connect`, with a `bitrate` when `needs_bitrate` is true. `supports_fd` says whether a session on it can run CAN FD: for an adapter Cynitor opens itself (`needs_bitrate` true), that it can be opened as CAN FD when given a `data_bitrate` (for a candleLight adapter, that its firmware does CAN FD, as the adapter tells); for SocketCAN, that the interface is set up for CAN FD, which Cynitor then uses without being asked. `data_bitrates` lists the only data bitrates an adapter with presets for them can run (the CANable 2.0's slcan firmware: 2 and 5 Mbit/s), and is `null` for any other. The list is rescanned at most every 10 seconds, and not at all while connected.

**List CAN adapters:**
```bash
curl "http://localhost:8080/api/can/adapters?refresh=1"
```

Returns `{"adapters": [...]}`, entries as in `available_adapters`. `refresh=1` rescans now instead of serving a list up to 10 seconds old (ignored while connected).

**Connect to a CAN interface:**
```bash
curl -X POST http://localhost:8080/api/can/connect \
  -H 'Content-Type: application/json' \
  -d '{"interface":"vcan0"}'
```

Response (success):
```json
{"status": "running", "can_interface": "vcan0", "can_fd": false, "allocator": null, "listen_only": false, "no_commands": false}
```

`allocator` is as in `GET /api/status`: `null` unless the session runs Cynitor's node-ID allocator.

`interface` is either a SocketCAN name, which must be one of `available_interfaces`, a python-can spec such as `gs_usb:0` or `pcan:PCAN_USBBUS1`, which is opened without that check (its interface one of `socketcan`, `pcan`, `kvaser`, `vector`, `ixxat`, `gs_usb`, `slcan`, `virtual`, and an `slcan` channel a serial port name such as `COM5`, `/dev/ttyACM0` or `/dev/serial/by-id/...`; any other is refused with `400` unless the server was started with `--allow-any-interface`), or a simulated bus: `sim:classic` (1 Mbit/s) or `sim:fd` (CAN FD, 1 and 4 Mbit/s), a bus inside Cynitor with demo nodes on it. A simulated bus runs at its own rates: a `bitrate` or `data_bitrate` given is checked, then ignored. `bitrate` (integer, 1–1000000 bit/s) is the bus speed, required for every adapter except SocketCAN, which ignores it. It may be left out only if the server was started with `--bitrate`, which then applies.

`data_bitrate` (integer, 1–12000000 bit/s, optional) opens the adapter as CAN FD with that data-phase bitrate; left out, the session is Classic CAN unless the server was started with `--data-bitrate`. Only adapters whose `supports_fd` is true take it (PEAK, Kvaser, Vector, IXXAT, candleLight with CAN FD firmware, slcan), at one of their `data_bitrates` where those are given (slcan: 2000000 or 5000000); SocketCAN ignores it and runs CAN FD when the interface is set up for it. `can_fd` in the response says which it became.

`listen_only` (boolean, optional) has the session send nothing on the bus: no node-ID, heartbeat, GetInfo or register reads, no allocator and no test nodes; subjects decode from their fixed IDs and the types the user set. Left out, it does only if the server was started with `--listen-only`. Meanwhile `GET /api/registers/{id}`, `POST /api/registers/{id}/set`, service calls and firmware updates answer `409` saying the session listens only. `listen_only` in the response, and in `GET /api/status`, says whether the session sends nothing. The CAN controller still acknowledges frames unless the interface itself is in listen-only mode.

`no_commands` (boolean, optional) has the session change no node: it names nodes and reads registers as usual, but `POST /api/registers/{id}/set`, service calls, firmware updates and starting a test node on a real bus (not the simulated bus) answer `409` saying the session takes no commands, and no allocator runs. Left out, it does only if the server was started with `--no-commands`. `no_commands` in the response, and in `GET /api/status`, says whether the session runs so. It is enforced by the server, so a script or another dashboard cannot get round it; to change nodes, disconnect and connect without it.

`allocator` (boolean, optional) has Cynitor give plug-and-play nodes their node-IDs in this session (see [Allocator Behavior](#4-allocator-behavior)); left out, it does only if the server was started with `--allocator`. Turn it on only for a bus with no allocator of its own.

```bash
curl -X POST http://localhost:8080/api/can/connect \
  -H 'Content-Type: application/json' \
  -d '{"interface":"can0","allocator":true}'
```

```bash
curl -X POST http://localhost:8080/api/can/connect \
  -H 'Content-Type: application/json' \
  -d '{"interface":"pcan:PCAN_USBBUS1","bitrate":500000,"data_bitrate":2000000}'
```

```bash
curl -X POST http://localhost:8080/api/can/connect \
  -H 'Content-Type: application/json' \
  -d '{"interface":"gs_usb:0","bitrate":250000}'
```

```bash
curl -X POST http://localhost:8080/api/can/connect \
  -H 'Content-Type: application/json' \
  -d '{"interface":"gs_usb:0","bitrate":1000000,"data_bitrate":4000000}'
```

Returns `409` if already connected, `400` if a SocketCAN name is unknown, `bitrate` is missing or invalid, `allocator` is not a boolean, or `data_bitrate` is invalid, given for an adapter that cannot run CAN FD, or not one of its presets. A candleLight adapter whose firmware runs Classic CAN only, or whose CAN clock cannot make the two bitrates, fails to open (`500`), saying so.

**Switch decoding on demand:**
```bash
curl -X POST http://localhost:8080/api/decoding \
  -H 'Content-Type: application/json' \
  -d '{"on_demand":true}'
```

Response: `{"decode_on_demand": true}`. `on_demand` (boolean, required) turns [decoding on demand](#5-decode-on-demand) on or off, at once if CAN is connected, and for the sessions that follow until the server stops. `400` if it is not a boolean.

**Disconnect from CAN:**
```bash
curl -X POST http://localhost:8080/api/can/disconnect
```

Response:
```json
{"status": "idle", "available_interfaces": ["vcan0", "can0"], "available_adapters": [...]}
```

Returns `409` if not connected.

**Transport-layer diagnostics (Debugging view):**
```bash
curl http://localhost:8080/api/can/transport
```

Read-only snapshot of the CAN transport *below* the DSDL/application layer —
used by the Debugging tab. When no CAN session is active it returns
`{"connected": false}` (HTTP 200) so the UI can render an idle state.

Response (connected):
```json
{
  "connected": true,
  "interface": "vcan0",
  "protocol": {"mtu": 7, "transfer_id_modulo": 32, "max_nodes": 128, "is_fd": false},
  "statistics": {
    "in_frames": 1024, "in_frames_cyphal": 1000, "in_frames_cyphal_accepted": 980,
    "in_frames_errored": 0, "in_frames_loopback": 12,
    "out_frames": 40, "out_frames_timeout": 0, "out_frames_loopback": 12,
    "media_acceptance_filtering_efficiency": 0.96, "lost_loopback_frames": 0
  },
  "capture_active": false,
  "link": {
    "operstate": "up", "state": "ERROR-ACTIVE", "bitrate": 500000, "dbitrate": null,
    "berr_tx": 0, "berr_rx": 0, "restart_ms": 0,
    "restarts": 0, "bus_errors": 0, "arbitration_lost": 0,
    "error_warning": 0, "error_passive": 0, "bus_off": 0
  },
  "bus_utilization": 12.0
}
```

`protocol` / `statistics` come from pycyphal's transport (`mtu` is the
single-frame payload limit: 7 = Classic CAN, up to 63 = CAN FD).
`capture_active` says whether frame capture runs, for any connection. `link` is
best-effort controller state parsed from `ip -details -statistics link show`;
fields are `null` on virtual interfaces (vcan) or where the controller does not
report them. For an adapter Cynitor opens itself, `link` holds what the CAN hub
counts instead: `bitrate`, `dbitrate` (the CAN FD data bitrate, or `null`),
`adapter_frames_in`, `adapter_frames_out`, `adapter_send_failures`,
`adapter_error_frames` (error frames, if the adapter's driver reports them),
`adapter_rx_overflows` (the adapter's notices that it lost received frames, its
buffer full, which are no bus error and count in `dropped.scanner` instead) and
`adapter_frames_read_late` (frames that waited over 0.5 s in the driver's queue
before the hub read them: the bus carries more than Cynitor reads, and a full
queue loses frames that nothing counts; only drivers that stamp frames as they
receive them can tell, such as PCAN, Kvaser, Vector and IXXAT).
The Debugging view polls this endpoint at ~1 Hz while active.

**Raw frame-capture snapshot:**
```bash
curl 'http://localhost:8080/api/can/capture?limit=500'
```

Returns the recent-frame ring buffer plus capture counters, for scripts: the
frames of the capture running, or of the last one. The dashboard does not use
it: the reply to its `capture` message carries the same frames without
overlapping the stream (see above). Live frames stream over the WebSocket
`can_frame` message; this endpoint does not start capture.

```json
{
  "active": true,
  "stats": {"captured": 1024, "rx": 1000, "tx": 24, "cyphal": 1010, "foreign": 13, "errors": 1, "dropped": 0},
  "frames": [ /* same per-frame shape as the can_frame stream, oldest→newest */ ]
}
```

When no CAN session exists: `{"active": false, "stats": null, "frames": []}`.
`limit` is clamped to 2000 (default 500).

**DSDL status (does not require CAN connection):**
```bash
curl http://localhost:8080/api/dsdl/status
```

Response:
```json
{
    "paths": [{"path": "...", "label": "Public regulated types", "source": "regulated"}],
    "public_compilable": true,
    "compiled": true,
    "last_compiled": 1773832423.0,
    "last_public_compiled": 1773832423.0,
    "last_custom_compiled": null,
    "source_types": 257,
    "custom_types": 0
}
```

`last_public_compiled` covers the public `uavcan/` and `reg/` namespaces, compiled into `python_compiled_messages/`; `last_custom_compiled` covers the custom types, compiled into the data folder (`dsdl/compiled`). `last_compiled` is the max of both, kept for backward compatibility.

The status is kept in memory, so polling it is cheap. It is read from disk again at once after a save, delete or compile made through this API, and otherwise at most every 30 s, for changes made elsewhere (such as the public types compiled when CAN connects, if they were not yet).

**DSDL namespace tree:**
```bash
curl http://localhost:8080/api/dsdl/namespaces
```

Returns a nested tree of namespaces with type entries. Each type includes `short_name`, `full_name`, `version`, `kind` (`"message"` or `"service"`), `fixed_port_id`, `source` (`"regulated"` or `"custom"`), `field_names` and `constant_names` (for search), and `compiled` (`true` once its Python code exists: in `python_compiled_messages/` for a public type, in the data folder's `dsdl/compiled` for a custom one). Custom namespaces, empty ones included, carry `"_source": "custom"`.

**DSDL type detail:**
```bash
curl http://localhost:8080/api/dsdl/type/uavcan.node.Heartbeat.1.0
```

Returns full type info: fields (with types), constants, dependencies, compilation status, and raw `.dsdl` source text. For services, fields are split into `request` and `response`.

What the compiler (pydsdl) reads in the type comes with it: `doc` (the type's comment), `deprecated`, a `doc` on each field and constant (its comment, a unit such as `[second]` first when it has one), and `layout`:

```json
"layout": {"union": false, "sealed": false, "extent_bytes": 12, "size_bytes": [7, 7]}
```

`size_bytes` is the serialized size, smallest and largest; `extent_bytes` how far a type that is not sealed may grow in later versions (for a sealed type, its largest size). A service has one layout each way: `{"request": {...}, "response": {...}}`. When pydsdl cannot read the type, `doc` is empty, `deprecated` false and `layout` null.

**Value schema of a message type** (what the Test nodes editor builds its form from):
```bash
curl http://localhost:8080/api/dsdl/value-schema/uavcan.si.unit.voltage.Scalar.1.0
```

```json
{"kind": "struct", "type": "uavcan.si.unit.voltage.Scalar.1.0", "constants": [],
 "fields": [{"name": "volt", "schema": {"kind": "float", "type": "saturated float32",
                                        "min": -3.4028234663852886e+38, "max": 3.4028234663852886e+38}}]}
```

`kind` is `struct` (`fields`), `union` (`variants`, the first the default), `array` (`capacity`, `fixed`, `element`, and `text: true` for bytes, which take a string), `int` or `float` (`min`, `max`: the field's range) or `bool`; a composite has its `constants`. Read from the type's source; `400` for a service type, a type that is not compiled, or none of that name.

`problem` is null for a type that compiles. For one that does not, it says why, as a compile would: `{"message": "Syntax error", "line": 2}`, where `line` is the line of the type's own source, or null when the fault is with the file as a whole (a missing `@sealed`) or in a type it uses, which the message then names.

**Create custom namespace:**
```bash
curl -X POST http://localhost:8080/api/dsdl/custom/namespace \
  -H 'Content-Type: application/json' \
  -d '{"namespace": "myapp.sensors"}'
```
Returns `201` with `{"namespace": "myapp.sensors", "path": "..."}`. A namespace under `uavcan` or `reg`, the public regulated types' own, returns `400`; so does saving a type into one.

**List custom namespaces:**
```bash
curl http://localhost:8080/api/dsdl/custom/namespaces
```
Returns `{"namespaces": ["myapp", "myapp.sensors"]}`.

**Save custom DSDL type:**
```bash
curl -X POST http://localhost:8080/api/dsdl/custom/type \
  -H 'Content-Type: application/json' \
  -d '{"namespace": "myapp.sensors", "type_name": "Temperature", "version": "1.0", "source_text": "float32 celsius\nfloat32 fahrenheit\n@sealed", "fixed_port_id": null}'
```
Returns `201` with `{"full_name": "myapp.sensors.Temperature.1.0", "path": "..."}`.

Pass `"overwrite": true` to replace an existing custom type's source. Only allowed while the type is **not compiled** — the server returns `409` if the type has already been compiled.

`fixed_port_id` is optional. A custom type may only take the fixed port-IDs the compiler accepts outside the `uavcan` namespace: 6144–7167 for a message, 256–383 for a service (a source with a `---` line). Any other value, or one that is not an integer, returns `400`.

**Delete custom DSDL type:**
```bash
curl -X DELETE http://localhost:8080/api/dsdl/custom/type/myapp.sensors.Temperature.1.0
```
Returns `200` with `{"full_name": "myapp.sensors.Temperature.1.0", "deleted": true}`.
Returns `404` if the source file is missing, or `400` on a malformed name. Deleting also removes the type's compiled code.

**Delete an empty custom namespace:**
```bash
curl -X DELETE http://localhost:8080/api/dsdl/custom/namespace/myapp.sensors
```
Returns `200` with `{"namespace": "myapp.sensors", "deleted": true}`; its empty sub-namespaces go with it.
Returns `400` while it still has types (delete them first) or on a malformed name, and `404` if there is no such namespace.

**Compile DSDL types:**
```bash
# Compile custom namespaces only
curl -X POST http://localhost:8080/api/dsdl/compile \
  -H 'Content-Type: application/json' \
  -d '{"scope": "custom"}'

# Recompile public (regulated) types only — reg + uavcan
curl -X POST http://localhost:8080/api/dsdl/compile \
  -H 'Content-Type: application/json' \
  -d '{"scope": "public"}'

# Recompile everything (regulated + custom)
curl -X POST http://localhost:8080/api/dsdl/compile \
  -H 'Content-Type: application/json' \
  -d '{"scope": "all"}'
```
Returns `200` with `{"ok": true}` on success, or `422` with `{"error": "..."}`, each failing namespace's error on a line of its own. A fault the compiler finds in a type reads `<full name>, line <n>: <message>` (`, line <n>` left out when it is about the whole file), for example `myapp.sensors.Reading.1.0, line 2: Syntax error`; any other failure reads `<namespace label>: <message>`. Compiles run one at a time: a request made while one runs waits for it to end.

Compilation runs inside the server. In the packaged binaries the public types are built in: `"public"` is refused and `"all"` compiles the custom types; `GET /api/dsdl/status` reports this as `"public_compilable": false`. Custom types and their compiled code are kept in the data folder (`dsdl/custom`, `dsdl/compiled`).

**Get latest event for a subject:**
```bash
curl http://localhost:8080/api/latest/subject/7509
```

Response:
```json
{
    "subject_id": 7509,
    "timestamp": "2026-03-13T10:30:45",
    ...
}
```

**Get all latest events from a node:**
```bash
curl http://localhost:8080/api/latest/node/42
```

Response:
```json
{
    "node_id": 42,
    "events": {
        "7509": {...},
        "7510": {...}
    }
}
```

If the node is discovered but no live telemetry has been cached yet, the endpoint returns `404`:

```json
{
    "error": "No telemetry cached for node 42",
    "known_node": true
}
```

**Get information about all discovered nodes on CAN network:**
```bash
curl http://localhost:8080/api/nodes
```

Response:
```json
{
    "node_count": 2,
    "nodes": {
        "37": {
            "node_id": 37,
            "unique_id": [215, 79, 139, 105, 174, 196, 24, 37, 77, 69, 95, 193, 150, 10, 89, 50],
            "unique_id_hex": "d74f8b69aec418254d455fc1960a5932",
            "uptime": 8285,
            "has_disappeared": false,
            "has_responded_to_getinfo": true,
            "name": "org.dontpanic.pycyphal.examples.publisher_si",
            "software_version": {
                "major": 1,
                "minor": 0
            },
            "publishers": [1235, 1236, 7509, 7510],
            "subscribers": [7509],
            "clients": [],
            "servers": [384, 385, 430],
            "last_seen": ["2026-05-04T12:30:45.123456"]
        },
        "74": {
            ...
        },
        "uid:ab12cd34ef56789000000000deadbeef": {
            "node_id": null,
            "last_node_id": 37,
            "unique_id": [171, 18, 205, 52, 239, 86, 120, 144, 0, 0, 0, 0, 222, 173, 190, 239],
            "unique_id_hex": "ab12cd34ef56789000000000deadbeef",
            "uptime": 1200,
            "has_disappeared": true,
            "has_responded_to_getinfo": true,
            "name": "org.example.displaced_node",
            "software_version": {"major": 1, "minor": 0},
            "publishers": [1235],
            "subscribers": [],
            "clients": [],
            "servers": [384, 430],
            "last_seen": ["2026-05-04T11:00:00.000000"],
            "_ghost": true
        }
    },
    "subject_types": {
        "1235": {"type": "uavcan.si.unit.voltage.Scalar_1_0", "set_by": "registers"},
        "1700": {"type": "uavcan.si.unit.velocity.Vector3.1.0", "set_by": "user"}
    }
}
```

A node that is one of the test nodes Cynitor runs (see "Test nodes" below) carries `"test_node": "<test id>"`: matched by its unique-ID, so one stopped keeps it, or, while one runs, by its node-ID.

`subject_types` holds every decoded subject's type and where it came from: a publisher's registers, or the user (see "Subject types" below). A subject a node publishes that is missing from it cannot be decoded until its type is set. It is absent during a replay.

Ghost entries (key `uid:<hex>`) represent devices whose `unique_id` is known but whose node_id slot was taken by another device. `last_node_id` is the most recent node_id the device held. Ghost data comes from the last snapshot before displacement.

**Get identity map (unique_id to node_id mappings):**
```bash
curl http://localhost:8080/api/identity-map
```

Response:
```json
{
    "mappings": {
        "d74f8b69aec418254d455fc1960a5932": {
            "current_node_id": 37,
            "previous_node_ids": [42, 99],
            "last_seen": 1741949445.123,
            "name": "org.example.my_node"
        },
        "ab12cd34ef56789000000000deadbeef": {
            "current_node_id": null,
            "previous_node_ids": [5, 10],
            "last_seen": 1741940000.0,
            "name": null
        }
    }
}
```

Each key is a hardware `unique_id` hex string. `current_node_id` is `null` if the device is offline and its slot was taken by another device. `previous_node_ids` lists all past node_id assignments. `last_seen` is a Unix timestamp of the last identity registration. `name` is the node name from GetInfo (if available). The identity map is persisted to SQLite and restored across server restarts.

**Delete a ghost identity (remove detached node data):**
```bash
curl -X DELETE http://localhost:8080/api/identity/d74f8b69aec418254d455fc1960a5932
```

Response:
```json
{"status": "ok"}
```

Removes the identity from the in-memory map and deletes its persisted snapshot from SQLite. Returns `503` if CAN is not connected.

**Health check:**
```bash
curl http://localhost:8080/api/health
```

Response:
```json
{
    "status": "healthy",
    "can_error": null,
    "can_status": "running",
    "can_interface": "vcan0",
    "connected_clients": 3,
    "server": {
        "host": "127.0.0.1",
        "port": 8080
    }
}
```

A server started with `--can` answers `503`, `"status": "degraded"` and `can_error` saying why, while that bus is down, until it is attached again (`--reconnect` does that by itself) or the operator disconnects it with `POST /api/can/disconnect`.

**Get recent application logs (all levels):**
```bash
curl "http://localhost:8080/api/logs?limit=200"
```

**Filter logs by exact level:**
```bash
curl "http://localhost:8080/api/logs?level=ERROR&limit=100"
```

**Filter logs by minimum level:**
```bash
curl "http://localhost:8080/api/logs?min_level=WARNING&limit=100"
```

**Only the records since one already seen** (what a poller asks; `since` is a record's `timestamp`, and that record comes back first):
```bash
curl -G "http://localhost:8080/api/logs" --data-urlencode "since=2026-03-14T15:52:01.123456+00:00"
```

`limit` (default 200, at most 5000) keeps the newest records of those that match. A `since`, `level` or `min_level` that cannot be read answers `400`.

Response:
```json
{
    "count": 2,
    "logs": [
        {
            "timestamp": "2026-03-14T15:52:01.123456+00:00",
            "logger": "websocket_server",
            "level": "ERROR",
            "message": "Error in GET /api/latest/node: ..."
        }
    ]
}
```

The server keeps its newest 5000 log records, oldest first in the response. Its HTTP access lines (one per request) are not among them: a dashboard polls several times a second, and they would push every other record out. They still go to the console.

**Get service schema for a node (types, request fields):**
```bash
curl http://localhost:8080/api/services/37
```

Response:
```json
{
    "node_id": 37,
    "services": [
        {
            "service_id": 430,
            "name": "GetInfo_1_0",
            "namespace": "uavcan.node",
            "full_type": "uavcan.node.GetInfo_1_0",
            "callable": true,
            "request_fields": []
        },
        {
            "service_id": 384,
            "name": "Access_1_0",
            "namespace": "uavcan.register",
            "full_type": "uavcan.register.Access_1_0",
            "callable": true,
            "request_fields": [
                {"name": "name", "type": "uavcan.register.Name_1_0", "kind": "composite", "fields": [...]},
                {"name": "value", "type": "uavcan.register.Value_1_0", "kind": "composite", "fields": [...]}
            ]
        }
    ]
}
```

Returns `404` if the node is not found, `503` if CAN is not connected.

**Invoke a service on a node:**
```bash
curl -X POST http://localhost:8080/api/services/37/430/call \
  -H 'Content-Type: application/json' \
  -d '{"attributes": {}}'
```

Response (success):
```json
{
    "status": "ok",
    "latency_ms": 23,
    "response": "{\n  \"protocol_version\": {\n    \"major\": 1,\n    \"minor\": 0\n  },\n  ...\n}"
}
```

`response` is the node's answer as JSON text (pycyphal's `to_builtin` form: numbers, strings, and objects for composite fields), so it reads well and a script can `json.loads` it.

Request attributes are keyed by field name: `{"value": <v>}` for a plain field, and for a composite field `{"type": "<its type>", "value": {"<sub-field>": <v>, ...}}` with each sub-field to set; a single `value` there sets the composite's first field, as single-field wrappers such as `uavcan.primitive.String.1.0` need.

`uavcan.node.ExecuteCommand` (service 435) is callable on every node that advertises it, registers or not. Restarting a node, for example (65535 is `COMMAND_RESTART`; the dashboard offers Restart and Factory reset as buttons):

```bash
curl -X POST http://localhost:8080/api/services/37/435/call \
  -H 'Content-Type: application/json' \
  -d '{"attributes": {"command": {"value": 65535}}}'
```

Response (timeout, HTTP `504`):
```json
{
    "status": "timeout",
    "latency_ms": 5000,
    "error": "Service 430 on node 37 timed out"
}
```

Response (service not found, HTTP `404`):
```json
{
    "status": "error",
    "error": "Service 430 not found on node 37"
}
```

Response (generic backend exception, HTTP `500`):
```json
{
    "status": "error",
    "latency_ms": 42,
    "error": "<exception message>"
}
```

Returns `400` for invalid request body or attribute validation errors, `404` if the service is not found on the node, `500` if the backend hit an unexpected error invoking the service, `503` if CAN is not connected, `504` for timeouts. The body always carries a `status` field whose values are one of `"ok"`, `"timeout"`, or `"error"`, so a client can switch on the body shape regardless of HTTP status. `latency_ms` is omitted only in the `404` case (no call was attempted).

**Get client ports for a node (with type names and server cross-references):**
```bash
curl http://localhost:8080/api/clients/37
```

Response:
```json
{
    "node_id": 37,
    "clients": [
        {
            "service_id": 430,
            "full_type": "uavcan.node.GetInfo_1_0",
            "server_nodes": [74, 99]
        }
    ]
}
```

Returns `404` if the node is not found, `503` if CAN is not connected.

**Get all registers for a node:**
```bash
curl http://localhost:8080/api/registers/37
```

Response:
```json
{
    "node_id": 37,
    "registers": [
        {
            "register_name": "uavcan.node.id",
            "value": "37",
            "type": "natural16",
            "access": "read-write"
        },
        {
            "register_name": "uavcan.node.description",
            "value": "My node",
            "type": "string",
            "access": "read-only"
        }
    ]
}
```

Returns `503` if CAN is not connected, and `504` with `{"error": "Node 37 does
not answer register requests"}` if the node answers none of the first two list
indexes (asked twice each, a few seconds). The list ends at the first two indexes
in a row that go unanswered.

**Get node lifecycle history:**
```bash
curl "http://localhost:8080/api/nodes/37/history?range=1h&limit=500"
```

Query params:
- `range` — time window: `5m`, `15m`, `1h`, `6h`, `24h`, `7d` (default `1h`)
- `types` — comma-separated event types to filter, e.g. `service_call,health_change`
- `unique_id` — filter by hardware unique ID hex string (preferred over node_id for stable identity)
- `limit` — max entries, 1–2000 (default `500`)

Response:
```json
{
    "node_id": 37,
    "events": [
        {
            "id": 12,
            "node_id": 37,
            "timestamp_unix": 1741949445.123,
            "event_type": "health_change",
            "detail": {"old": "NOMINAL", "new": "CAUTION"}
        }
    ]
}
```

Event types: `first_seen`, `reappeared`, `disappeared`, `restart_suspected` (the uptime dropped, then counted on), `health_change`, `mode_change`, `port_change`, `service_call`, `register_write` (Cynitor wrote a register: `name`, `value` as sent, `status` `ok` with `read_back`, `not_applied` or `timeout`), `firmware_update` (Cynitor told the node to update from `file`), `got_node_id`, `lost_node_id`, `node_id_migration`, `node_id_conflict` (heartbeats on this node-ID alternate between two uptimes: two nodes share it; or, with `"own_node_id": true` in its detail, another node uses Cynitor's own node-ID, which `/api/status`'s `last_error` then says too; reported at most once a minute) and `type_conflict` (the node publishes a subject with another type than the one it is decoded as, which an earlier publisher advertised). A `service_call`, `register_write` or `firmware_update` carries `client`, the address of the dashboard or script that asked for it.

Returns `400` for invalid node_id or limit, `503` if the event logger is not available.

**Get recent lifecycle events of every node:**
```bash
curl "http://localhost:8080/api/nodes/events?range=15m&types=restart_suspected,node_id_conflict"
```

The same events as above, for all nodes at once, newest first: the Graph tab uses them to mark nodes that restarted or share a node-ID. Query params: `range` (as above, default `15m`), `types`, `limit` (1–2000, default `500`).

```json
{
    "now_unix": 1741949460.5,
    "events": [
        {"id": 12, "node_id": 37, "unique_id": "d74f8b69…", "timestamp_unix": 1741949445.123,
         "event_type": "restart_suspected", "detail": {"old_uptime": 812, "new_uptime": 3}}
    ]
}
```

`now_unix` is the server's clock at the time of the reply, so that a client can tell an event's age without relying on its own clock. Returns `400` for an invalid `range` or `limit`, `503` if the event logger is not available.

**Get node subject summary (aggregated telemetry stats per subject):**
```bash
curl http://localhost:8080/api/nodes/37/history/subjects
```

Query params:
- `unique_id` — filter by hardware unique ID hex string (preferred over node_id for stable identity)

Response:
```json
{
    "node_id": 37,
    "subjects": [
        {
            "subject_id": 1235,
            "message_type": "Temperature_1_0",
            "total_events": 4200,
            "avg_rate": 10.0,
            "first_seen_unix": 1741940000.0,
            "last_seen_unix": 1741949445.0
        }
    ]
}
```

Returns `400` for invalid node_id, `503` if the event logger is not available.

**Get service call history:**
```bash
curl "http://localhost:8080/api/services/430/history?range=7d&limit=50"
```

Query params:
- `range` — time window: `5m`, `15m`, `1h`, `6h`, `24h`, `7d` (default `7d`)
- `node_id` — filter history to a specific node
- `unique_id` — filter by hardware unique ID hex string (preferred over node_id for stable identity)
- `limit` — max entries, 1–200 (default `50`)

Response:
```json
{
    "service_id": 430,
    "history": [
        {
            "node_id": 37,
            "timestamp_unix": 1741949445.123,
            "service_id": 430,
            "service_type": "uavcan.node.GetInfo_1_0",
            "status": "ok",
            "latency_ms": 23,
            "response": "{\n  \"protocol_version\": {\n    \"major\": 1,\n    \"minor\": 0\n  },\n  ...\n}",
            "node_name": "org.example.my_node",
            "node_unique_id": [215, 79, 139, ...]
        }
    ]
}
```

`status` is one of `"ok"`, `"timeout"`, or `"error"`. The `response` field is present only for successful calls. `node_name` and `node_unique_id` are enriched from live telemetry when available.

Returns `400` for invalid service_id or limit, `503` if the event logger is not available.

**Set a register value on a node:**
```bash
curl -X POST http://localhost:8080/api/registers/37/set \
  -H 'Content-Type: application/json' \
  -d '{"name": "uavcan.node.id", "value": "42", "type": "natural16"}'
```

Response (success):
```json
{"status": "ok", "name": "uavcan.node.id", "value": "42"}
```

A number given for a register of another numeric type is converted to the register's type before it is sent. Returns `400` if required fields (`name`, `value`, `type`) are missing, the register is read-only, or the type is incompatible; `409` if the node answered with another value than the one written (it kept its own, as Cyphal allows; the error names what it holds), compared in the register's own precision; `504` if the node did not answer. Returns `503` if CAN is not connected.

### Recordings (`/api/recordings*`)

Named captures with optional length/event-count limits. Two storage modes:

- **`dedicated`** (default for new recordings) — matching subject events and service calls stream into a per-recording table (`recording_events`) from the moment the recording starts. Survives the global buffer's retention. Quick-save snapshots matching events from the global buffer into the same table at creation time.
- **`global`** — Phase 1 bookmarks. Metadata only; export reads from the shared `events` table within the recording's time-range × filter. Subject to global retention.

Recordings are kept in the data folder, so every recording endpoint works with CAN disconnected too: saved recordings are listed, exported, renamed, deleted and replayed (replay needs CAN disconnected). A live recording captures while CAN is connected; one started with CAN disconnected begins capturing once it connects.

#### List, create, inspect

```http
GET    /api/recordings                       → { recordings: [...] }
GET    /api/recordings/{rec_id}              → { recording: { ...stats } }
GET    /api/recordings/buffer                → { buffer: { ...global buffer stats } }
POST   /api/recordings                       body: { name, filter?, notes?, max_length_seconds?, max_events?, stop_on_limit? } → 201 { recording }
POST   /api/recordings/{rec_id}/stop         → { recording }
PATCH  /api/recordings/{rec_id}              body: { name?, notes?, max_length_seconds?, max_events?, stop_on_limit? } → { recording }
DELETE /api/recordings/{rec_id}[?purge=true] → { deleted, purged }
```

A POST without `end_unix` starts a live recording; `end_unix` stays `null` until stopped (manually or by hitting a limit). Stats use `now()` for the upper bound.

A recording row contains: `id`, `name`, `start_unix`, `end_unix`, `filter`, `notes`, `created_at`, `max_length_seconds`, `max_events`, `stop_on_limit`, `auto_stopped`, `event_count`, `events_source`, plus computed `subjects`, `duration_seconds`.

#### Limits and auto-stop

`max_length_seconds` and `max_events` are optional caps. With `stop_on_limit: true` (default off — opt in per recording), the recording auto-stops on the first limit breach: `end_unix` is set, `auto_stopped` becomes `true`, and the recording disappears from the active-routing registry. With `stop_on_limit: false`, the limits are soft targets — the recording keeps capturing past 100%, useful for showing progress bars in the UI without enforcing a cap.

PATCH changes only the fields it is sent (`null` counts as not sent), so a limit cannot be removed once set. A running recording takes new limits at once: one lowered below what it has reached stops it (with `stop_on_limit`) on its next event, or within 5 seconds.

Auto-stop is checked both on each matching event (during ingest) and via a 5-second background sweep (catches time-based limits when the bus is silent).

#### Retroactive "Quick save"

```http
POST   /api/recordings/quick   body: { name, last_seconds, filter?, notes? }
```

Snapshots matching events from the global buffer in `[now - last_seconds, now]` into the recording's dedicated store at creation time. Useful workflow: "the bus just glitched, save the last 30 seconds." Returns `201` with the full recording row. `last_seconds` is at most 86400, the history kept. A recording's `name` holds at most 200 characters and its `notes` 10,000 (400 otherwise).

#### Filter shape (OR-across-dimensions)

```json
{
  "subject_ids":   [7509, 7510],
  "service_ids":   [384],
  "node_ids":      [42],
  "message_types": ["Heartbeat_1_0"]
}
```

An event matches if it satisfies **any** dimension (subject in `subject_ids` OR node in `node_ids` OR service in `service_ids` OR type in `message_types`). Empty/missing keys impose no restriction on that dimension. Empty filter overall = capture everything. Unknown keys are ignored. Invalid types are dropped silently (non-integer ids, non-string types). `service_ids` matches `service_call` rows; it has no effect against the global `events` table for legacy bookmarks.

#### Service calls

A live recording takes every service call on the bus that its filter matches, between any two nodes, Cynitor's own among them. A Cyphal service call goes from one node to another, so Cynitor's own node hears only the calls made to it: the recordings take the calls from a listen-only tap on the bus instead (on SocketCAN a socket the kernel passes only service frames, behind the CAN hub a listener on its forwarding). Each request and response is rebuilt from its frames, and the two are paired by client, server, service-ID and transfer-ID. A call matches `service_ids` by its service-ID, and `node_ids` by its server or its client.

A call is stored as a `service_call` event: `service_id`; `publisher_node_id`, the server's node-ID; `message_type`, the service's type when Cynitor knows it (from the server's registers, or a standard fixed service-ID), else `null`; and its fields in `attributes`:

- `client_node_id` — the node that made the call.
- `status` — `ok`, or `timeout` when no response came within 5 seconds.
- `latency_ms` — from the request to the response, as heard on the bus; `null` for a timeout, or for a response whose request the recording did not hear.
- `request`, `response` — the transfer's fields as JSON text when Cynitor knows the type, else its bytes in hex; `null` when not heard.

Quick save holds no service calls: the global buffer it copies from holds subject events only. A call made from the Services panel is also kept in its node's history, as event type `service_call`.

#### Export

```http
GET    /api/recordings/{rec_id}/export?format=csv      → text/csv stream
GET    /api/recordings/{rec_id}/export?format=jsonl    → application/x-ndjson stream
```

CSV columns: `recording_id, timestamp_unix, timestamp, subject_id, service_id, publisher_node_id, unique_id, message_type, rate, attribute, value, unit`. One row per attribute (events with N attributes → N rows). A service call fills `service_id` instead of `subject_id`, gives its server's node-ID as `publisher_node_id` and its type as `message_type`, and takes a row per field of the call (`client_node_id`, `status`, `latency_ms`, `request`, `response`; see Service calls), the field's name in `attribute`.

JSONL (JSON Lines) streams one JSON object per line. The first line is a header: `{ "recording": {...}, "exported_at_unix": float }`. Every subsequent line is a single event object with `kind`, `subject_id`/`service_id`, `timestamp_unix`, `attributes`, etc. Streamed with the same pagination as CSV — no hard event cap.

Both formats set `Content-Disposition: attachment; filename="<sanitized-name>.<ext>"`.

A browser download cannot send an `Authorization` header; with a token set, pass it as `?token=<token>` (the dashboard does).

### Raw CAN logs

Every frame on the bus, in both directions, written to a candump `.log` file (python-can's `CanutilsLogWriter`). python-can (`can.LogReader`), SavvyCAN and can-utils (`canplayer`, `log2asc`, which also converts to Vector ASC) read it. It holds Classic and CAN FD frames and error frames, each with its direction (`R`/`T`). Separate from recordings, which hold decoded events: one raw log runs at a time, and it stops when CAN disconnects. It also stops by itself at 4 GiB, or when less than 512 MiB is left on the disk, and says why in `error`; a start with less than that free answers 409. Files live in the data folder under `raw/`, named `cynitor-YYYYMMDD-HHMMSS.log`.

```http
GET    /api/rawlogs           → { active: {name, frames, bytes, started_unix, error} | null, logs: [{name, bytes, modified_unix}] }
POST   /api/rawlogs           → 201 { name, frames, bytes, started_unix, error }   (409 if CAN is not connected, a log runs, or the disk is nearly full)
POST   /api/rawlogs/stop      → { name, frames, bytes, started_unix, error }       (409 if none runs)
GET    /api/rawlogs/{name}    → the file, as an attachment                          (404 if no such log)
DELETE /api/rawlogs/{name}    → { deleted }                                         (409 while it runs, 404 if no such log)
POST   /api/rawlogs/{name}/play { speed } → { status, can_interface, can_fd }   (409 if CAN is connected, 404 if no such log, 400 bad speed)
```

**Playback.** `POST /api/rawlogs/{name}/play` opens the log as if it were the bus: the session runs as usual (nodes, subjects, telemetry WebSocket, recordings), with `can_interface` `rawlog:<name>` in `/api/status`. `speed` is 1 (logged pace) to 1000, or 0 for as fast as the file can be read; default 1. Frames the recording Cynitor sent itself are left out, and nothing is sent: GetInfo, register reads and commands get no answer. The session ends by itself at the end of the log, and `POST /api/can/disconnect` ends it earlier.

When a log stops, what the session knew about the bus is saved beside it as `cynitor-YYYYMMDD-HHMMSS.types.json`: each node's published subjects and their types, its servers and name, Cynitor's node-ID, the bitrates and whether it was CAN FD. Playback reads it, so subjects typed through registers still decode. Without it (a log from elsewhere or an older Cynitor), only fixed-port subjects decode, and CAN FD is detected from the frames. Deleting a log deletes its sidecar.

`logs` includes the active log's file; `error` is set if writing failed (e.g. the disk is full), which ends the log. Only names of the `cynitor-YYYYMMDD-HHMMSS.log` form reach a file. The frames come from the CAN hub for adapters Cynitor opens itself, and on SocketCAN from a second, listen-only socket, whose timestamps are the kernel's. On SocketCAN a frame is marked `T` when the kernel says it was created on this computer, whichever program sent it (Cynitor, or on a `vcan` every node), and `R` when it came from the bus.

#### Global buffer (`GET /api/recordings/buffer`)

Stats about the shared `events` ring used by legacy bookmarks and quick-save:

```json
{
  "buffer": {
    "retention_seconds": 86400,
    "max_events": 5000000,
    "event_count": 142057,
    "oldest_event_unix": 1748742543.21,
    "newest_event_unix": 1748828943.18,
    "db_size_bytes": 38420480
  }
}
```

Use this to surface "what's available for quick-save" and to estimate observed message rate (count / (newest - oldest)).

### Replay (`/api/replay/*`)

A recording stored under `recording_events` can be streamed back through the same WebSocket the live telemetry flows on. Replay events carry the `replay: true` field but otherwise look identical to live events — frontends consume them through their normal cache path.

**Invariant:** replay is mutually exclusive with an active CAN session. Starting replay while CAN is connected returns `409`; calling `/api/can/connect` while a replay is running returns `409` with the same body shape. Only one replay session at a time; multiple browser tabs all watch the same one.

**Start playback:**
```bash
curl -X POST http://localhost:8080/api/replay/start \
  -H 'Content-Type: application/json' \
  -d '{"recording_id": 42, "speed": 2.0, "start_offset_s": 0}'
```
- `recording_id` (required, integer)
- `speed` (optional, 0.1–50.0, default `1.0`)
- `start_offset_s` (optional, seconds from the recording's first event)

Response on success — current replay status:
```json
{
  "active": true,
  "recording_id": 42,
  "position_s": 0.0,
  "duration_s": 32.4,
  "speed": 2.0,
  "paused": false,
  "events_emitted": 0,
  "total_events": 65,
  "finished": false
}
```

Returns `404` if the recording has no `kind='subject'` rows to replay, `409` if CAN is connected or another replay is in progress.

**Control playback (pause / resume / stop):**
```bash
curl -X POST http://localhost:8080/api/replay/control \
  -H 'Content-Type: application/json' \
  -d '{"action": "pause"}'
```
`action` is one of `"pause"`, `"resume"`, `"stop"`. Returns the latest status (or `{"active": false}` after stop). `404` if no replay is running and the action is not `stop`; `400` for an unknown action.

**Seek to a position:**
```bash
curl -X POST http://localhost:8080/api/replay/seek \
  -H 'Content-Type: application/json' \
  -d '{"position_s": 12.5}'
```
`position_s` is clamped to `[0, duration_s]`. Playback moves there at once, also between two events, and `events_emitted` then counts the events before that position. Returns 404 if no replay is running.

**Change speed without re-seeking:**
```bash
curl -X POST http://localhost:8080/api/replay/speed \
  -H 'Content-Type: application/json' \
  -d '{"speed": 5.0}'
```
`speed` is clamped to `[0.1, 50.0]`. Returns 404 if no replay is running.

**Query current status (poll-friendly):**
```bash
curl http://localhost:8080/api/replay/status
```
Returns the same shape as the start response; `{"active": false}` when no replay is running.

**Replay-ended WebSocket sentinel:**

When replay terminates — naturally at the end of the recording, or because a client called `stop` — the backend pushes one frame to every subscribed client before tearing the WS down:
```json
{
  "type": "replay_ended",
  "recording_id": 42,
  "finished": true
}
```
`finished` distinguishes the two paths: `true` means playback reached the end, `false` means a client stopped it. Frontends can use this to switch back to an idle state without polling.

**MVP scope notes:**
- Only `kind='subject'` rows are replayed. `service_call` rows stay in storage for export but aren't played.
- `/api/nodes` during replay is synthesised from the recording's publisher list — no GetInfo / health / mode / uptime / client port lists. The placeholder payload carries `_replay: true` on each node so consumers can flag the view as approximate.

### Subject types

Cynitor decodes a subject when a publisher names its type in its registers (`uavcan.pub.<name>.type`) or the subject-ID is a fixed one. For any other subject the user can set the type; it is saved in the data folder (`subject_types.json`) and applied on every connect, raw-log playback included.

```http
PUT    /api/subjects/{subject_id}/type { type } → { subject_id, type }   (400 not a compiled message type, 409 registers name it or not connected)
DELETE /api/subjects/{subject_id}/type          → { subject_id, type: null }
GET    /api/subjects/{subject_id}/type-guesses  → { samples, matches, candidates: [{type, custom, preview}] }   (409 already decoded or not connected)
```

`type` is a DSDL name, `uavcan.si.unit.velocity.Vector3.1.0` (the compiled spelling `..._1_0` works too). The registers' word wins: once a publisher names the subject's type, the user's type stops applying.

`type-guesses` listens to the subject for 3 s (up to 20 messages) and returns the compiled message types every payload fits: decoded and encoded again, it gives back exactly the same bytes (on CAN FD, plus the zeros that fill a frame up). Up to 100, best first: plausible values (no NaN, infinity, or float beyond 1e12 or below 1e-12), then custom types, then types without a fixed subject-ID of their own. `preview` is the latest payload decoded as that type. `samples` is 0 if nothing was published on the subject meanwhile.

### Test nodes

Cyphal nodes Cynitor runs on the bus for testing, made in the Test nodes tab (`server/testnodes.py`). Each has a name, a node-ID of its own or one Cynitor gives it, health and mode, and up to 8 publishers. They are kept in the data folder (`test_nodes.json`), run while CAN is connected (not while a raw log plays), and all stop when it disconnects.

```http
GET    /api/testnodes                      → { test_nodes: [node], bus, health, modes, waves, limits }
POST   /api/testnodes          definition  → 201 node        (400 not a valid definition, 409 at the limit of 16)
PUT    /api/testnodes/{id}     definition  → node            (400, 404, 409 its node-ID is taken, or none is free, while it runs)
DELETE /api/testnodes/{id}                 → { deleted }     (404); a running one stops first
POST   /api/testnodes/{id}/start { confirm_real_bus? } → node   (404, 409 see below, 400 its type changed under it)
POST   /api/testnodes/{id}/stop            → node            (404)
```

A definition:

```json
{"name": "test.battery", "node_id": 100, "health": "nominal", "mode": "operational", "vendor_status": 0,
 "software_version": {"major": 1, "minor": 0},
 "publishers": [{"subject_id": 1000, "type": "uavcan.si.unit.voltage.Scalar.1.0", "rate_hz": 10, "port": "battery",
                 "value": {"volt": {"$wave": "sine", "min": 11, "max": 12.6, "period_s": 20}}}]}
```

`name` is 1 to 50 bytes; `node_id` 1 to 125, or `null` (or left out) to leave it to Cynitor, which then gives the node a free one each time it starts; `health` `nominal`, `advisory`, `caution` or `warning`; `mode` `operational`, `initialization`, `maintenance` or `software_update`; `vendor_status` 0 to 255. Each publisher has a `subject_id` (0 to 8191; not 7509 or 7510, the node's own heartbeat and port list), a compiled message `type`, a `rate_hz` (0.01 to 100, default 1) and a `port` name for its registers (`uavcan.pub.<port>.id` and `.type`), which defaults to one made from the type (`voltage_scalar`). Only `name` and `node_id` are required; a 400 says which field is wrong, as `publishers[0].value.volt: ...`.

`value` is the message in pycyphal's builtin form: an object for a composite, its fields left out keeping their defaults; a list for an array; a string for a byte array; an object of one variant for a union. Any number may instead be a waveform, `{"$wave", "min", "max", "period_s"}`: `sine`, `ramp` and `square` run from `min` to `max` over `period_s` seconds (a sine starting at `min`; a square at `min` for its first half), `noise` is random between them, and `count` adds one per message, from `min` and wrapping after `max` (no period). Every number is rounded and clamped to its field's range as it is sent.

A node, as listed, is its definition with its `id`, the `unique_id` it gives in GetInfo (hex), `assigned_node_id`, `running`, `started_unix`, and `error`: why one of its publishers stopped, while it runs. `assigned_node_id` is the node-ID Cynitor gave a node whose `node_id` is `null` when it last started, which it runs on while it runs; `null` before its first start, and for a node with a `node_id` of its own. A definition sent back with `PUT` leaves these six out. `bus` is null while CAN is not connected, else `{"simulated", "interface"}`; `limits` gives the ranges above (`node_id`, `subject_id`, `rate_hz`), the most test nodes (`test_nodes`), publishers each (`publishers`) and messages a second of all running test nodes together (`total_rate_hz`, 1000).

A change of `health`, `mode` or `vendor_status` a running node takes as it runs; any other change restarts it. Starting one is refused with 409 while CAN is not connected, when another test node runs on its node-ID, when a node on the bus has it (Cynitor's own included), or when it would take all of them over `total_rate_hz`. One with no `node_id` is not refused for a node-ID: it takes the one it was given last time while that is free, so that it stays the same node from run to run, else the first free from 100 up, then from 1; never one a node on the bus has (Cynitor's own included), or that another test node is set to or was given. Only with none free is it refused. On a real bus, as opposed to a simulated one, it is also refused, with `{"error", "confirm": "real_bus"}`, until it is asked for again with `{"confirm_real_bus": true}`: its messages reach every node on the bus, and may move real things.

### Firmware updates

A node with a Cyphal bootloader (e.g. Zubax Kocherga) updates itself from a file Cynitor serves. Cynitor sends it `uavcan.node.ExecuteCommand` `COMMAND_BEGIN_SOFTWARE_UPDATE` (65533) with the file name as the parameter; the node restarts into its bootloader and reads the file, 256 bytes at a time, with `uavcan.file.Read` from Cynitor's node-ID. Checking the image and starting it is the bootloader's job. Files live in the data folder under `firmware/`.

```http
GET    /api/firmware                → { files: [{name, bytes, modified_unix}], updates: { "<node_id>": update } }
POST   /api/firmware?name=<name>    → 201 { name, bytes }   body: the file (application/octet-stream); replaces a file of that name
DELETE /api/firmware/{name}         → { deleted }           (404 if no such file)
POST   /api/nodes/{node_id}/firmware { file } → update      (404 no such file, 409 refused or nothing can be sent, 504 no answer)
```

An `update` is `{ file, bytes, read, state, started_unix, updated_unix, mode }`: `read` is how far the node has read the file, `state` is `requested` (the node accepted the command), `reading`, or `transferred` (it read the last, short chunk), and `mode` the mode in its latest heartbeat (`SOFTWARE_UPDATE` in the bootloader, `OPERATIONAL` once the new firmware runs), or null. Updates are kept per session and end with it.

Names are plain file names (letters, digits, `.`, `_`, `-`; up to 128 characters); a file holds 1 byte to 32 MiB (400 empty, 413 larger), and all of them together 256 MiB (413 "delete one first"). Only `uavcan.file.Read` is served, and only for these files: other nodes cannot list, write or delete files on this computer. Nothing is served while a raw log plays or when Cynitor has no node-ID; then the update is refused with 409.

### Event Logger (SQLite)

Events are automatically logged to `telemetry_events.db`. The `events` table has fields:
- `id` (auto-increment)
- `subject_id`
- `timestamp`, `timestamp_unix`
- `rate`, `message_type`
- `publisher_node_id`, `unique_id`
- `attributes` (JSON)
- `created_at` (database timestamp)

**Retention.** The global `events` table is pruned by **time-based retention** (default 24 hours). A hard event-count cap (`max_events`, default 5,000,000) acts as a safety net only — it bounds disk if rate × retention would otherwise blow past it. Events are written in transactions of up to 500 (about 20,000 events/s on an SSD). Pruning runs every 1000 writes; configure both via `EventLogger(retention_seconds=..., max_events=...)`.

Per-recording event stores (`recording_events`) are **not** subject to retention: a recording keeps its events until it is deleted, which deletes them too. `?purge=true` also deletes a legacy bookmark's time range from the global `events` table (a bookmark keeps no events of its own). Recording rows survive global retention by definition.

**Query logged events programmatically:**
```python
from event_logger import EventLogger

logger = EventLogger("telemetry_events.db")

# Get last 100 Heartbeat events from node 42
events = logger.get_events(
    node_id=42,
    message_type="Heartbeat_1_0",
    limit=100
)

# Clear old data
logger.clear_events()

# Check event count
count = logger.get_event_count()
```

## Configuration

### Port, host and data folder

Set on the command line, as the README's Options table lists them: `--port <n>` (default 8080),
`--bind <host>` (default `127.0.0.1`; beyond loopback only with `CYNITOR_AUTH_TOKEN` set, or with
`--insecure-no-auth`), and `--data-dir <dir>` (or `CYNITOR_DATA_DIR`) for where history, recordings and raw
logs are kept.

```bash
CYNITOR_AUTH_TOKEN=$(openssl rand -hex 24) python3 main.py --can vcan0 --bind 0.0.0.0 --port 9000
```

## Architecture Components

### 1. ScannerNode (`scanner_node.py`)
- Subscribes to standard UAVCAN messages: Heartbeat (7509), port.List (7510), PnP NodeIDAllocation v1/v2 (8166/8165)
- Discovers nodes and their publishers/services
- Decodes and extracts message attributes
- Forwards all standard messages as telemetry events to the queue
- Emits standardized JSON events to queue

### 2. TelemetryManager (`telemetry_manager.py`)
- Consumption loop for message_queue
- Maintains latest-state cache (by subject and node)
- Normalizes events into JSON-safe payloads before caching/broadcasting
- Broadcasts events to all subscribers
- Supports multiple concurrent consumers

### 3. WebSocketServer (`websocket_server.py`)
- Async HTTP/WebSocket server (aiohttp)
- Client connection management
- Event filtering (subject_id, node_id, message_type)
- REST API for queries: every route is listed in `_setup_routes`; the handlers are in the `api_*.py` modules, one a part of the API, as classes the server inherits

### 4. EventLogger (`event_logger.py`)
- SQLite persistence layer
- Async batch writing for efficiency
- Flushes queued events during shutdown
- Query API for historical analysis
- Node lifecycle history (health/mode changes, service calls) with 30-day retention
- Service call history with response body storage
- Configurable retention limits

## Performance Considerations

The server takes the bus's traffic on one core: every frame is handed to pycyphal on the event loop, and every message of a decoded subject is deserialized, turned into an event, fanned out and queued for SQLite there. What it works through in a second, measured on a desktop (Intel i7-10700K) through the CAN hub, with a backlog waiting:

| | Single-frame messages | Ten-frame messages (16 floats, Classic CAN) |
|---|---|---|
| Every subject decoded | about 6,000 | about 1,100 |
| Decode on demand, subjects nobody watches | about 23,000 | about 2,600 |

For scale, a 1 Mbit/s Classic CAN bus carries about 8,000 frames a second when full. A Raspberry Pi 4 core runs this several times slower (an estimate, not measured). SocketCAN, which needs no hub, was not measured.

- `server_cpu` (in `/api/status` and the `metrics` message, shown in the dashboard's Server section) says how near the server is to its ceiling: near 100 % it cannot keep up.
- Past the ceiling, the event logger loses messages first (`dropped.logger`): alone it writes about 30,000 rows a second, but it shares the core with the decoding. Then a subject whose decoding has fallen a second behind loses the frames it has waiting (`dropped.scanner`), all at once, so that the live view stays within about a second of the bus instead of falling ever further behind, and what is decoded is still whole. On SocketCAN, frames the kernel's receive queue had no room for are counted there too.
- [Decode on demand](#5-decode-on-demand) is for a bus past the ceiling: what nobody watches is counted, not decoded.

Use `max_events` in EventLogger to prevent database bloat.

## Troubleshooting

**WebSocket connection refused:**
- Ensure `main.py` is running
- Check firewall (port 8080)
- The server binds to `127.0.0.1` by default; if you need to reach it from another host, restart with `--bind 0.0.0.0` and `CYNITOR_AUTH_TOKEN` set (see the authentication section above)

**High memory usage:**
- Reduce TelemetryManager queue size
- Reduce EventLogger max_events
- Check number of WebSocket clients

**No events appearing:**
- Verify you started with `python3 main.py --can <iface>` or completed startup selection API
- Check nodes are broadcasting (start other nodes)
- Remember `/api/latest/node/{node_id}` only returns cached publisher traffic; a discovered node with no cached telemetry returns `404` with `known_node: true`
- Look for errors in logs: `ERROR` or `WARNING` messages

**Database locked errors:**
- Reduce batch write size
- Check for other processes accessing the DB
- Use WAL mode: `sqlite3 telemetry_events.db "PRAGMA journal_mode=WAL;"`

## Development

Run with debug logging:
```bash
PYTHONPATH=/path/to/workspace python3 -c "
import logging
logging.basicConfig(level=logging.DEBUG)
import asyncio
from main import main
asyncio.run(main())
"
```

## Files

| File | Purpose |
|---|---|
| `scanner_node.py` | UAVCAN network scanner |
| `scanner_registers.py` | The scanner's register reads and writes |
| `scanner_services.py` | The scanner's service clients and calls |
| `telemetry_manager.py` | Event pub-sub router |
| `node_info.py` | Per-node state (lifecycle, ports, getInfo) |
| `node_identity_map.py` | Bidirectional unique_id ↔ node_id mapping with migration detection |
| `websocket_server.py` | WebSocket/REST server: routes, access, the live stream |
| `api_*.py` | The REST API's handlers, a module a part of the API (CAN, raw logs, test nodes, firmware, nodes, recordings, DSDL) |
| `log_store.py` | In-memory API log buffer + log handler |
| `event_logger.py` | SQLite persistence |
| `allocator.py` | Cynitor's node-ID allocator, off unless asked for; answers from the scanner's node |
| `transfer_rates.py` | Message rates counted from CAN frames, for the subjects not decoded while decoding on demand |
| `cpu_load.py` | The server's own CPU use, for the dashboard |
| `startup_setup.py` | DSDL compilation via nnvg, env setup |
| `main.py` | System orchestration |

## License

MIT, as the repository's `LICENSE` file says.
