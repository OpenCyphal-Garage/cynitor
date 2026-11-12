# Cynitor MCP Server Adapter

An [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) server that wraps Cynitor's REST API, so AI agents can monitor and interact with a Cyphal/CAN bus through natural language.

## What This Does

The adapter runs as a separate process alongside the Cynitor server. It translates MCP tool calls into Cynitor REST API calls, letting an AI agent (Claude, or any MCP-compatible client) do things like:

- "What nodes are on the bus?" → `list_nodes` → `GET /api/nodes`
- "Read the registers of node 42" → `get_node_registers(42)` → `GET /api/registers/42`
- "Save the last 30 seconds of telemetry" → `quick_save("glitch", 30)` → `POST /api/recordings/quick`

The adapter does **not** replace the dashboard or the REST API. It is a thin layer on top of them.

## Architecture

```
AI Agent (Claude)
    ↓ MCP (stdio)
MCP Server (this)
    ↓ HTTP (localhost)
Cynitor Server
    ↓ CAN
Cyphal Network
```

- **Transport**: stdio (Claude launches the process, talks over stdin/stdout)
- **HTTP client**: httpx.AsyncClient talking to Cynitor's REST API
- **SDK**: FastMCP from the `mcp` Python package
- **Process model**: separate from Cynitor — no shared imports, no coupling to pycyphal or the CAN session lifecycle

## Tools (v1)

19 tools grouped by what an AI agent needs to do with a CAN bus. These are **not** a 1:1 mapping of Cynitor's 67 REST endpoints — related calls are grouped into AI-friendly tools with clear descriptions.

### Connection & Status

| Tool | Parameters | What it does |
|------|-----------|--------------|
| `get_status` | — | Bus state, interface, utilization, own node-ID. The tool the AI calls first to orient itself. |
| `connect_can` | `interface`, `bitrate?`, `data_bitrate?`, `listen_only?`, `no_commands?` | Connect to a CAN interface (SocketCAN name, adapter spec, or `sim:classic`/`sim:fd`). |
| `disconnect_can` | — | Disconnect from CAN. |

### Node Discovery & Configuration

| Tool | Parameters | What it does |
|------|-----------|--------------|
| `list_nodes` | `limit?` | All discovered nodes: node-ID, name, health, mode, uptime, port counts. |
| `get_node_detail` | `node_id` | Everything about one node: identity, health, publishers with latest values, subscribers, servers, clients. |
| `get_node_registers` | `node_id` | All registers of a node (sends register-read requests on the bus). |
| `set_node_register` | `node_id`, `name`, `value`, `type` | Write a register value on a node. |

### Messages & Subjects

| Tool | Parameters | What it does |
|------|-----------|--------------|
| `get_latest_subject` | `subject_id` | Last decoded message on a subject: attributes, publisher, type, rate. |
| `set_subject_type` | `subject_id`, `type_name` | Tell Cynitor how to decode a subject (full DSDL name). |
| `guess_subject_type` | `subject_id` | Listen to a subject for 3 seconds and rank the DSDL types that fit. |

### Services

| Tool | Parameters | What it does |
|------|-----------|--------------|
| `get_node_services` | `node_id` | Services a node exposes, with request/response schemas. |
| `call_service` | `node_id`, `service_id`, `attributes?` | Invoke a Cyphal service on a node (e.g. GetInfo, ExecuteCommand). |

### History & Events

| Tool | Parameters | What it does |
|------|-----------|--------------|
| `get_bus_events` | `range?`, `types?`, `limit?` | Recent lifecycle events across all nodes (appeared, disappeared, health changes, restarts). |
| `get_node_history` | `node_id`, `range?`, `types?`, `limit?` | Event history for one specific node. |

### Recordings

| Tool | Parameters | What it does |
|------|-----------|--------------|
| `list_recordings` | — | All recordings with status, event count, duration. |
| `start_recording` | `name`, `max_length_seconds?`, `max_events?` | Start capturing bus events. |
| `stop_recording` | `recording_id` | Stop a recording. |
| `quick_save` | `name`, `last_seconds` | Save the last N seconds of history retroactively ("save what just happened"). |

### DSDL

| Tool | Parameters | What it does |
|------|-----------|--------------|
| `lookup_dsdl_type` | `full_name` | Type definition: fields, their types and ranges, fixed port-ID. |

### Planned for v2

CAN adapter listing, transport diagnostics, server logs, DSDL namespace browsing and compilation, custom DSDL authoring, firmware management, test nodes, raw CAN logs, replay control, recording export, service call history, identity map management.

## Real-Time Telemetry

MCP is request-response. Cynitor's live telemetry streams over a WebSocket at hundreds of messages per second — this does not map to MCP's model. The adapter handles this by offering snapshot tools (`get_latest_subject`, `get_node_detail`) rather than a continuous feed. The dashboard remains the right tool for watching the bus in real time.

If MCP gains a streaming primitive in the future, the adapter can add a telemetry subscription tool.

## Configuration

Two environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CYNITOR_URL` | `http://127.0.0.1:8080` | Cynitor server base URL |
| `CYNITOR_AUTH_TOKEN` | *(empty)* | Bearer token, if Cynitor requires one |

### Claude Code

Add `.mcp.json` at the repo root:

```json
{
  "mcpServers": {
    "cynitor": {
      "command": "python3",
      "args": ["mcp_server/server.py"],
      "env": {
        "CYNITOR_URL": "http://127.0.0.1:8080"
      }
    }
  }
}
```

### Claude Desktop

Add to `~/.claude/claude_desktop_config.json` under `mcpServers`, same shape.

## Dependencies

```
mcp[cli]>=1.0
httpx>=0.27
```

Python 3.10+. Runs in its own process — no conflict with pycyphal, aiohttp, or anything in `server/requirements.txt`.

## File Structure

```
mcp_server/
    __init__.py
    server.py            # FastMCP instance + all tool definitions
    client.py            # CynitorClient: thin httpx wrapper
    requirements.txt
    tests/
        __init__.py
        test_tools.py
```

Three real files. Every tool is a decorated function in `server.py`. No subdirectories, no abstractions.

## Design Decisions

**Not 1:1 endpoint mapping.** The 67 endpoints serve a real-time dashboard that polls every second. An AI agent asks questions. `get_node_detail` combines 2-3 API calls into "tell me everything about node 42" because that is how an AI thinks.

**Formatted text, not raw JSON.** MCP tools can return text or structured content. The adapter returns compact, readable summaries. This also lets it truncate large outputs (100+ nodes) without the AI parsing incomplete JSON.

**Separate process.** Cynitor's server imports pycyphal, python-can, and aiohttp — a dependency tree that takes seconds to load. The HTTP boundary means the MCP server can restart without touching the CAN session, and vice versa.

**stdio transport first.** Zero networking config, works with Claude Desktop and Claude Code out of the box. Streamable HTTP can be added later if a remote MCP client needs it.

## Testing

```bash
# Unit tests (mocked HTTP)
cd mcp_server && python -m pytest tests/ -v

# Interactive testing with MCP Inspector
mcp dev mcp_server/server.py
# → opens Inspector at http://localhost:6274

# Needs a running Cynitor server (simulated bus is enough)
cd server && python3 main.py --can sim:classic
```

## Status

This is a proposal. The tool list and design are open for discussion. See the [OpenCyphal forum thread](https://forum.opencyphal.org/t/proposal-a-formal-implementation-independent-specification-for-the-cynitor-server-protocol/2895) for context.
