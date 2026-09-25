"""
Chat UI for an LLM agent that uses the apeiron-mcp server as its toolset.

Supports Anthropic (Claude) or OpenAI. Set exactly one env var:
    ANTHROPIC_API_KEY=...   # uses claude-3-5-sonnet
    OPENAI_API_KEY=...      # uses gpt-4o-mini

The MCP server is expected to already be running over streamable HTTP
(this demo does not spawn a stdio subprocess):

    MCP_TRANSPORT=streamable-http apeiron-mcp
    # listens on http://127.0.0.1:8000/mcp by default

Run (with the project virtualenv, so `mcp` is importable):

    .venv/bin/pip install anthropic openai
    .venv/bin/python examples/agent_demo.py

Then open http://127.0.0.1:8080. Type a prompt; the reply streams in as the
model writes it, with each MCP tool call shown above the answer.

Override the MCP endpoint with `MCP_URL` (default `http://localhost:8000/mcp`)
and the UI bind address with `AGENT_HOST` / `AGENT_PORT` (default
`127.0.0.1:8080`). Apeiron credentials belong on the already-running server
(environment or a local `.env`); without them the tools answer from labelled
stub data.
"""
from __future__ import annotations

import json
import os
import traceback
from typing import Any, AsyncIterator

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

DEFAULT_MCP_URL = "http://localhost:8000/mcp"
DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8080
SYSTEM_PROMPT = (
    "You are a health-data assistant. Use the provided tools to answer "
    "the user's question about a client's health. Be concise."
)
OPENAI_MODEL = "gpt-4o-mini"
ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"


def mcp_url() -> str:
    """Streamable HTTP endpoint of the already-running MCP server."""
    return os.getenv("MCP_URL", DEFAULT_MCP_URL).strip() or DEFAULT_MCP_URL


def select_provider() -> str | None:
    """Pick the LLM provider from the environment, or None if unset."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return None


def require_provider() -> str:
    provider = select_provider()
    if provider is None:
        raise RuntimeError(
            "Set ANTHROPIC_API_KEY or OPENAI_API_KEY — the agent needs an LLM "
            "to choose and call the MCP tools."
        )
    return provider


def _event(kind: str, **payload: Any) -> str:
    """One server-sent event. `kind` is status, token, tool, error, or done."""
    return f"data: {json.dumps({'type': kind, **payload}, default=str)}\n\n"


def _describe_error(exc: BaseException) -> str:
    """Flatten ExceptionGroup nesting (anyio TaskGroups) down to the real cause."""
    inner = getattr(exc, "exceptions", None)
    if inner:
        parts = [_describe_error(item) for item in inner]
        return " | ".join(part for part in parts if part) or type(exc).__name__
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _compact_tool_result(text: str) -> str:
    """Keep the UI readable: source, domain, notes, and the data body."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    compact = {
        "source": parsed.get("source"),
        "domain": parsed.get("domain"),
        "notes": parsed.get("notes"),
        "data": parsed.get("data"),
    }
    if compact["notes"] is None:
        compact.pop("notes")
    return json.dumps(compact, indent=2)


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


async def call_mcp_tool(session: ClientSession, name: str, args: dict[str, Any]) -> str:
    result = await session.call_tool(name, args)
    return result.content[0].text if result.content else "{}"


# --------------------------------------------------------------------------- #
# Streaming Anthropic agent loop (yields SSE frames for the browser)
# --------------------------------------------------------------------------- #

async def stream_anthropic(
    session: ClientSession, tools: list[dict], user_msg: str
) -> AsyncIterator[str]:
    """Claude tool-use loop, streaming the reply as the model writes it."""
    from anthropic import Anthropic

    client = Anthropic()
    messages: list[dict] = [{"role": "user", "content": user_msg}]

    while True:
        with client.messages.stream(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            tools=tools,
            messages=messages,
            system=SYSTEM_PROMPT,
        ) as stream:
            for text in stream.text_stream:
                yield _event("token", text=text)
            message = stream.get_final_message()

        messages.append({"role": "assistant", "content": message.content})
        if message.stop_reason != "tool_use":
            return

        tool_results = []
        for block in message.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            yield _event("tool", name=block.name, arguments=block.input)
            text = await call_mcp_tool(session, block.name, block.input)
            yield _event("tool", name=block.name, result=_compact_tool_result(text))
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": text,
            })
        messages.append({"role": "user", "content": tool_results})


