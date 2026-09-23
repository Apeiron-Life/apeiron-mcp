# syntax=docker/dockerfile:1
#
# apeiron-mcp — MCP server exposing client health data.
#
# The image defaults to the network-friendly `streamable-http` transport so it
# can be deployed as a long-running service. Set MCP_TRANSPORT=stdio (and run
# with `docker run -i`) to drive it from a local MCP client over stdin/stdout.

ARG PYTHON_VERSION=3.12

# --------------------------------------------------------------------------- #
# Stage 1 — build a self-contained virtualenv
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN python -m venv "$VIRTUAL_ENV" \
 && pip install --upgrade pip

WORKDIR /build

# Only the files needed to build the wheel, so source edits don't bust every
# layer (README.md is required by `readme = "README.md"` in pyproject.toml).
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install .

# Pre-compile bytecode so the container starts fast and never needs to write
# .pyc files into the (read-only for the runtime user) site-packages.
RUN python -m compileall -q "$VIRTUAL_ENV"/lib/python*/site-packages/apeiron_mcp

# --------------------------------------------------------------------------- #
# Stage 2 — minimal runtime image
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="apeiron-mcp" \
      org.opencontainers.image.description="MCP server exposing client health data (sleep, exercise, nutrition, assessments, notes, chat, trends, summaries)." \
      org.opencontainers.image.source="https://github.com/akash-gdve/apeiron-mcp" \
      org.opencontainers.image.licenses="Proprietary"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

# Non-root runtime user.
RUN groupadd --gid 10001 apeiron \
 && useradd --uid 10001 --gid 10001 --shell /usr/sbin/nologin --no-create-home apeiron

COPY --from=builder /opt/venv /opt/venv
COPY docker/healthcheck.py /usr/local/bin/healthcheck.py
RUN chmod 0755 /usr/local/bin/healthcheck.py

USER 10001:10001

EXPOSE 8000

# TCP probe for HTTP transports; always healthy for stdio (see the script).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "/usr/local/bin/healthcheck.py"]

ENTRYPOINT ["apeiron-mcp"]
