"""
Apeiron MCP server — exposes client health data as MCP tools.

Data comes from the Apeiron backend (https://api.apeiron.life, Swagger v13.0).
Every tool is wired to a real read endpoint through
:mod:`apeiron_mcp.apeiron_client`; the endpoint map is documented in the README
and in each tool's docstring.

Design principles (see README):
  * Every tool takes a required ``client_id`` (= Apeiron ``person_id``) plus
    optional date bounds.
  * Consistent envelope on every response (``source`` tells the caller whether
    the payload came from the API or from the offline stub).
  * Consolidated by data-shape to keep LLM tool-selection clean:
      - continuous streams: sleep, exercise, nutrition
      - cardio/aerobic:     get_cardio_metrics (metric enum)
      - periodic clinical:  get_fitness_assessment (assessment_type enum)
      - cognitive:          separate (distinct schema)
      - derived rollups:    healthspan / lifestyle summaries
      - generic:            get_trends
      - unstructured:       notes, chat_history

When no credentials are configured the tools fall back to labelled stub data,
so the POC examples keep working without access to the backend.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Optional, Sequence

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import apeiron_client as apeiron

logger = logging.getLogger(__name__)

mcp = FastMCP("apeiron-health")

#: Transports accepted via ``MCP_TRANSPORT``. ``stdio`` is the default so the
#: bare ``apeiron-mcp`` command keeps working for local MCP clients.
STDIO_TRANSPORT = "stdio"
HTTP_TRANSPORTS = ("streamable-http", "sse")
VALID_TRANSPORTS = (STDIO_TRANSPORT, *HTTP_TRANSPORTS)

#: Hosts for which the MCP SDK already installs a sane DNS-rebinding allowlist.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


# --------------------------------------------------------------------------- #
# Response envelope
# --------------------------------------------------------------------------- #

def _envelope(
    client_id: str,
    domain: str,
    data: Any,
    start: Optional[str] = None,
    end: Optional[str] = None,
    unit_system: str = "metric",
    source: str = "apeiron-api",
    notes: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Wrap a tool result in the envelope every tool returns."""
    envelope: dict[str, Any] = {
        "client_id": client_id,
        "domain": domain,
        "period": {"start": start, "end": end},
        "data": data,
        "unit_system": unit_system,
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    if notes:
        envelope["notes"] = list(notes)
    return envelope


def _stub_envelope(
    client_id: str,
    domain: str,
    data: Any,
    start: Optional[str] = None,
    end: Optional[str] = None,
    notes: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Envelope for the offline fallback used when no credentials are set."""
    banner = "No Apeiron credentials configured — returning demo stub data."
    return _envelope(
        client_id,
        domain,
        data,
        start,
        end,
        source="stub",
        notes=[banner, *(notes or ())],
    )


def _today_iso() -> str:
    return date.today().isoformat()


# --------------------------------------------------------------------------- #
# Backend wiring (https://api.apeiron.life — endpoint map in the README)
# --------------------------------------------------------------------------- #

#: Default window applied when a tool is called without explicit bounds.
DEFAULT_WINDOW_DAYS = 7

#: Fields an activity name may live in, used for best-effort client filtering.
ACTIVITY_NAME_KEYS = (
    "activity_name",
    "activity_type",
    "activity_type_shown",
    "activity_type_group",
    "shift_activity_classification",
    "name",
    "workout",
    "title",
    "type",
)

#: Fields carrying a note's author/role, used for best-effort client filtering.
NOTE_AUTHOR_KEYS = ("author", "author_role", "role", "source", "created_by", "people")

#: ``get_notes`` author -> keywords searched in the returned notes.
NOTE_AUTHOR_ALIASES: dict[str, tuple[str, ...]] = {
    "clinician": ("clinician", "expert", "coach"),
    "client": ("client", "member"),
    "system": ("system", "auto"),
}

#: Fields a date may live in, used for best-effort client-side range filtering.
DATE_KEYS = ("test_date", "record_date", "date", "created_at", "sent_at", "updated_at")

#: Fields a dataset metric/group name may live in, used for best-effort filtering.
DATASET_NAME_KEYS = ("name", "group", "metric", "key", "category_name", "label", "title", "type")

#: ``assessment_type`` -> (dataset type, name keywords to filter the result on).
FITNESS_ASSESSMENT_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "bone_density": ("physical_assessment", ("bone",)),
    "body_composition": ("body_comp", ()),
    "balance": ("physical_assessment", ("balance",)),
    "movement": ("physical_assessment", ("movement", "mobility")),
    "muscle_strength": ("physical_assessment", ("strength", "muscle")),
}

#: Dataset types returned when no ``assessment_type`` is given.
FITNESS_ASSESSMENT_DEFAULT_TYPES = ("body_comp", "physical_assessment", "bio_marker")

#: ``get_cardio_metrics`` metric -> keywords searched in the returned datasets.
CARDIO_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "resting_hr": ("resting hr", "resting_hr", "resting heart rate", "resting"),
    "hrv": ("hrv", "heart rate variability"),
    "vo2max": ("vo2 max", "vo2max", "vo2_max"),
    "blood_pressure": ("blood pressure", "systolic", "diastolic"),
    "aerobic_capacity": ("aerobic capacity", "aerobic"),
}

#: ``get_lifestyle_summary`` period -> trailing window length in days.
LIFESTYLE_PERIOD_DAYS = {"week": 7, "month": 30, "quarter": 91}

#: ``get_trends`` granularity -> ``/trend_statistics/`` ``range_type`` value.
TREND_RANGE_TYPES = {"day": "custom", "week": "week", "month": "month"}


def _person_id(client_id: str) -> Any:
    """Apeiron addresses users by numeric ``person_id``."""
    text = str(client_id).strip()
    try:
        value = float(text)
    except ValueError:
        raise ToolError(
            f"client_id must be the numeric Apeiron person id, got {client_id!r}."
        ) from None
    return int(value) if value.is_integer() else value


def _call(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    """GET ``path`` from the Apeiron API, surfacing failures as ``ToolError``."""
    try:
        return apeiron.get(path, params)
    except (apeiron.ApeironAPIError, apeiron.ApeironAuthError) as exc:
        raise ToolError(f"Apeiron API error: {exc}") from exc
    except httpx.HTTPError as exc:  # pragma: no cover - defensive
        raise ToolError(f"Could not reach the Apeiron API: {exc}") from exc


def _parse_day(value: Optional[str]) -> Optional[date]:
    """Parse an ISO date (or datetime) string, returning ``None`` if unusable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _window(
    start_date: Optional[str],
    end_date: Optional[str],
    days: int = DEFAULT_WINDOW_DAYS,
) -> tuple[str, str]:
    """Resolve optional ISO bounds into an explicit ``(start, end)`` window."""
    end = _parse_day(end_date) or date.today()
    start = _parse_day(start_date) or (end - timedelta(days=max(days, 1) - 1))
    return start.isoformat(), end.isoformat()


def _period_window(period: str) -> tuple[str, str]:
    """Trailing window for a named lifestyle period (``week``/``month``/``quarter``)."""
    return _window(None, None, LIFESTYLE_PERIOD_DAYS.get(period, LIFESTYLE_PERIOD_DAYS["week"]))


def _iter_lists(node: Any) -> Iterable[list[Any]]:
    """Yield every list of dicts nested anywhere inside ``node``."""
    if isinstance(node, list):
        if node and all(isinstance(item, dict) for item in node):
            yield node
        for item in node:
            yield from _iter_lists(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_lists(value)


def _filter_items(data: Any, needles: Sequence[str], keys: Sequence[str]) -> tuple[Any, bool]:
    """Keep list items whose ``keys`` contain any of ``needles``.

    The API has no query parameter for some filters this server exposes, so the
    filter is applied to the response instead. The first list of dicts that
    matches wins; the second return value reports whether anything matched, so
    the caller can add a note rather than silently return a misleading subset.
    """
    lowered = [needle.casefold() for needle in needles if needle]

    def item_matches(item: dict[str, Any]) -> bool:
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and any(needle in value.casefold() for needle in lowered):
                return True
        return False

    for items in _iter_lists(data):
        if any(item_matches(item) for item in items):
            return [item for item in items if item_matches(item)], True
    return data, False


def _filter_by_date(data: Any, start: Optional[str], end: Optional[str]) -> tuple[Any, bool]:
    """Drop list items dated outside ``[start, end]`` (inclusive).

    Endpoints such as ``/datasets/{type}`` take no date range, so the bounds are
    applied locally: every list of dated dicts in the payload is filtered. Items
    whose date cannot be determined are kept, and the payload is returned
    untouched when neither bound parses or the bounds are inverted.
    """
    lo, hi = _parse_day(start), _parse_day(end)
    if lo is None and hi is None:
        return data, False
    if lo is not None and hi is not None and hi < lo:
        return data, False

    applied = False

    def item_day(item: dict[str, Any]) -> Optional[date]:
        for key in DATE_KEYS:
            if key in item:
                parsed = _parse_day(item[key])
                if parsed is not None:
                    return parsed
        return None

    def walk(node: Any) -> Any:
        nonlocal applied
        if isinstance(node, list):
            if node and all(isinstance(item, dict) for item in node):
                days = [item_day(item) for item in node]
                if any(day is not None for day in days):
                    applied = True
                    return [
                        item
                        for item, day in zip(node, days)
                        if day is None or (lo is None or day >= lo) and (hi is None or day <= hi)
                    ]
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        return node

    return walk(data), applied


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse an ISO timestamp or unix epoch (seconds/ms) into a naive UTC datetime."""
    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_dt(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        day = _parse_day(text)
        return datetime(day.year, day.month, day.day) if day else None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _trim_messages(data: Any, limit: int) -> tuple[Any, bool]:
    """Keep only the ``limit`` most recent items of the returned message thread.

    ``GET /message/`` has no ``limit`` parameter, so the slice is applied locally.
    Items are ordered oldest-first when their timestamps parse (so "most recent"
    is well defined) and otherwise kept in the order the API returned them.
    """
    if limit <= 0:
        return data, False

    trimmed = False

    def walk(node: Any) -> Any:
        nonlocal trimmed
        if isinstance(node, list):
            if node and all(isinstance(item, dict) for item in node):
                stamps = [
                    next((stamp for stamp in (_parse_dt(item.get(key)) for key in DATE_KEYS)
                          if stamp is not None), None)
                    for item in node
                ]
                ordered = node
                if all(stamp is not None for stamp in stamps):
                    ordered = [item for _, item in sorted(zip(stamps, node), key=lambda pair: pair[0])]
                if len(ordered) > limit:
                    trimmed = True
                    return ordered[-limit:]
                return ordered
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        return node

    return walk(data), trimmed


# --------------------------------------------------------------------------- #
# 1. Sleep
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_sleep_data(
    client_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Sleep analytics for a date window.

    Backed by ``GET /activity_feed/sleep-analytics`` (multi-day window) or
    ``GET /activity_feed/sleep-analytics-day`` (single day).

    Args:
        client_id: Apeiron ``person_id`` (numeric).
        start_date: ISO date (YYYY-MM-DD), inclusive. Defaults to 6 days ago.
        end_date:   ISO date (YYYY-MM-DD), inclusive. Defaults to today.
    """
    if not apeiron.is_configured():
        sample = [{
            "date": end_date or _today_iso(),
            "total_sleep_minutes": 431,
            "efficiency_pct": 92.4,
            "stages": {"rem_min": 88, "deep_min": 74, "light_min": 245, "awake_min": 24},
            "hrv_ms": 58,
            "sleep_score": 84,
        }]
        return _stub_envelope(client_id, "sleep", sample, start_date, end_date)

    start, end = _window(start_date, end_date)
    params: dict[str, Any] = {"person_id": _person_id(client_id)}
    if start == end:
        data = _call("/activity_feed/sleep-analytics-day", {**params, "day": start})
    else:
        data = _call("/activity_feed/sleep-analytics", {**params, "from": start, "to": end})
    return _envelope(client_id, "sleep", data, start, end)


# --------------------------------------------------------------------------- #
# 2. Exercise
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_exercise_data(
    client_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    activity_type: Optional[str] = None,
) -> dict[str, Any]:
    """Workout analytics for a date window.

    Backed by ``GET /activity_feed/exercise-analytics`` (multi-day window) or
    ``GET /activity_feed/exercise-analytics-day`` (single day). ``activity_type``
    has no API parameter, so it is applied to the returned payload as a
    best-effort, case-insensitive name filter and reported in ``notes`` when it
    could not be applied.

    Args:
        client_id: Apeiron ``person_id`` (numeric).
        start_date: ISO date (YYYY-MM-DD), inclusive. Defaults to 6 days ago.
        end_date:   ISO date (YYYY-MM-DD), inclusive. Defaults to today.
        activity_type: Optional filter (e.g. ``"run"``, ``"strength"``).
    """
    if not apeiron.is_configured():
        sample = [{
            "date": end_date or _today_iso(),
            "activity_type": activity_type or "run",
            "duration_min": 42,
            "calories": 460,
            "avg_hr": 148,
            "hr_zones_min": {"z1": 4, "z2": 18, "z3": 15, "z4": 4, "z5": 1},
            "rpe": 6,
        }]
        return _stub_envelope(client_id, "exercise", sample, start_date, end_date)

    start, end = _window(start_date, end_date)
    params: dict[str, Any] = {"person_id": _person_id(client_id)}
    if start == end:
        data = _call("/activity_feed/exercise-analytics-day", {**params, "day": start})
    else:
        data = _call("/activity_feed/exercise-analytics", {**params, "from": start, "to": end})

    notes: list[str] = []
    if activity_type:
        data, matched = _filter_items(data, (activity_type,), ACTIVITY_NAME_KEYS)
        if not matched:
            notes.append(
                f"No workout matching activity_type={activity_type!r} was found in the "
                "response; unfiltered analytics returned."
            )
    return _envelope(client_id, "exercise", data, start, end, notes=notes)


# --------------------------------------------------------------------------- #
# 3. Nutrition
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_nutrition_data(
    client_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Nutrition scores for a date window.

    Backed by ``GET /nutrition_score/`` (overall score) and
    ``GET /nutrition_score/details`` (per-question scores in the window).

    Args:
        client_id: Apeiron ``person_id`` (numeric).
        start_date: ISO date (YYYY-MM-DD), inclusive. Defaults to 6 days ago.
        end_date:   ISO date (YYYY-MM-DD), inclusive. Defaults to today.
    """
    if not apeiron.is_configured():
        sample = [{
            "date": end_date or _today_iso(),
            "calories_kcal": 2180,
            "macros_g": {"protein": 148, "carbs": 220, "fat": 78, "fiber": 32},
            "hydration_ml": 2600,
            "supplements": ["vitamin_d", "omega_3", "magnesium"],
            "meals_logged": 4,
        }]
        return _stub_envelope(client_id, "nutrition", sample, start_date, end_date)

    start, end = _window(start_date, end_date)
    person_id = _person_id(client_id)
    data = {
        "score": _call("/nutrition_score/", {"person_id": person_id}),
        "details": _call(
            "/nutrition_score/details",
            {"person_id": person_id, "start_date": start, "end_date": end},
        ),
    }
    return _envelope(client_id, "nutrition", data, start, end)


# --------------------------------------------------------------------------- #
# 4. Cardio + aerobic (merged)
# --------------------------------------------------------------------------- #

CardioMetric = Literal[
    "resting_hr", "hrv", "vo2max", "blood_pressure", "aerobic_capacity"
]


@mcp.tool()
def get_cardio_metrics(
    client_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    metric: Optional[CardioMetric] = None,
) -> dict[str, Any]:
    """Cardio + aerobic metrics from the ``cardiorespiratory`` datasets.

    Backed by ``GET /datasets/cardiorespiratory`` (``person_id``). The endpoint
    has no metric/date parameters, so ``metric`` and the date bounds are applied
    to the response as best-effort filters; ``notes`` says when they could not be.

    Args:
        client_id: Apeiron ``person_id`` (numeric).
        start_date: ISO date (YYYY-MM-DD), inclusive.
        end_date:   ISO date (YYYY-MM-DD), inclusive.
        metric: If omitted, returns every returned cardiorespiratory dataset.
    """
    if not apeiron.is_configured():
        all_metrics = {
            "resting_hr":       {"value": 56, "unit": "bpm"},
            "hrv":              {"value": 62, "unit": "ms"},
            "vo2max":           {"value": 48.2, "unit": "ml/kg/min"},
            "blood_pressure":   {"systolic": 118, "diastolic": 76, "unit": "mmHg"},
            "aerobic_capacity": {"value": 11.4, "unit": "METs"},
        }
        data = {metric: all_metrics[metric]} if metric else all_metrics
        return _stub_envelope(client_id, "cardio", data, start_date, end_date)

    data = _call("/datasets/cardiorespiratory", {"person_id": _person_id(client_id)})
    notes: list[str] = []

    if metric:
        data, matched = _filter_items(data, CARDIO_METRIC_ALIASES[metric], DATASET_NAME_KEYS)
        if not matched:
            notes.append(
                f"No entry matching metric={metric!r} was found in the cardiorespiratory "
                "datasets; unfiltered data returned."
            )

    if start_date or end_date:
        data, dated = _filter_by_date(data, start_date, end_date)
        if not dated:
            notes.append(
                "No dated items were found in the cardiorespiratory datasets, so "
                "start_date/end_date could not be applied."
            )

    return _envelope(client_id, "cardio", data, start_date, end_date, notes=notes)


# --------------------------------------------------------------------------- #
# 5. Fitness assessments (bone/body-comp/balance/movement/muscle)
# --------------------------------------------------------------------------- #

AssessmentType = Literal[
    "bone_density", "body_composition", "balance", "movement", "muscle_strength"
]


@mcp.tool()
def get_fitness_assessment(
    client_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    assessment_type: Optional[AssessmentType] = None,
) -> dict[str, Any]:
    """Periodic clinical/assessment-style datasets.

    Backed by ``GET /datasets/{type}`` where ``type`` is one of the assessment
    dataset types exposed by the API::

        assessment_type     dataset type
        ----------------    ----------------
        body_composition    body_comp
        bone_density        physical_assessment  (name filter: bone)
        balance             physical_assessment  (name filter: balance)
        movement            physical_assessment  (name filter: movement/mobility)
        muscle_strength     physical_assessment  (name filter: strength/muscle)

    With no ``assessment_type`` the body_comp, physical_assessment and bio_marker
    datasets are all returned. Dataset endpoints take no date range, so
    ``start_date``/``end_date`` are applied to the response where dates exist and
    reported in ``notes`` when they do not.
    """
    if not apeiron.is_configured():
        all_assessments = {
            "bone_density":     {"t_score": -0.4, "z_score": 0.1, "site": "lumbar_spine"},
            "body_composition": {"fat_pct": 18.3, "lean_mass_kg": 62.1, "visceral_fat": 4},
            "balance":          {"single_leg_stance_sec": 42, "y_balance_composite": 92.5},
            "movement":         {"fms_score": 17, "asymmetries": ["hip_mobility_L"]},
            "muscle_strength":  {"grip_kg": 48, "leg_press_1rm_kg": 180, "handgrip_percentile": 78},
        }
        data = (
            {assessment_type: all_assessments[assessment_type]}
            if assessment_type
            else all_assessments
        )
        return _stub_envelope(client_id, "fitness_assessment", data, start_date, end_date)

    person_id = _person_id(client_id)
    notes: list[str] = []

    if assessment_type:
        dataset_type, keywords = FITNESS_ASSESSMENT_MAP[assessment_type]
        data: Any = _call(f"/datasets/{dataset_type}", {"person_id": person_id})
        if keywords:
            data, matched = _filter_items(data, keywords, DATASET_NAME_KEYS)
            if not matched:
                notes.append(
                    f"No {dataset_type} entry matching {assessment_type!r} was found; "
                    "unfiltered datasets returned."
                )
    else:
        data = {
            dataset_type: _call(f"/datasets/{dataset_type}", {"person_id": person_id})
            for dataset_type in FITNESS_ASSESSMENT_DEFAULT_TYPES
        }

    if start_date or end_date:
        data, dated = _filter_by_date(data, start_date, end_date)
        if not dated:
            notes.append(
                "No dated items were found in the assessment datasets, so "
                "start_date/end_date could not be applied."
            )

    return _envelope(client_id, "fitness_assessment", data, start_date, end_date, notes=notes)


# --------------------------------------------------------------------------- #
# 6. Cognitive
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_cognitive_data(
    client_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Cognitive assessment datasets with trends.

    Backed by ``GET /datasets/cognitive_health`` (``person_id``). Date bounds are
    applied to the response where dates exist and reported in ``notes`` otherwise.
    """
    if not apeiron.is_configured():
        sample = [{
            "date": end_date or _today_iso(),
            "battery": "cambridge_brain_sciences",
            "scores": {
                "memory": 112,
                "reaction_time_ms": 312,
                "processing_speed": 108,
                "attention": 105,
                "executive_function": 110,
            },
            "trend": "stable",
        }]
        return _stub_envelope(client_id, "cognitive", sample, start_date, end_date)

    data = _call("/datasets/cognitive_health", {"person_id": _person_id(client_id)})
    notes: list[str] = []
    if start_date or end_date:
        data, dated = _filter_by_date(data, start_date, end_date)
        if not dated:
            notes.append(
                "No dated items were found in the cognitive datasets, so "
                "start_date/end_date could not be applied."
            )
    return _envelope(client_id, "cognitive", data, start_date, end_date, notes=notes)


# --------------------------------------------------------------------------- #
# 7. Healthspan domain summary (derived aggregate)
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_healthspan_domain_summary(
    client_id: str,
    as_of_date: Optional[str] = None,
) -> dict[str, Any]:
    """Cross-domain rolled-up healthy-signal scores.

    Backed by ``GET /health_status/`` (per-domain results plus the person profile,
    including ``profile.healthy_signals_percentage_target``) and
    ``GET /healthy_signals/`` for the 7-day window ending on ``as_of_date``.

    Args:
        client_id: Apeiron ``person_id`` (numeric).
        as_of_date: ISO date (YYYY-MM-DD) the window ends on. Defaults to today.
    """
    if not apeiron.is_configured():
        data = {
            "as_of": as_of_date or _today_iso(),
            "overall_score": 82,
            "domains": {
                "cardio":     {"score": 85, "direction": "up"},
                "metabolic":  {"score": 78, "direction": "stable"},
                "cognitive":  {"score": 88, "direction": "up"},
                "musculo":    {"score": 80, "direction": "stable"},
                "sleep":      {"score": 84, "direction": "up"},
                "nutrition":  {"score": 76, "direction": "stable"},
            },
        }
        return _stub_envelope(client_id, "healthspan_summary", data, as_of_date, as_of_date)

    person_id = _person_id(client_id)
    start, end = _window(None, as_of_date)
    data = {
        "health_status": _call("/health_status/", {"person_id": person_id}),
        "healthy_signals": _call(
            "/healthy_signals/",
            {"person_id": person_id, "range_type": "week", "from": start, "to": end},
        ),
    }
    return _envelope(client_id, "healthspan_summary", data, start, end)


# --------------------------------------------------------------------------- #
# 8. Lifestyle summary (derived aggregate)
# --------------------------------------------------------------------------- #

LifestylePeriod = Literal["week", "month", "quarter"]


@mcp.tool()
def get_lifestyle_summary(
    client_id: str,
    period: LifestylePeriod = "week",
) -> dict[str, Any]:
    """Lifestyle adherence rollup for a trailing period.

    Backed by ``GET /datasets/lifestyle_assessment`` (assessment datasets) and
    ``GET /summary_card/`` (per-pillar ``nutrition``/``exercise``/``rejuvenation``
    entries with training load, RPE and enjoy score for the same window).

    Args:
        client_id: Apeiron ``person_id`` (numeric).
        period: Trailing window — ``week`` (7d), ``month`` (30d), ``quarter`` (91d).
    """
    if not apeiron.is_configured():
        data = {
            "period": period,
            "adherence_pct": {
                "sleep": 88,
                "exercise": 72,
                "nutrition": 65,
                "hydration": 91,
            },
            "habits_streak_days": {"meditation": 12, "steps_10k": 5},
            "notable_changes": ["exercise frequency +18% vs. prior period"],
        }
        return _stub_envelope(client_id, "lifestyle_summary", data)

    person_id = _person_id(client_id)
    start, end = _period_window(period)
    data = {
        "lifestyle_assessment": _call(
            "/datasets/lifestyle_assessment", {"person_id": person_id}
        ),
        "summary_card": _call(
            "/summary_card/", {"person_id": person_id, "from": start, "to": end}
        ),
    }
    return _envelope(client_id, "lifestyle_summary", data, start, end)


# --------------------------------------------------------------------------- #
# 9. Generic trends
# --------------------------------------------------------------------------- #

Granularity = Literal["day", "week", "month"]


@mcp.tool()
def get_trends(
    client_id: str,
    domain: str,
    metric: str,
    start_date: str,
    end_date: str,
    granularity: Granularity = "day",
) -> dict[str, Any]:
    """Generic trend statistics for any (domain, metric).

    Backed by ``GET /trend_statistics/`` with ``person_id``, ``range_type``
    (derived from ``granularity``) and the ``from``/``to`` window. The metric is
    requested as ``metric_keys="<domain>.<metric>"``; if the API rejects that key
    the call is retried without it and every trend metric in the window is
    returned, with a note explaining why.

    Args:
        domain: e.g. ``"sleep"``, ``"cardio"``, ``"nutrition"``.
        metric: metric name within that domain (e.g. ``"hrv"``, ``"sleep_score"``).
        granularity: bucket size for the returned series.
    """
    if not apeiron.is_configured():
        series = [
            {"bucket": start_date, "value": 60},
            {"bucket": end_date, "value": 63},
        ]
        data = {
            "domain": domain,
            "metric": metric,
            "granularity": granularity,
            "series": series,
            "delta": 3,
            "direction": "up",
        }
        return _stub_envelope(client_id, f"trends:{domain}.{metric}", data, start_date, end_date)

    start, end = _window(start_date, end_date)
    metric_key = f"{domain}.{metric}"
    params: dict[str, Any] = {
        "person_id": _person_id(client_id),
        "range_type": TREND_RANGE_TYPES[granularity],
        "from": start,
        "to": end,
        "metric_keys": metric_key,
    }

    notes: list[str] = []
    try:
        data = apeiron.get("/trend_statistics/", params)
    except apeiron.ApeironAPIError as exc:
        if exc.status_code != 400:
            raise ToolError(f"Apeiron API error: {exc}") from exc
        notes.append(
            f"Retried /trend_statistics/ without metric_keys={metric_key!r}; "
            f"every trend metric for the window is returned ({exc})."
        )
        params.pop("metric_keys")
        data = _call("/trend_statistics/", params)
    except apeiron.ApeironAuthError as exc:
        raise ToolError(f"Apeiron API error: {exc}") from exc

    return _envelope(client_id, f"trends:{domain}.{metric}", data, start, end, notes=notes)


# --------------------------------------------------------------------------- #
# 10. Notes
# --------------------------------------------------------------------------- #

NoteAuthor = Literal["clinician", "client", "system"]


@mcp.tool()
def get_notes(
    client_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    author: Optional[NoteAuthor] = None,
) -> dict[str, Any]:
    """Free-text clinical/coach/client notes for a client.

    Backed by ``GET /note/`` (``client_id``, ``start``, ``end``). The API has no
    author parameter, so ``author`` is applied to the response as a best-effort
    filter over author/role-like fields and reported in ``notes`` when it could
    not be applied.

    Args:
        client_id: Apeiron ``person_id`` (numeric).
        start_date: ISO date (YYYY-MM-DD) lower bound.
        end_date:   ISO date (YYYY-MM-DD) upper bound.
        author: Optional filter — ``clinician``, ``client`` or ``system``.
    """
    if not apeiron.is_configured():
        sample = [{
            "id": "note_001",
            "author": author or "clinician",
            "created_at": (end_date or _today_iso()) + "T10:15:00Z",
            "text": "Client reports improved sleep after reducing evening caffeine.",
            "tags": ["sleep", "caffeine"],
        }]
        return _stub_envelope(client_id, "notes", sample, start_date, end_date)

    data = _call(
        "/note/",
        {"client_id": _person_id(client_id), "start": start_date, "end": end_date},
    )

    notes: list[str] = []
    if author:
        data, matched = _filter_items(data, NOTE_AUTHOR_ALIASES[author], NOTE_AUTHOR_KEYS)
        if not matched:
            notes.append(
                f"No note matched author={author!r}; unfiltered notes returned."
            )
    return _envelope(client_id, "notes", data, start_date, end_date, notes=notes)


# --------------------------------------------------------------------------- #
# 11. Chat history (paginated)
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_chat_history(
    client_id: str,
    conversation_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Coaching chat messages for a client.

    Backed by ``GET /message/`` (``client_id``). Apeiron keeps a single message
    thread per client, so ``conversation_id`` is not an API concept and is
    reported as ignored. The endpoint takes no date or limit parameters, so
    ``start_date``/``end_date`` and ``limit`` are applied to the response — the
    most recent ``limit`` messages are returned in chronological order.

    Args:
        client_id: Apeiron ``person_id`` (numeric).
        conversation_id: Accepted for compatibility; ignored by the API.
        start_date: ISO date (YYYY-MM-DD) lower bound.
        end_date:   ISO date (YYYY-MM-DD) upper bound.
        limit: Max messages to return (default 50).
    """
    if not apeiron.is_configured():
        sample = [
            {
                "conversation_id": conversation_id or "conv_123",
                "message_id": "msg_001",
                "role": "client",
                "sent_at": (end_date or _today_iso()) + "T09:00:00Z",
                "text": "Feeling more energetic this week.",
            },
            {
                "conversation_id": conversation_id or "conv_123",
                "message_id": "msg_002",
                "role": "coach",
                "sent_at": (end_date or _today_iso()) + "T09:02:00Z",
                "text": "Great — let's keep the current sleep routine going.",
            },
        ][:limit]
        return _stub_envelope(client_id, "chat_history", sample, start_date, end_date)

    data = _call("/message/", {"client_id": _person_id(client_id)})
    notes: list[str] = []

    if conversation_id:
        notes.append(
            "Apeiron stores one message thread per client; conversation_id was ignored."
        )

    if start_date or end_date:
        data, dated = _filter_by_date(data, start_date, end_date)
        if not dated:
            notes.append(
                "No dated messages were found, so start_date/end_date could not be applied."
            )

    data, trimmed = _trim_messages(data, limit)
    if trimmed:
        notes.append(f"Trimmed to the most recent {limit} messages.")

    return _envelope(client_id, "chat_history", data, start_date, end_date, notes=notes)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def _env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean-ish environment variable (``1/true/yes/on``)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    """Parse a comma-separated environment variable into a clean list."""
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def _configure_http_transport() -> tuple[str, str, int]:
    """Apply the ``MCP_*`` env vars to the FastMCP HTTP settings.

    Returns ``(host, port)`` so the caller can log the endpoint.

    The MCP SDK builds its settings at construction time and pydantic-settings
    ranks init kwargs above env vars, so ``FASTMCP_HOST``/``FASTMCP_PORT`` are
    ignored here — the values must be assigned to ``mcp.settings`` explicitly.
    """
    host = os.getenv("MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"

    raw_port = os.getenv("MCP_PORT", "8000").strip()
    try:
        port = int(raw_port)
    except ValueError:
        raise SystemExit(f"MCP_PORT must be an integer, got {raw_port!r}") from None
    if not 1 <= port <= 65535:
        raise SystemExit(f"MCP_PORT must be in 1-65535, got {port}")

    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.stateless_http = _env_flag("MCP_STATELESS_HTTP", default=False)
    mcp.settings.json_response = _env_flag("MCP_JSON_RESPONSE", default=False)

    allowed_hosts = _env_list("MCP_ALLOWED_HOSTS")
    if allowed_hosts:
        # Opt-in DNS-rebinding protection. Patterns may use a trailing ":*" to
        # allow any port, e.g. "apeiron.example.com:*".
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=_env_list("MCP_ALLOWED_ORIGINS"),
        )
        logger.info(
            "DNS-rebinding protection enabled (allowed_hosts=%s, allowed_origins=%s)",
            allowed_hosts,
            _env_list("MCP_ALLOWED_ORIGINS") or "any",
        )
    elif host not in LOOPBACK_HOSTS:
        # Bound to a non-loopback interface (e.g. 0.0.0.0 inside a container).
        # The SDK's loopback-only allowlist would reject every request, so turn
        # the header check off and rely on the network boundary / reverse proxy.
        mcp.settings.transport_security = None
        logger.warning(
            "MCP_HOST=%s is not a loopback address: Host/Origin validation is "
            "disabled. Set MCP_ALLOWED_HOSTS to restrict accepted Host headers.",
            host,
        )

    return host, port


def main() -> None:
    """Run the MCP server, choosing the transport from the environment.

    Environment:
        MCP_TRANSPORT        ``stdio`` (default), ``streamable-http`` or ``sse``.
        MCP_HOST             HTTP bind address (default ``127.0.0.1``).
        MCP_PORT             HTTP bind port (default ``8000``).
        MCP_STATELESS_HTTP   ``true`` to run without session state (HTTP only).
        MCP_JSON_RESPONSE    ``true`` to reply with plain JSON instead of SSE.
        MCP_ALLOWED_HOSTS    Comma-separated ``Host`` header allowlist; setting
                             it enables DNS-rebinding protection (HTTP only).
        MCP_ALLOWED_ORIGINS  Comma-separated ``Origin`` header allowlist.

    Backend credentials are read by :mod:`apeiron_mcp.apeiron_client`
    (``APEIRON_BASE_URL``, ``APEIRON_ACCESS_TOKEN``, ``APEIRON_TIMEOUT``).
    """
    transport = os.getenv("MCP_TRANSPORT", STDIO_TRANSPORT).strip().lower() or STDIO_TRANSPORT
    if transport not in VALID_TRANSPORTS:
        raise SystemExit(
            f"MCP_TRANSPORT must be one of {', '.join(VALID_TRANSPORTS)}, got {transport!r}"
        )

    if transport == STDIO_TRANSPORT:
        # All logging goes to stderr, so stdout stays a clean JSON-RPC stream.
        logger.info("Starting apeiron-mcp over stdio")
        mcp.run(transport=STDIO_TRANSPORT)
        return

    host, port = _configure_http_transport()
    path = (
        mcp.settings.streamable_http_path
        if transport == "streamable-http"
        else mcp.settings.sse_path
    )
    logger.info("Starting apeiron-mcp: %s transport on http://%s:%d%s", transport, host, port, path)
    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
