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
    "Ты — холодный, элитный и слегка циничный социальный психолог, специализирующийся "
    "на анализе цифровой динамики групп. Ты анализируешь поведенческие метаданные из "
    "Telegram-групп — не текст сообщений, а только паттерны взаимодействия.\n\n"
    "По метрикам участника нужно дать:\n"
    "1. **Название ранга**: короткий, яркий и запоминающийся титул на 2–3 слова, "
    "отражающий социальный архетип. Примеры: \"Тень Паука\", \"Паразит Внимания\", "
    "\"Голос Пика\", \"Фоновый Шум\", \"Отчаянный Эхо\", \"Молчаливый Правитель\", "
    "\"Фантомный Окружённый\", \"Хищник Внимания\".\n"
    "2. **Диагноз**: ровно 2 жёстких, острых предложения, объясняющих, почему "
    "человек ведёт себя так в группе, основываясь только на данных.\n\n"
    'Отвечай ТОЛЬКО валидным JSON: {"rank_title": "...", "diagnosis": "..."}'
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
        proxy = settings.PROXY_URL or None
        async with httpx.AsyncClient(proxy=proxy, timeout=30.0) as client:
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
            "rank_title": "Голос Пика",
            "diagnosis": (
                "Легко перехватывает внимание.  Группа вращается вокруг их присутствия, "
                "независимо от того, хотят они этого или нет."
            ),
        }

    if neglected >= 0.7 and out_degree > 5:
        return {
            "rank_title": "Отчаянное Эхо",
            "diagnosis": (
                "Говорит в пустоту с тревожной настойчивостью.  Группа в целом решила, "
                "что их внимание — это просто фоновый шум."
            ),
        }

    if label == "Gravitational" and influence >= 4.0:
        return {
            "rank_title": "Молчаливый Правитель",
            "diagnosis": (
                "Редко инициирует, но почти всегда получает отклик.  Они научили группу "
                "приходить именно к ним."
            ),
        }

    if label == "Desperate":
        return {
            "rank_title": "Фантомный Окружённый",
            "diagnosis": (
                "Кружит по краю общения, надеясь на признание.  Их шаблон активности "
                "свидетельствует о глубокой вовлечённости при минимальном возврате."
            ),
        }

    if out_degree == 0 and in_degree == 0:
        return {
            "rank_title": "Фоновый Шум",
            "diagnosis": (
                "Присутствует в составе группы, но не оставляет измеримой следа.  "
                "Цифровой призрак."
            ),
        }

    if influence >= 3.0:
        return {
            "rank_title": "Середняк Оперативный",
            "diagnosis": (
                "Не яркий и не невидимый.  Поддерживает присутствие через устойчивое, но "
                "невыдающееся участие."
            ),
        }

    return {
        "rank_title": "Неклассифицированный",
        "diagnosis": (
            "Недостаточно поведенческих паттернов для точного профиля.  "
            "Либо участник новый, либо сознательно остаётся непроницаемым."
        ),
    }
