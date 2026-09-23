# apeiron-mcp

An MCP server exposing client health data. Tool surface is intentionally
consolidated by *data shape* (time-series vitals, periodic assessments,
unstructured text, derived aggregates) rather than by raw domain, to keep
LLM tool-selection unambiguous.

## Tools

| # | Tool | Purpose |
|---|------|---------|
| 1 | `get_sleep_data` | Sleep stages, duration, efficiency, HRV, score. |
| 2 | `get_exercise_data` | Workouts: type, duration, calories, HR zones, RPE. |
| 3 | `get_nutrition_data` | Meals, macros, calories, hydration, supplements. |
| 4 | `get_cardio_metrics` | Cardio + aerobic (resting HR, HRV, VO2max, BP, aerobic capacity). |
| 5 | `get_fitness_assessment` | Bone density, body comp, balance, movement, muscle strength. |
| 6 | `get_cognitive_data` | Cognitive test batteries and trends. |
| 7 | `get_healthspan_domain_summary` | Cross-domain rolled-up scores. |
| 8 | `get_lifestyle_summary` | Sleep/activity/nutrition adherence rollup. |
| 9 | `get_trends` | Generic time-series trend for any (domain, metric). |
| 10 | `get_notes` | Clinician/client/system free-text notes. |
| 11 | `get_chat_history` | Paginated coaching conversation logs. |

All tools return the envelope:

```json
{
  "client_id": "...",
  "domain": "...",
  "period": {"start": "...", "end": "..."},
  "data": [],
  "unit_system": "metric",
  "last_synced_at": "..."
}
```

## Install & run

```bash
pip install -e .
apeiron-mcp                                # stdio (default)
MCP_TRANSPORT=streamable-http apeiron-mcp  # HTTP on http://127.0.0.1:8000/mcp
```

The transport is selected with `MCP_TRANSPORT`; see
[Configuration](#configuration) for the bind address, port and other options.

## Register with Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "apeiron": {
      "command": "apeiron-mcp"
    }
  }
}
```

## Run with Docker

The image is host-agnostic: it defaults to the network-friendly
`streamable-http` transport so it can run as a long-lived service, and it can
also be driven over stdio by a local MCP client.

### Build

```bash
docker build -t apeiron-mcp:0.1.0 .
```

### Run as an HTTP service (recommended for deployment)

```bash
docker run -d --name apeiron-mcp \
  -p 8000:8000 \
  -e APEIRON_ACCESS_TOKEN="$APEIRON_ACCESS_TOKEN" \
  --restart unless-stopped \
  apeiron-mcp:0.1.0
```

The endpoint is then `http://localhost:8000/mcp`. Verify it:

```bash
curl -i -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

Prefer a plain-JSON reply (easier to script against)? Add
`-e MCP_JSON_RESPONSE=true`.

### Run over stdio (for a local MCP client)

`-i` keeps stdin attached and the image writes all logs to **stderr**, so
stdout stays a clean JSON-RPC stream:

```bash
docker run -i --rm -e MCP_TRANSPORT=stdio apeiron-mcp:0.1.0
```

Point a client at it with:

```json
{
  "mcpServers": {
    "apeiron": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-e", "MCP_TRANSPORT=stdio", "apeiron-mcp:0.1.0"],
      "env": { "APEIRON_ACCESS_TOKEN": "..." }
    }
  }
}
```

### With Docker Compose

```bash
cp .env.example .env      # then edit APEIRON_ACCESS_TOKEN
docker compose up -d --build
docker compose logs -f
```

The `apeiron-mcp` service is started by default; a stdio service lives behind a
profile (add `-T` when running from a script or CI, where no TTY is attached):

```bash
docker compose --profile stdio run --rm apeiron-mcp-stdio
```

The container runs as a non-root user (uid/gid 10001) with a read-only
root filesystem, all Linux capabilities dropped and a built-in `HEALTHCHECK`
(TCP probe on the configured port). Host port mapping is controlled by
`MCP_HOST_PORT` (host side) and `MCP_PORT` (container side, default `8000`).

### Configuration

Backend credentials:

| Variable | Default | Purpose |
|----------|---------|---------|
| `APEIRON_BASE_URL` | `https://api.apeiron.life` | Backend base URL. |
| `APEIRON_ACCESS_TOKEN` | *(unset)* | Bearer token. **When unset the tools return stub data.** |
| `APEIRON_TIMEOUT` | `30` | Per-request timeout in seconds. |

Server behaviour:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCP_TRANSPORT` | `stdio` (image: `streamable-http`) | `stdio`, `streamable-http` or `sse`. |
| `MCP_HOST` | `127.0.0.1` (image: `0.0.0.0`) | HTTP bind address. |
| `MCP_PORT` | `8000` | HTTP bind port. |
| `MCP_STATELESS_HTTP` | `false` | Run without HTTP sessions (use behind a load balancer without sticky sessions). |
| `MCP_JSON_RESPONSE` | `false` | Reply with plain JSON instead of SSE streams. |
| `MCP_ALLOWED_HOSTS` | *(unset)* | Comma-separated `Host` allowlist, matched against the full `Host` header **including the port**; setting it enables DNS-rebinding protection (other hosts get `421`). Use `name:*` for any port. |
| `MCP_ALLOWED_ORIGINS` | *(unset)* | Comma-separated `Origin` allowlist. |
| `MCP_HOST_PORT` | `8000` | **Compose only** — host port published by `docker-compose.yml` (maps to container `MCP_PORT`). |

> **Security note:** binding to a non-loopback address (`0.0.0.0`, required in a
> container) disables the SDK's `Host`/`Origin` validation so the service is
> reachable — the server logs a warning when this happens. When you expose the
> service beyond a trusted network, set `MCP_ALLOWED_HOSTS` (e.g.
> `apeiron.example.com,apeiron.example.com:*` — list both forms, because entries
> are matched against the `Host` header including its port) and terminate TLS at
> a reverse proxy. The server itself ships no authentication layer, so do not
> publish the port directly to the internet.

## Wiring to a real backend

The current implementation returns stub payloads. Replace the bodies of the
functions in `src/apeiron_mcp/server.py` with calls into your health data
backend (HTTP client, DB, etc.).
