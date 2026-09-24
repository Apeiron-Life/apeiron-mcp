"""
POC walkthrough of the apeiron-mcp server.

Simulates an AI assistant reviewing a client's health data end-to-end:
  1. Discovers tools available on the server.
  2. Pulls last-week sleep, exercise, nutrition.
  3. Checks cardio (HRV) + a body-composition assessment.
  4. Fetches the rolled-up healthspan summary and lifestyle adherence.
  5. Reads latest clinician note and chat.

Run:
    .venv/bin/python examples/poc_walkthrough.py
    APEIRON_PERSON_ID=123 .venv/bin/python examples/poc_walkthrough.py

With no Apeiron credentials the tools answer from the built-in stub data
(envelope ``"source": "stub"``); with credentials the same run pulls live data
from api.apeiron.life.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from apeiron_mcp.apeiron_client import load_env_file

SERVER_CMD = str(Path(sys.executable).with_name("apeiron-mcp"))

#: Apeiron person id to query. Override with APEIRON_PERSON_ID when you have
#: credentials configured; without them the tools return stub data.
CLIENT_ID = os.getenv("APEIRON_PERSON_ID", "1")


def server_env() -> dict[str, str]:
    """Apeiron settings to forward to the server process.

    The stdio client only passes a safe subset of the environment (HOME, PATH,
    ...), so credentials have to be handed over explicitly. A local ``.env`` is
    honoured as well, exactly like ``docker compose`` does.
    """
    load_env_file()
    return {key: value for key, value in os.environ.items() if key.startswith("APEIRON_")}

# ANSI colors for a nicer demo
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


def banner(title: str) -> None:
    print(f"\n{BOLD}{CYAN}━━━ {title} ━━━{RESET}")


def show(label: str, payload) -> None:
    print(f"{YELLOW}▸ {label}{RESET}")
    text = payload.content[0].text if payload.content else "{}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Tool errors (bad token, unknown person id, ...) come back as plain text.
        print(DIM + " ".join(text.split())[:400] + RESET)
        return
    # keep the demo compact: show where the data came from + the data body only
    compact = {
        "source": parsed.get("source"),
        "domain": parsed.get("domain"),
        "notes": parsed.get("notes"),
        "data": parsed.get("data"),
    }
    if compact["notes"] is None:
        compact.pop("notes")
    print(DIM + json.dumps(compact, indent=2) + RESET)


async def main() -> None:
    end = date.today()
    start = end - timedelta(days=7)
    period = {"client_id": CLIENT_ID, "start_date": start.isoformat(), "end_date": end.isoformat()}

    params = StdioServerParameters(command=SERVER_CMD, args=[], env=server_env())
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            banner("1. Discover tools")
            tools = await session.list_tools()
            print(f"{GREEN}Server exposes {len(tools.tools)} tools:{RESET}")
            for t in tools.tools:
                print(f"  • {BOLD}{t.name}{RESET} — {(t.description or '').splitlines()[0]}")

            banner(f"2. Weekly vitals for client {CLIENT_ID}")
            show("Sleep",     await session.call_tool("get_sleep_data", period))
            show("Exercise",  await session.call_tool("get_exercise_data", period))
            show("Nutrition", await session.call_tool("get_nutrition_data", period))

            banner("3. Cardio + fitness assessment (parameterized tools)")
            show("HRV only",
                 await session.call_tool("get_cardio_metrics", {**period, "metric": "hrv"}))
            show("Body composition",
                 await session.call_tool("get_fitness_assessment",
                                         {**period, "assessment_type": "body_composition"}))

            banner("4. Derived rollups")
            show("Healthspan domain summary",
                 await session.call_tool("get_healthspan_domain_summary", {"client_id": CLIENT_ID}))
            show("Lifestyle summary (week)",
                 await session.call_tool("get_lifestyle_summary",
                                         {"client_id": CLIENT_ID, "period": "week"}))

            banner("5. Trend + unstructured data")
            show("HRV trend (daily)",
                 await session.call_tool("get_trends", {
                     **period, "domain": "cardio", "metric": "hrv", "granularity": "day",
                 }))
            show("Clinician notes",
                 await session.call_tool("get_notes", {**period, "author": "clinician"}))
            show("Chat history (limit 2)",
                 await session.call_tool("get_chat_history", {"client_id": CLIENT_ID, "limit": 2}))

            live = bool(os.getenv("APEIRON_ACCESS_TOKEN") or os.getenv("APEIRON_EMAIL"))
            endpoint = "api.apeiron.life" if live else "stub data (no credentials configured)"
            print(f"\n{GREEN}{BOLD}✓ POC complete — {len(tools.tools)} tools exercised against {endpoint}.{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
