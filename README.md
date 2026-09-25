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
# 8000 is taken by apeiron-ml's research-api on the shared EC2 -> publish on 8001.
docker run -d --name apeiron-mcp \
  -p 8001:8000 \
  -e APEIRON_ACCESS_TOKEN="$APEIRON_ACCESS_TOKEN" \
  --restart unless-stopped \
  apeiron-mcp:0.1.0
```

The endpoint is then `http://localhost:8001/mcp`. Verify it:

```bash
curl -i -X POST http://localhost:8001/mcp \
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
`MCP_HOST_PORT` (host side, default `8001`) and `MCP_PORT` (container side,
default `8000`). MCP publishes on `8001` because apeiron-ml's `research-api`
already binds `8000` on the shared EC2 instance.

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
| `MCP_HOST_PORT` | `8001` | **Compose only** — host port published by `docker-compose.yml` (maps to container `MCP_PORT`). Defaults to `8001` because `8000` is taken by `research-api` on the shared EC2. |

> **Security note:** binding to a non-loopback address (`0.0.0.0`, required in a
> container) disables the SDK's `Host`/`Origin` validation so the service is
> reachable — the server logs a warning when this happens. When you expose the
> service beyond a trusted network, set `MCP_ALLOWED_HOSTS` (e.g.
> `apeiron.example.com,apeiron.example.com:*` — list both forms, because entries
> are matched against the `Host` header including its port) and terminate TLS at
> a reverse proxy. The server itself ships no authentication layer, so do not
> publish the port directly to the internet.

## CI/CD

Two GitHub Actions workflows mirror the apeiron-ml pipeline: they build the image
in CI, push it to AWS ECR, and deploy a chosen tag to the staging/production host.

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| [`build_mcp.yml`](.github/workflows/build_mcp.yml) | push to any branch (or manual) | Builds `./Dockerfile` (target `runtime`) and pushes `phs/apeiron-mcp:<tag>` + `:latest` to ECR. |
| [`deploy_mcp.yml`](.github/workflows/deploy_mcp.yml) | manual (`workflow_dispatch`) | Pulls the requested tag onto the target host and (re)starts the `apeiron-mcp` container. |

### Image tags

| Source branch | Image tag | Git tag |
|---------------|-----------|---------|
| `feature/APLF-664_…` | `APLF-664` | — |
| `main` | next semantic version, e.g. `0.1.0` | `v0.1.0` (pushed back by CI) |
| `staging` | `stag-<version>`, e.g. `stag-0.1.0` | — |

Every build also updates `latest`.

### Required repository secrets

| Secret | Used by | Purpose |
|--------|---------|---------|
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | build | AWS credentials for the ECR login. |
| `AWS_ECR_URL` | build, deploy | Registry host, e.g. `<account-id>.dkr.ecr.us-west-2.amazonaws.com`. |
| `STAGING_SSH_PRIVATE_KEY` / `PRODUCTION_SSH_PRIVATE_KEY` | deploy | SSH key for the target host. |
| `STAGING_SERVER_HOSTNAME` / `PRODUCTION_SERVER_HOSTNAME` | deploy | EC2 hostname to deploy to. |

### Deploying

1. Provide a `.env.docker` in the home directory of the target host (the
   workflow runs `docker run --env-file .env.docker`). Start from the committed
   template and fill in the real values:

   ```bash
   cp .env.example ~/.env.docker   # then edit APEIRON_ACCESS_TOKEN etc.
   ```

   See [Configuration](#configuration) for every variable.
2. Run the **Deploy MCP** workflow and pick the image tag and the environment.
   MCP is published on host port `8001` and the container always listens on
   `8000`; `8000` is left alone because apeiron-ml's `research-api` uses it.
3. The workflow restarts the container and fails loudly (with the last 50 log
   lines) if it exits during startup.

The `ECR_REPOSITORY` (`phs/apeiron-mcp`), `AWS_REGION` (`us-west-2`),
`CONTAINER_NAME` (`apeiron-mcp`) and `HOST_PORT` (`8001`) values live in the
`env:` block at the top of each workflow, so a different registry, region,
container name or host port is a one-line change.

> **Exposing the service:** the apeiron-ml EC2 security group only opens ports
> `8000` and `22`, so `8001` is reachable on the host/VPC but **not** from the
> internet until an ingress rule for `8001` is added to the terraform security
> group (`apeiron-ml/terraform_utils/research{,_staging}_env/ec2.tf`). In the
> meantime reach it over an SSH/SSM tunnel or front it with a reverse proxy.

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

### LLM agent chat UI

`examples/agent_demo.py` puts a small browser chat UI in front of the server: an
LLM picks the MCP tools, every tool call is shown above the answer, and the reply
streams in as it is written. Start the server over streamable HTTP, set one LLM
key, then open <http://127.0.0.1:8080>:

```bash
MCP_TRANSPORT=streamable-http apeiron-mcp   # terminal 1
.venv/bin/pip install anthropic openai
OPENAI_API_KEY=... .venv/bin/python examples/agent_demo.py   # terminal 2 (or ANTHROPIC_API_KEY)
```

Failures (a bad API key, the server not running) are reported inline in the page
instead of as a stack trace. `MCP_URL` overrides the MCP endpoint and
`AGENT_HOST` / `AGENT_PORT` the UI bind address.

API failures (bad token, unknown `person_id`, transport error) surface as MCP
tool errors carrying the failing method, path and HTTP status.

