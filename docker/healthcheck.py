"""Container healthcheck for apeiron-mcp.

Exits ``0`` when the server looks healthy and ``1`` otherwise:

* ``stdio`` transport -> always healthy (the parent process owns the pipes,
  so there is no port to probe; a running PID is the only signal).
* HTTP transports -> the configured port accepts TCP connections.

Reads the same ``MCP_*`` env vars as ``apeiron_mcp.server.main``, plus
``MCP_HEALTHCHECK_HOST`` (default ``127.0.0.1``) and
``MCP_HEALTHCHECK_TIMEOUT`` (default ``3`` seconds).
"""
from __future__ import annotations

import os
import socket
import sys

HTTP_TRANSPORTS = {"streamable-http", "sse"}


def main() -> int:
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower() or "stdio"
    if transport not in HTTP_TRANSPORTS:
        return 0

    host = os.getenv("MCP_HEALTHCHECK_HOST", "127.0.0.1").strip() or "127.0.0.1"

    raw_port = os.getenv("MCP_PORT", "8000").strip()
    try:
        port = int(raw_port)
    except ValueError:
        print(f"healthcheck: MCP_PORT is not an integer: {raw_port!r}", file=sys.stderr)
        return 1

    raw_timeout = os.getenv("MCP_HEALTHCHECK_TIMEOUT", "3").strip()
    try:
        timeout = float(raw_timeout)
    except ValueError:
        print(f"healthcheck: MCP_HEALTHCHECK_TIMEOUT is not a number: {raw_timeout!r}", file=sys.stderr)
        return 1

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return 0
    except OSError as exc:
        print(f"healthcheck: cannot reach {host}:{port} ({exc})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
