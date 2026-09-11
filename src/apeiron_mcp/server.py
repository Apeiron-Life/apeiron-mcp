"""
Apeiron MCP server — exposes client health data as MCP tools.

Design principles (see README):
  * Every tool takes a required ``client_id`` plus optional date bounds.
  * Consistent envelope on every response.
  * Consolidated by data-shape to keep LLM tool-selection clean:
      - continuous streams: sleep, exercise, nutrition
      - cardio/aerobic:     get_cardio_metrics (metric enum)
      - periodic clinical:  get_fitness_assessment (assessment_type enum)
      - cognitive:          separate (distinct schema)
      - derived rollups:    healthspan / lifestyle summaries
      - generic:            get_trends
      - unstructured:       notes, chat_history

Replace the ``_stub_*`` helpers with real backend calls when integrating.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("apeiron-health")


# --------------------------------------------------------------------------- #
# Response envelope helpers
# --------------------------------------------------------------------------- #

def _envelope(
    client_id: str,
    domain: str,
    data: Any,
    start: Optional[str] = None,
    end: Optional[str] = None,
    unit_system: str = "metric",
) -> dict[str, Any]:
    return {
        "client_id": client_id,
        "domain": domain,
        "period": {"start": start, "end": end},
        "data": data,
        "unit_system": unit_system,
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
    }


def _today_iso() -> str:
    return date.today().isoformat()


# --------------------------------------------------------------------------- #
# 1. Sleep
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_sleep_data(
    client_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Sleep stages, duration, efficiency, HRV during sleep, sleep score.

    Args:
        client_id: Client identifier.
        start_date: ISO date (YYYY-MM-DD), inclusive. Defaults to 7 days ago.
        end_date:   ISO date (YYYY-MM-DD), inclusive. Defaults to today.
    """
    # TODO: replace with real backend call
    sample = [{
        "date": end_date or _today_iso(),
        "total_sleep_minutes": 431,
        "efficiency_pct": 92.4,
        "stages": {"rem_min": 88, "deep_min": 74, "light_min": 245, "awake_min": 24},
        "hrv_ms": 58,
        "sleep_score": 84,
    }]
    return _envelope(client_id, "sleep", sample, start_date, end_date)


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
    """Workout logs — type, duration, calories, heart-rate zones, RPE.

    Args:
        client_id: Client identifier.
        start_date: ISO date, inclusive.
        end_date:   ISO date, inclusive.
        activity_type: Optional filter (e.g. ``"run"``, ``"strength"``).
    """
    sample = [{
        "date": end_date or _today_iso(),
        "activity_type": activity_type or "run",
        "duration_min": 42,
        "calories": 460,
        "avg_hr": 148,
        "hr_zones_min": {"z1": 4, "z2": 18, "z3": 15, "z4": 4, "z5": 1},
        "rpe": 6,
    }]
    return _envelope(client_id, "exercise", sample, start_date, end_date)


# --------------------------------------------------------------------------- #
# 3. Nutrition
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_nutrition_data(
    client_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Meals logged, macros, calories, hydration, supplements."""
    sample = [{
        "date": end_date or _today_iso(),
        "calories_kcal": 2180,
        "macros_g": {"protein": 148, "carbs": 220, "fat": 78, "fiber": 32},
        "hydration_ml": 2600,
        "supplements": ["vitamin_d", "omega_3", "magnesium"],
        "meals_logged": 4,
    }]
    return _envelope(client_id, "nutrition", sample, start_date, end_date)


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
    """Cardio + aerobic metrics (same physiological domain).

    Args:
        metric: If omitted, returns all supported metrics.
    """
    all_metrics = {
        "resting_hr":       {"value": 56, "unit": "bpm"},
        "hrv":              {"value": 62, "unit": "ms"},
        "vo2max":           {"value": 48.2, "unit": "ml/kg/min"},
        "blood_pressure":   {"systolic": 118, "diastolic": 76, "unit": "mmHg"},
        "aerobic_capacity": {"value": 11.4, "unit": "METs"},
    }
    data = {metric: all_metrics[metric]} if metric else all_metrics
    return _envelope(client_id, "cardio", data, start_date, end_date)


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
    """Periodic clinical/assessment-style readings.

    Covers bone density, body composition, balance, movement quality, and
    muscle strength — periodic (not continuous) measurements.
    """
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
    return _envelope(client_id, "fitness_assessment", data, start_date, end_date)


# --------------------------------------------------------------------------- #
# 6. Cognitive
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_cognitive_data(
    client_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Cognitive test scores (memory, reaction time, processing speed) with trends."""
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
    return _envelope(client_id, "cognitive", sample, start_date, end_date)


# --------------------------------------------------------------------------- #
# 7. Healthspan domain summary (derived aggregate)
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_healthspan_domain_summary(
    client_id: str,
    as_of_date: Optional[str] = None,
) -> dict[str, Any]:
    """Cross-domain rolled-up healthspan scores."""
    as_of = as_of_date or _today_iso()
    data = {
        "as_of": as_of,
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
    return _envelope(client_id, "healthspan_summary", data, as_of, as_of)


# --------------------------------------------------------------------------- #
# 8. Lifestyle summary (derived aggregate)
# --------------------------------------------------------------------------- #

LifestylePeriod = Literal["week", "month", "quarter"]


@mcp.tool()
def get_lifestyle_summary(
    client_id: str,
    period: LifestylePeriod = "week",
) -> dict[str, Any]:
    """Sleep/activity/nutrition adherence rollup for a period."""
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
    return _envelope(client_id, "lifestyle_summary", data)


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
    """Generic trend tool — time series with deltas/direction for any metric.

    Args:
        domain: e.g. ``"sleep"``, ``"cardio"``, ``"nutrition"``.
        metric: metric name within that domain (e.g. ``"hrv"``, ``"sleep_score"``).
        granularity: bucket size for the returned series.
    """
    # TODO: fetch real series; stub returns a flat placeholder.
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
    return _envelope(client_id, f"trends:{domain}.{metric}", data, start_date, end_date)


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
    """Free-text clinical/coach/client notes."""
    sample = [{
        "id": "note_001",
        "author": author or "clinician",
        "created_at": (end_date or _today_iso()) + "T10:15:00Z",
        "text": "Client reports improved sleep after reducing evening caffeine.",
        "tags": ["sleep", "caffeine"],
    }]
    return _envelope(client_id, "notes", sample, start_date, end_date)


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
    """Paginated coaching/chat conversation logs.

    Args:
        conversation_id: Restrict to one conversation thread.
        limit: Max messages to return (default 50).
    """
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
    return _envelope(client_id, "chat_history", sample, start_date, end_date)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
