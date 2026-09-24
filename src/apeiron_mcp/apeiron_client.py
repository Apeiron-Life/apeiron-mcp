"""
HTTP client for the Apeiron backend (https://api.apeiron.life).

The backend is described by the Swagger spec published at
``https://api.apeiron.life/_api.json`` (v13.0). The read endpoints used by
:mod:`apeiron_mcp.server` are listed in the README.

Configuration is via env vars (a local ``.env`` file is read at import for
``APEIRON_*`` keys — walking up from the working directory, exactly like
``docker compose`` does; real environment variables always win):

    APEIRON_BASE_URL       (default: https://api.apeiron.life)
    APEIRON_ACCESS_TOKEN   Bearer token for the API
    APEIRON_EMAIL          Login email   — used to mint a token when no static
    APEIRON_PASSWORD       Login password — bearer token is configured
    APEIRON_TIMEOUT        Request timeout in seconds (default: 30)

When neither ``APEIRON_ACCESS_TOKEN`` nor an email/password pair is set,
:func:`is_configured` returns ``False`` and the MCP tools fall back to stub
data, so the server stays usable for demos without credentials.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.apeiron.life"

#: ``POST /people/login`` accepts only ``ios`` or ``dashboard`` for ``app``.
LOGIN_APP = "dashboard"

_http_client: Optional[httpx.Client] = None
_http_client_lock = threading.Lock()

_login_token: Optional[str] = None
_login_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

def _flatten(detail: Any) -> str:
    """Flatten an error body into a short, single-line message."""
    if isinstance(detail, (dict, list)):
        try:
            text = json.dumps(detail)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            text = str(detail)
    else:
        text = str(detail)
    return " ".join(text.split())[:500]


class ApeironAPIError(RuntimeError):
    """The Apeiron API answered with a non-2xx status code."""

    def __init__(self, method: str, path: str, status_code: int, detail: Any) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{method} {path} failed with HTTP {status_code}: {_flatten(detail)}")


class ApeironAuthError(RuntimeError):
    """Credentials are missing or rejected, so the API cannot be called."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _base_url() -> str:
    return os.getenv("APEIRON_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _static_token() -> Optional[str]:
    token = os.getenv("APEIRON_ACCESS_TOKEN", "").strip()
    return token or None


def _credentials() -> Optional[tuple[str, str]]:
    email = os.getenv("APEIRON_EMAIL", "").strip()
    password = os.getenv("APEIRON_PASSWORD", "")
    if email and password:
        return email, password
    return None


def _timeout() -> float:
    try:
        return float(os.getenv("APEIRON_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def is_configured() -> bool:
    """True if we have credentials to talk to the real backend."""
    return bool(_static_token() or _credentials())


def reset() -> None:
    """Drop cached state (login token + HTTP client). Mainly for tests."""
    global _login_token, _http_client
    with _login_lock:
        _login_token = None
    with _http_client_lock:
        client, _http_client = _http_client, None
    if client is not None:
        client.close()

# --------------------------------------------------------------------------- #
# Local .env support
# --------------------------------------------------------------------------- #

#: Only these prefixes are read from a local ``.env``. ``MCP_*`` settings stay
#: compose-only so a stray ``.env`` cannot move a local run off stdio.
ENV_FILE_PREFIXES = ("APEIRON_",)

#: How many directories above the working directory to search for ``.env``.
_ENV_SEARCH_DEPTH = 5

_env_file_checked = False
_env_file_used: Optional[Path] = None


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv-style file (no shell expansion, surrounding quotes stripped)."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _env_file_candidates(explicit: Optional[str]) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    candidates: list[Path] = []
    here = Path.cwd().resolve()
    for depth, directory in enumerate([here, *here.parents]):
        if depth > _ENV_SEARCH_DEPTH:
            break
        candidates.append(directory / ".env")
    # Also the checkout root, so runs started from a sub-directory (examples/)
    # still find it.
    root = Path(__file__).resolve().parents[2] / ".env"
    if root not in candidates:
        candidates.append(root)
    return candidates


def load_env_file(
    path: Optional[str] = None,
    prefixes: tuple[str, ...] = ENV_FILE_PREFIXES,
) -> Optional[Path]:
    """Load ``APEIRON_*`` settings from a local ``.env`` file, if there is one.

    ``docker compose`` reads ``.env`` automatically; this gives a plain local run
    the same backend configuration. Existing environment variables always win and
    only ``prefixes`` are applied, so server/transport settings in the same file
    stay compose-only. Returns the file that was used, or ``None``.
    """
    global _env_file_checked, _env_file_used
    if path is None and _env_file_checked:
        return _env_file_used

    for candidate in _env_file_candidates(path):
        if not candidate.is_file():
            continue
        try:
            values = _parse_env_file(candidate)
        except OSError as exc:
            logger.warning("Could not read %s: %s", candidate, exc)
            continue

        applied = 0
        for key, value in values.items():
            if key.startswith(prefixes) and key not in os.environ:
                os.environ[key] = value
                applied += 1
        logger.info("Loaded %d Apeiron setting(s) from %s", applied, candidate)
        if path is None:
            _env_file_checked, _env_file_used = True, candidate
        return candidate

    if path is None:
        _env_file_checked = True
    return None


# Read the local `.env` once at import, so both the server and direct imports
# (e.g. the `fetch_token` one-liner) see the same backend configuration.
load_env_file()


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

def _http() -> httpx.Client:
    """Return a pooled, thread-safe HTTP client for the Apeiron API."""
    global _http_client
    if _http_client is None:
        with _http_client_lock:
            if _http_client is None:
                _http_client = httpx.Client(
                    base_url=_base_url(),
                    timeout=_timeout(),
                    headers={"Accept": "application/json"},
                )
                atexit.register(_http_client.close)
    return _http_client


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        # A few endpoints answer with a JSON document serialised as a string.
        return {"raw": response.text}


def fetch_token(email: str, password: str) -> str:
    """Exchange credentials for a bearer token (``POST /people/login``).

    Useful for bootstrapping ``APEIRON_ACCESS_TOKEN``:

        python -c "from apeiron_mcp.apeiron_client import fetch_token; \\
                   print(fetch_token('you@example.com', 'secret'))"
    """
    payload = _request(
        "POST",
        "/people/login",
        json_body={"email": email, "password": password, "app": LOGIN_APP},
        authenticated=False,
    )
    token = payload.get("token") if isinstance(payload, dict) else None
    if not token:
        raise ApeironAuthError("POST /people/login did not return a token.")
    return str(token)


def _bearer() -> str:
    """The bearer token to send, minting one from credentials if needed."""
    token = _static_token()
    if token:
        return token

    creds = _credentials()
    if creds is None:
        raise ApeironAuthError(
            "No Apeiron credentials configured: set APEIRON_ACCESS_TOKEN, or "
            "APEIRON_EMAIL + APEIRON_PASSWORD."
        )

    global _login_token
    with _login_lock:
        if _login_token is None:
            _login_token = fetch_token(*creds)
            logger.info("Authenticated with the Apeiron API as %s", creds[0])
        return _login_token


def _request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
    authenticated: bool = True,
    _retry: bool = True,
) -> Any:
    """Perform one API call and parse the JSON answer.

    ``params`` values that are ``None`` are dropped so callers can pass
    optional query parameters inline. A cached login token is refreshed once
    when the API answers ``401``.
    """
    headers: dict[str, str] = {}
    if authenticated:
        headers["Authorization"] = f"Bearer {_bearer()}"

    clean = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        response = _http().request(method, path, params=clean, json=json_body, headers=headers)
    except httpx.HTTPError as exc:
        raise ApeironAPIError(method, path, 0, f"transport error: {exc}") from exc

    if (
        response.status_code == 401
        and authenticated
        and _retry
        and _static_token() is None
    ):
        # The cached login token expired — authenticate again and retry once.
        global _login_token
        with _login_lock:
            _login_token = None
        return _request(
            method,
            path,
            params=params,
            json_body=json_body,
            authenticated=authenticated,
            _retry=False,
        )

    if response.status_code >= 400:
        raise ApeironAPIError(method, path, response.status_code, _decode(response))
    return _decode(response)


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #

def get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    """GET ``path`` on the Apeiron API and return the parsed JSON body."""
    return _request("GET", path, params=params)


def post(path: str, json_body: Any = None, params: Optional[dict[str, Any]] = None) -> Any:
    """POST ``path`` on the Apeiron API and return the parsed JSON body."""
    return _request("POST", path, params=params, json_body=json_body)