# --------------------------------------------------------------------------- #
# Streaming OpenAI agent loop (yields SSE frames for the browser)
# --------------------------------------------------------------------------- #

async def stream_openai(
    session: ClientSession, tools: list[dict], user_msg: str
) -> AsyncIterator[str]:
    """gpt-4o-mini tool-use loop, reassembling streamed tool-call deltas."""
    from openai import OpenAI

    client = OpenAI()
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    while True:
        chunks = client.chat.completions.create(
            model=OPENAI_MODEL, messages=messages, tools=tools, stream=True,
        )
        content: list[str] = []
        pending: dict[int, dict[str, str]] = {}
        for chunk in chunks:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content.append(delta.content)
                yield _event("token", text=delta.content)
            for call in delta.tool_calls or []:
                slot = pending.setdefault(
                    call.index, {"id": "", "name": "", "arguments": ""}
                )
                if call.id:
                    slot["id"] = call.id
                if call.function:
                    slot["name"] = call.function.name or slot["name"]
                    slot["arguments"] += call.function.arguments or ""

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if pending:
            message["tool_calls"] = [
                {
                    "id": slot["id"],
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"]},
                }
                for slot in pending.values()
            ]
        messages.append(message)
        if not pending:
            return

        for slot in pending.values():
            args = json.loads(slot["arguments"] or "{}")
            yield _event("tool", name=slot["name"], arguments=args)
            text = await call_mcp_tool(session, slot["name"], args)
            yield _event("tool", name=slot["name"], result=_compact_tool_result(text))
            messages.append({"role": "tool", "tool_call_id": slot["id"], "content": text})


# --------------------------------------------------------------------------- #
# Chat UI (Starlette): the index page and the server-sent-events endpoint
# --------------------------------------------------------------------------- #

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Apeiron MCP agent</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff; --fg: #16181d; --muted: #5c636e; --border: #c9ccd4;
    --field: #ffffff; --user-bg: #e4e8ff; --user-fg: #16181d;
    --answer-bg: #f1f2f5; --answer-fg: #16181d;
    --tool-bg: #fff7e4; --tool-border: #e8cf94; --tool-fg: #453714;
    --error: #a11224;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #131519; --fg: #e9ebef; --muted: #a2a9b6; --border: #3a3f4a;
      --field: #1c1f25; --user-bg: #2a3450; --user-fg: #e9ebef;
      --answer-bg: #1c1f25; --answer-fg: #e9ebef;
      --tool-bg: #2a2415; --tool-border: #584a22; --tool-fg: #f2e2b6;
      --error: #ff9a9a;
    }
  }
  body {
    font: 15px/1.5 system-ui, sans-serif;
    margin: 0 auto; padding: 24px; max-width: 880px;
    background: var(--bg); color: var(--fg);
  }
  h1 { font-size: 18px; margin: 0 0 16px; }
  #log { display: flex; flex-direction: column; gap: 10px; margin-bottom: 18px; }
  .msg {
    padding: 10px 12px; border-radius: 10px;
    white-space: pre-wrap; overflow-wrap: anywhere; color: var(--fg);
  }
  .user { background: var(--user-bg); color: var(--user-fg); align-self: flex-end; max-width: 80%; }
  .answer { background: var(--answer-bg); color: var(--answer-fg); }
  .answer:empty::after { content: '…'; color: var(--muted); }
  .tool {
    font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
    background: var(--tool-bg); color: var(--tool-fg);
    border: 1px solid var(--tool-border);
    max-height: 320px; overflow: auto;
  }
  .status, .error { font-size: 12px; color: var(--muted); }
  .error { color: var(--error); }
  form { display: flex; gap: 8px; }
  input, button {
    font: inherit; background: var(--field); color: var(--fg);
    border: 1px solid var(--border); border-radius: 8px;
  }
  input { flex: 1; padding: 10px 12px; }
  button { padding: 10px 18px; cursor: pointer; }
  button:disabled { opacity: 0.6; cursor: default; }
  input:focus-visible, button:focus-visible { outline: 2px solid var(--muted); outline-offset: 1px; }
