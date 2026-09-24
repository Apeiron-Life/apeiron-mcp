# apeiron-mcp

An MCP server exposing client health data from the Apeiron backend
(`https://api.apeiron.life`). Tool surface is intentionally consolidated by
*data shape* (time-series vitals, periodic assessments, unstructured text,
derived aggregates) rather than by raw domain, to keep LLM tool-selection
unambiguous.

## Tools

| # | Tool | Purpose | Apeiron API endpoint |
|---|------|---------|----------------------|
| 1 | `get_sleep_data` | Sleep analytics for a date window. | `GET /activity_feed/sleep-analytics`¹ |
| 2 | `get_exercise_data` | Workout analytics for a date window. | `GET /activity_feed/exercise-analytics`¹ |
| 3 | `get_nutrition_data` | Nutrition score + per-question scores. | `GET /nutrition_score/`, `GET /nutrition_score/details` |
| 4 | `get_cardio_metrics` | Cardio + aerobic (resting HR, HRV, VO2max, BP, aerobic capacity). | `GET /datasets/cardiorespiratory` |
| 5 | `get_fitness_assessment` | Body comp, bone density, balance, movement, muscle strength. | `GET /datasets/{body_comp,physical_assessment,bio_marker}` |
| 6 | `get_cognitive_data` | Cognitive assessment datasets and trends. | `GET /datasets/cognitive_health` |
| 7 | `get_healthspan_domain_summary` | Cross-domain rolled-up healthy-signal scores. | `GET /health_status/`, `GET /healthy_signals/` |
| 8 | `get_lifestyle_summary` | Lifestyle/adherence rollup for a period. | `GET /datasets/lifestyle_assessment`, `GET /summary_card/` |
| 9 | `get_trends` | Generic trend statistics for any (domain, metric). | `GET /trend_statistics/` |
| 10 | `get_notes` | Clinician/client/system free-text notes. | `GET /note/` |
| 11 | `get_chat_history` | Coaching chat messages (most recent first). | `GET /message/` |

¹ Uses the ``…-day`` variant (`GET /activity_feed/sleep-analytics-day`,
`GET /activity_feed/exercise-analytics-day`) when the requested window is a
single day.

