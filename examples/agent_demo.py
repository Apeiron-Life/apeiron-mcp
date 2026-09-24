"""
Minimal LLM agent that uses the apeiron-mcp server as its toolset.

Supports Anthropic (Claude) or OpenAI. Set exactly one env var:
    ANTHROPIC_API_KEY=...   # uses claude-3-5-sonnet
    OPENAI_API_KEY=...      # uses gpt-4o-mini

Run (with the project virtualenv, so `apeiron-mcp` and `mcp` are importable):

    .venv/bin/pip install anthropic openai
    .venv/bin/python examples/agent_demo.py "How is client 123 doing this week?"

Set `APEIRON_PERSON_ID` for the default prompt. Apeiron credentials are read
from the environment or a local `.env` (see the README); without them the tools
answer from labelled stub data.

You'll see:
    • The agent's tool choices (name + arguments)
    • The raw JSON returned by each MCP tool
    • The final natural-language answer
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from apeiron_mcp.apeiron_client import load_env_file

SERVER_CMD = str(Path(sys.executable).with_name("apeiron-mcp"))


def server_env() -> dict[str, str]:
    """Apeiron settings to forward to the server process.

    The stdio client only passes a safe subset of the environment (HOME, PATH,
    ...), so credentials have to be handed over explicitly. A local ``.env`` is
    honoured as well, exactly like ``docker compose`` does.
    """
    load_env_file()
    return {key: value for key, value in os.environ.items() if key.startswith("APEIRON_")}


def require_server() -> None:
    """Fail with a readable message when the MCP entrypoint is not installed."""
    if not Path(SERVER_CMD).exists():
        raise SystemExit(
            f"Could not find the apeiron-mcp executable at {SERVER_CMD}.\n"
            "Install the package first:  pip install -e ."
        )


def select_provider() -> str:
    """Pick the LLM provider, failing fast before the server is spawned.

    Called outside the asyncio task group on purpose: raising inside the stdio
    client's context managers buries the message in a BaseExceptionGroup.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    raise SystemExit(
        "Set ANTHROPIC_API_KEY or OPENAI_API_KEY first — the agent needs an LLM "
        "to choose and call the MCP tools."
    )

BOLD, DIM, CYAN, YELLOW, GREEN, RESET = (
    "\033[1m", "\033[2m", "\033[36m", "\033[33m", "\033[32m", "\033[0m",
)


# --------------------------------------------------------------------------- #
# Schema helpers
# --------------------------------------------------------------------------- #

async def mcp_tools_to_openai(session: ClientSession) -> list[dict]:
    tools = (await session.list_tools()).tools
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema,
            },
        }
        for t in tools
    ]


async def mcp_tools_to_anthropic(session: ClientSession) -> list[dict]:
    tools = (await session.list_tools()).tools
    return [
        {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
        for t in tools
    ]


def print_tool_call(name: str, args: dict[str, Any]) -> None:
    print(f"\n{CYAN}▸ tool call:{RESET} {BOLD}{name}{RESET}({json.dumps(args)})")


def print_tool_result(text: str) -> None:
    try:
        parsed = json.loads(text)
        compact = {
            "source": parsed.get("source"),
            "domain": parsed.get("domain"),
            "notes": parsed.get("notes"),
            "data": parsed.get("data"),
        }
        if compact["notes"] is None:
            compact.pop("notes")
        text = json.dumps(compact, indent=2)
    except Exception:
        pass
    print(DIM + text + RESET)


async def call_mcp_tool(session: ClientSession, name: str, args: dict[str, Any]) -> str:
    result = await session.call_tool(name, args)
    return result.content[0].text if result.content else "{}"


# --------------------------------------------------------------------------- #
# Anthropic agent loop
# --------------------------------------------------------------------------- #

async def run_anthropic(session: ClientSession, user_msg: str) -> None:
    from anthropic import Anthropic

    client = Anthropic()
    tools = await mcp_tools_to_anthropic(session)
    messages: list[dict] = [{"role": "user", "content": user_msg}]

    while True:
        resp = client.messages.create(
            model="claude-3-5-sonnet-latest",
            max_tokens=1024,
            tools=tools,
            messages=messages,
            system=(
                "You are a health-data assistant. Use the provided tools to answer "
                "the user's question about a client's health. Be concise."
            ),
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    print(f"\n{GREEN}{BOLD}Final answer:{RESET}\n{block.text}")
            return

        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                print_tool_call(block.name, block.input)
                text = await call_mcp_tool(session, block.name, block.input)
                print_tool_result(text)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": text,
                })
        messages.append({"role": "user", "content": tool_results})


# --------------------------------------------------------------------------- #
# OpenAI agent loop
# --------------------------------------------------------------------------- #

async def run_openai(session: ClientSession, user_msg: str) -> None:
    from openai import OpenAI

    client = OpenAI()
    tools = await mcp_tools_to_openai(session)
    messages: list[dict] = [
        {"role": "system", "content": "You are a health-data assistant. Use tools; be concise."},
        {"role": "user", "content": user_msg},
    ]

    while True:
        resp = client.chat.completions.create(
            model="gpt-4o-mini", messages=messages, tools=tools,
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            print(f"\n{GREEN}{BOLD}Final answer:{RESET}\n{msg.content}")
            return

        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            print_tool_call(tc.function.name, args)
            text = await call_mcp_tool(session, tc.function.name, args)
            print_tool_result(text)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

async def main(provider: str) -> None:
    user_msg = " ".join(sys.argv[1:]) or (
        f"For client {os.getenv('APEIRON_PERSON_ID', '1')}, review last week: "
        "summarise sleep, exercise, and HRV, then say whether the healthspan "
        "summary agrees."
    )
    print(f"{YELLOW}User:{RESET} {user_msg}")
    print(f"{DIM}Provider: {provider}{RESET}")

    params = StdioServerParameters(command=SERVER_CMD, args=[], env=server_env())
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            if provider == "anthropic":
                await run_anthropic(session, user_msg)
            else:
                await run_openai(session, user_msg)


if __name__ == "__main__":
    # Validate everything that can fail cheaply *before* the event loop starts and
    # the server subprocess is spawned, so errors stay readable.
    require_server()
    asyncio.run(main(select_provider()))