</style>
</head>
<body>
<h1>Apeiron MCP agent</h1>
<div id="log"></div>
<form id="form">
  <input id="prompt" autocomplete="off"
         value="For client 1, review last week: summarise sleep, exercise, and HRV, then say whether the healthspan summary agrees.">
  <button id="send">Send</button>
</form>
<script>
const log = document.getElementById('log');
const sendBtn = document.getElementById('send');

function add(cls, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  log.appendChild(div);
  return div;
}

async function send(prompt) {
  add('user', prompt);
  const answer = add('answer', '');
  const tools = new Map();
  const resp = await fetch('/chat?prompt=' + encodeURIComponent(prompt));
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).error || detail; } catch (e) {}
    add('error', detail);
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split('\\n\\n');
    buffer = frames.pop();
    for (const frame of frames) {
      const line = frame.split('\\n').find(l => l.startsWith('data: '));
      if (!line) continue;
      const ev = JSON.parse(line.slice(6));
      if (ev.type === 'token') { answer.textContent += ev.text; }
      else if (ev.type === 'tool' && ev.result !== undefined) {
        const card = tools.get(ev.name);
        if (card) card.textContent = ev.name + ' → ' + ev.result;
      }
      else if (ev.type === 'tool') {
        tools.set(ev.name, add('tool', ev.name + '(' + JSON.stringify(ev.arguments) + ') …'));
      }
      else if (ev.type === 'status') { add('status', ev.text); }
      else if (ev.type === 'error') { add('error', ev.text); }
    }
  }
}

document.getElementById('form').addEventListener('submit', (event) => {
  event.preventDefault();
  const input = document.getElementById('prompt');
  if (!input.value.trim()) return;
  sendBtn.disabled = true;
  send(input.value.trim()).finally(() => { sendBtn.disabled = false; });
});
</script>
</body>
</html>
"""


async def index(_request: Request) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


async def chat(request: Request) -> Response:
    """SSE stream for one agent turn: status/tool/token events, then done/error."""
    prompt = (request.query_params.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "Missing ?prompt= query parameter."}, status_code=400)
    try:
        provider = require_provider()
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return StreamingResponse(
        agent_events(provider, prompt),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

async def agent_events(provider: str, user_msg: str) -> AsyncIterator[str]:
    """Open one MCP session and stream the agent's work for a single prompt."""
    url = mcp_url()
    yield _event("status", text=f"Connecting to {url} …")
    try:
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if provider == "anthropic":
                    tools = await mcp_tools_to_anthropic(session)
                    loop = stream_anthropic(session, tools, user_msg)
                else:
                    tools = await mcp_tools_to_openai(session)
                    loop = stream_openai(session, tools, user_msg)
                yield _event("status", text=f"Provider {provider} · {len(tools)} MCP tools")
                async for frame in loop:
                    yield frame
    except Exception as exc:  # noqa: BLE001 — report failures to the browser, keep the UI up
        traceback.print_exc()  # full detail stays on the server console
        yield _event("error", text=_describe_error(exc))
        return
    yield _event("done")


def ui_address() -> tuple[str, int]:
    """Bind address for the chat UI (``AGENT_HOST`` / ``AGENT_PORT``)."""
    host = os.getenv("AGENT_HOST", DEFAULT_UI_HOST).strip() or DEFAULT_UI_HOST
    raw_port = os.getenv("AGENT_PORT", str(DEFAULT_UI_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        raise SystemExit(f"AGENT_PORT must be an integer, got {raw_port!r}")
    return host, port


def main() -> None:
    """Start the chat UI, validating the LLM key before binding the port."""
    provider = select_provider()
    if provider is None:
        raise SystemExit(
            "Set ANTHROPIC_API_KEY or OPENAI_API_KEY — the agent needs an LLM "
            "to choose and call the MCP tools."
        )
    host, port = ui_address()
    app = Starlette(routes=[Route("/", index), Route("/chat", chat)])
    print(f"MCP endpoint: {mcp_url()}")
    print(f"Provider:     {provider}")
    print(f"Chat UI:      http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
