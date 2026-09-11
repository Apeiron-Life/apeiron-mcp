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
apeiron-mcp            # runs the server over stdio
```

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

## Wiring to a real backend

The current implementation returns stub payloads. Replace the bodies of the
functions in `src/apeiron_mcp/server.py` with calls into your health data
backend (HTTP client, DB, etc.).