See [Endpoint mapping](#endpoint-mapping) for the parameters sent to each
endpoint and the filters that are applied server-side vs. locally.

All tools return the envelope:

```json
{
  "client_id": "...",
  "domain": "...",
  "period": {"start": "...", "end": "..."},
  "data": [],
  "unit_system": "metric",
  "last_synced_at": "...",
  "source": "apeiron-api",
  "notes": ["..."]
}
```

* `source` is `"apeiron-api"` for live data and `"stub"` when no credentials are
  configured.
* `notes` is only present when there is something to explain: a filter the
  server could not apply, a retried request, or an argument the API ignores.
* `data` carries the API payload as returned by the backend — the server does not
  reshape it into an invented schema, so field names and units (`weight_lbs`,
  `height_inch`, …) are the backend's.
* `client_id` is the Apeiron `person_id` and must be numeric whenever credentials
  are configured.

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
| `APEIRON_ACCESS_TOKEN` | *(unset)* | Bearer token for the API. |
| `APEIRON_EMAIL` | *(unset)* | Login email — used to mint a token when no bearer token is set. |
| `APEIRON_PASSWORD` | *(unset)* | Login password. |
| `APEIRON_TIMEOUT` | `30` | Per-request timeout in seconds. |

With none of `APEIRON_ACCESS_TOKEN` / `APEIRON_EMAIL`+`APEIRON_PASSWORD` set, the
tools return labelled stub data (`"source": "stub"`) instead of API results, so
the demos keep working offline.

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

## Authentication

Every read endpoint needs a bearer token; without one the API answers
`401 {"message": "No authorization token found"}`. Two ways to supply it:

1. **Static token** — set `APEIRON_ACCESS_TOKEN`. Mint one with the login
   endpoint:

   ```bash
   curl -s https://api.apeiron.life/people/login \
     -H 'Content-Type: application/json' \
     -d '{"email":"you@example.com","password":"secret","app":"dashboard"}'
   # -> {"token": "...", "person": {...}}
   ```

2. **Email + password** — set `APEIRON_EMAIL` and `APEIRON_PASSWORD`; the client
   calls `POST /people/login` on first use, caches the token, and re-authenticates
   once if a call comes back `401`. To print a token for `APEIRON_ACCESS_TOKEN`:

   ```bash
   python -c "from apeiron_mcp.apeiron_client import fetch_token; \
              print(fetch_token('you@example.com', 'secret'))"
   ```

## Endpoint mapping

Swagger spec: <https://api.apeiron.life/_api.json> (v13.0). The server only uses
read (`GET`) endpoints; the validated dataset types are `bio_marker`,
`body_comp`, `cardiorespiratory`, `cognitive_health`, `lifestyle_assessment` and
`physical_assessment`.

| Tool | Request | Date handling |
|------|---------|---------------|
| `get_sleep_data` | `GET /activity_feed/sleep-analytics?person_id&from&to` | native (defaults to the last 7 days) |
| | `GET /activity_feed/sleep-analytics-day?person_id&day` | native, when `start_date == end_date` |
| `get_exercise_data` | `GET /activity_feed/exercise-analytics?person_id&from&to` | native; `activity_type` filtered locally |
| `get_nutrition_data` | `GET /nutrition_score/?person_id` and `GET /nutrition_score/details?person_id&start_date&end_date` | native |
| `get_cardio_metrics` | `GET /datasets/cardiorespiratory?person_id` | filtered locally on `test_date` |
| `get_fitness_assessment` | `GET /datasets/{type}?person_id` | filtered locally on `test_date` |
| `get_cognitive_data` | `GET /datasets/cognitive_health?person_id` | filtered locally on `test_date` |
| `get_healthspan_domain_summary` | `GET /health_status/?person_id` and `GET /healthy_signals/?person_id&range_type=week&from&to` | native for healthy signals |
| `get_lifestyle_summary` | `GET /datasets/lifestyle_assessment?person_id` and `GET /summary_card/?person_id&from&to` | native for the summary card |
| `get_trends` | `GET /trend_statistics/?person_id&metric_keys&range_type&from&to` | native; `granularity` → `range_type` (`day`→`custom`, `week`→`week`, `month`→`month`) |
| `get_notes` | `GET /note/?client_id&start&end` | native; `author` filtered locally |
| `get_chat_history` | `GET /message/?client_id` | filtered and trimmed locally (`limit`) |

### Filters applied locally

Some tool arguments have no counterpart in the API, so they are applied to the
response instead. When a filter matches nothing, the full response is returned
and an entry is added to the envelope's `notes` field, so results are never
silently emptied:

* `get_exercise_data.activity_type` — matched against activity-name fields
  (`activity_type_shown`, `name`, `workout`, …).
* `get_cardio_metrics.metric` — `resting_hr`, `hrv`, `vo2max`, `blood_pressure`
  and `aerobic_capacity` are matched against metric/group names (so `hrv` also
  matches "heart rate variability").
* `get_notes.author` — matched against author/role/source fields.
* Date bounds on `/datasets/{type}` (which takes no date parameter) and on
  `/message/`.
* `get_chat_history.limit` — the most recent N messages are returned.
* `get_trends` retries without `metric_keys` when the API rejects
  `"<domain>.<metric>"`, returning every trend metric for the window.
* `get_chat_history.conversation_id` is ignored: Apeiron keeps a single message
  thread per client.

Everything else (`person_id`, `group`, dates on range-aware endpoints) is sent to
the API as query parameters.

## Development

```bash
pip install -e .
python examples/poc_walkthrough.py                        # all 11 tools, stub data
APEIRON_PERSON_ID=123 python examples/poc_walkthrough.py  # same calls, live data
```

The walkthrough prints the envelope's `source` and `notes` for every call, so it
doubles as a wiring check: without credentials everything reports
`"source": "stub"`; with credentials the same calls hit `api.apeiron.life`. Note
that the MCP stdio client only forwards a safe subset of the environment
(`HOME`, `PATH`, …), which is why the examples pass the `APEIRON_*` variables to
the server process explicitly.

API failures (bad token, unknown `person_id`, transport error) surface as MCP
tool errors carrying the failing method, path and HTTP status.

