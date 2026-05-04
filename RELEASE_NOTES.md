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
