"""LLM-powered cynical psychological profiler.

Sends behavioural metadata to an OpenAI-compatible ``/v1/chat/completions``
endpoint and returns a Rank Title + 2-sentence diagnosis.  Falls back to
rule-based heuristics when the LLM is unavailable or un-configured.
"""

from __future__ import annotations

import json
import logging

import httpx

from config import get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a cold, elite, slightly cynical socio-psychologist specialising in "
    "digital group dynamics analysis.  You analyse behavioural metadata from "
    "Telegram groups — not message content, only interaction patterns.\n\n"
    "Given a subject's behavioural metrics you must provide:\n"
    "1. A **Rank Title**: A sharp, evocative 2-3 word title that captures their "
    "social archetype.  Examples: \"Shadow Puppet\", \"Ego Leech\", \"Apex Voice\", "
    "\"Background Static\", \"Desperate Echo\", \"Silent Sovereign\", "
    "\"Phantom Orbiter\", \"Attention Parasite\".\n"
    "2. A **Diagnosis**: Exactly 2 brutal, razor-sharp sentences explaining WHY "
    "this person behaves this way in this group, based solely on the data.\n\n"
    'Respond ONLY with valid JSON: {"rank_title": "...", "diagnosis": "..."}'
)


async def generate_profile(user_metrics: dict) -> dict:
    """Produce a psychological profile for a single user.

    Parameters
    ----------
    user_metrics : dict
        The output of :func:`engine.graph.get_user_metrics`.

    Returns
    -------
    dict
        ``{"rank_title": str, "diagnosis": str}``
    """
    settings = get_settings()

    if not settings.LLM_API_KEY:
        logger.info("No LLM_API_KEY configured – using heuristic profiler")
        return _heuristic_profile(user_metrics)

    user_prompt = (
        "Analyse this Telegram group member:\n"
        f"{json.dumps(user_metrics, indent=2, default=str)}\n\n"
        "Provide your Rank Title and Diagnosis."
    )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.LLM_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.9,
                    "max_tokens": 256,
                },
            )
            resp.raise_for_status()

        data = resp.json()
        content: str = data["choices"][0]["message"]["content"].strip()

        # Strip markdown fences if the LLM wrapped the JSON
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        parsed = json.loads(content)
        return {
            "rank_title": parsed.get("rank_title", "Unknown Entity"),
            "diagnosis": parsed.get("diagnosis", "Insufficient behavioural data for analysis."),
        }
    except Exception as exc:
        logger.error("LLM profiling failed: %s", exc)
        return _heuristic_profile(user_metrics)


# ---------------------------------------------------------------------------
# Heuristic fallback
# ---------------------------------------------------------------------------

def _heuristic_profile(metrics: dict) -> dict:
    """Deterministic rule-based profile when no LLM is available."""
    influence = metrics.get("influence_score", 0.0)
    neglected = metrics.get("neglected_rate", 0.0)
    label = metrics.get("gravity_label", "Balanced")
    out_degree = metrics.get("out_degree", 0)
    in_degree = metrics.get("in_degree", 0)

    if influence >= 7.0:
        return {
            "rank_title": "Apex Voice",
            "diagnosis": (
                "Commands attention effortlessly.  The group orbits around their "
                "presence whether they intend it or not."
            ),
        }

    if neglected >= 0.7 and out_degree > 5:
        return {
            "rank_title": "Desperate Echo",
            "diagnosis": (
                "Speaks into the void with alarming persistence.  The group has "
                "collectively decided they are background noise."
            ),
        }

    if label == "Gravitational" and influence >= 4.0:
        return {
            "rank_title": "Silent Sovereign",
            "diagnosis": (
                "Rarely initiates but always receives.  They have trained this "
                "group to come to them."
            ),
        }

    if label == "Desperate":
        return {
            "rank_title": "Phantom Orbiter",
            "diagnosis": (
                "Circles the periphery hoping for acknowledgment.  Their engagement "
                "pattern suggests deep investment with minimal return."
            ),
        }

    if out_degree == 0 and in_degree == 0:
        return {
            "rank_title": "Background Static",
            "diagnosis": (
                "Exists in the group roster but contributes nothing measurable.  "
                "A digital ghost."
            ),
        }

    if influence >= 3.0:
        return {
            "rank_title": "Mid-Tier Operator",
            "diagnosis": (
                "Neither remarkable nor invisible.  Maintains presence through "
                "consistent but unremarkable participation."
            ),
        }

    return {
        "rank_title": "Unclassified Subject",
        "diagnosis": (
            "Insufficient behavioural patterns to form a conclusive profile.  "
            "Either new or deliberately opaque."
        ),
    }
