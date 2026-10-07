"""Social-graph analytics engine built on NetworkX.

Constructs directed weighted graphs from replies and reactions.
Calculates influence, gravitation, neglected nodes, pair dynamics,
and weekly purge anomalies.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import networkx as nx
from sqlalchemy import select

from database import Message, Reaction, get_session

logger = logging.getLogger(__name__)

_MIN_INTERACTIONS = 5


# ---------------------------------------------------------------------------
# Graph Construction
# ---------------------------------------------------------------------------

async def build_social_graph(
    chat_id: int,
    days: int = 30,
) -> tuple[nx.DiGraph, dict[int, str]]:
    """Build a directed weighted graph from reply + reaction data."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    async with get_session() as session:
        res_msgs = await session.execute(
            select(Message)
            .where(Message.chat_id == chat_id, Message.timestamp >= cutoff)
            .order_by(Message.timestamp)
        )
        messages: list[Message] = list(res_msgs.scalars().all())

        res_rxns = await session.execute(
            select(Reaction)
            .where(Reaction.chat_id == chat_id, Reaction.timestamp >= cutoff)
        )
        reactions: list[Reaction] = list(res_rxns.scalars().all())

    user_map: dict[int, str] = {}
    graph = nx.DiGraph()

    for msg in messages:
        uid = msg.user_id
        user_map.setdefault(uid, msg.username or f"id_{uid}")
        graph.add_node(uid)

    edge_data: dict[int, dict[int, dict]] = defaultdict(lambda: defaultdict(lambda: {"count": 0, "fast_count": 0}))
    user_msg_times: dict[int, list[datetime]] = defaultdict(list)
    for msg in messages:
        user_msg_times[msg.user_id].append(msg.timestamp)

    for msg in messages:
        if msg.reply_to_user_id is None:
            continue
        from_uid = msg.user_id
        to_uid = msg.reply_to_user_id
        if from_uid == to_uid:
            continue

        if to_uid not in user_map:
            user_map[to_uid] = f"id_{to_uid}"
            graph.add_node(to_uid)

        edge_data[from_uid][to_uid]["count"] += 1

        target_times = user_msg_times.get(to_uid, [])
        for t in reversed(target_times):
            if t < msg.timestamp:
                if (msg.timestamp - t).total_seconds() < 90:
                    edge_data[from_uid][to_uid]["fast_count"] += 1
                break

    reaction_weight: dict[tuple[int, int], int] = defaultdict(int)
    for rxn in reactions:
        from_uid = rxn.from_user_id
        to_uid = rxn.target_user_id
        if from_uid == to_uid:
            continue
        reaction_weight[(from_uid, to_uid)] += 1
        for uid in (from_uid, to_uid):
            if uid not in user_map:
                user_map[uid] = f"id_{uid}"
                graph.add_node(uid)

    all_edges: set[tuple[int, int]] = set()
    for f_uid, targets in edge_data.items():
        for t_uid in targets:
            all_edges.add((f_uid, t_uid))
    for pair in reaction_weight:
        all_edges.add(pair)

    for f_uid, t_uid in all_edges:
        ed = edge_data.get(f_uid, {}).get(t_uid, {"count": 0, "fast_count": 0})
        count = ed["count"]
        fast = ed["fast_count"]
        speed_factor = 1.8 if (count > 0 and fast / count > 0.4) else 1.0
        rxn_w = reaction_weight.get((f_uid, t_uid), 0)
        weight = count * speed_factor + rxn_w
        if weight > 0:
            graph.add_edge(f_uid, t_uid, weight=weight)

    return graph, user_map


# ---------------------------------------------------------------------------
# Algorithmic Metrics
# ---------------------------------------------------------------------------

async def compute_influence(graph: nx.DiGraph) -> dict[int, float]:
    """PageRank scaled to 0.0 - 10.0 with pure Python fallback."""
    if graph.number_of_nodes() == 0:
        return {}
    try:
        raw = nx.pagerank(graph, weight="weight")
    except Exception:
        raw = {node: sum(d.get("weight", 1) for _, _, d in graph.in_edges(node, data=True)) + 0.1 for node in graph.nodes()}

    max_pr = max(raw.values()) if raw else 1.0
    if max_pr == 0:
        max_pr = 1.0
    return {uid: (score / max_pr) * 10.0 for uid, score in raw.items()}


def compute_gravity_desperation(graph: nx.DiGraph) -> dict[int, dict]:
    """In-degree / out-degree ratio per node."""
    result: dict[int, dict] = {}
    for node in graph.nodes():
        in_d = graph.in_degree(node)
        out_d = graph.out_degree(node)
        ratio = (in_d / out_d) if out_d > 0 else (float("inf") if in_d > 0 else 0.0)

        if ratio > 1.3 or (in_d > 0 and out_d == 0):
            label = "Центр притяжения"
        elif ratio < 0.6:
            label = "Ищущий внимания"
        else:
            label = "Сбалансированный"

        result[node] = {"in_degree": in_d, "out_degree": out_d, "ratio": ratio, "label": label}
    return result


def find_secret_dynamic(graph: nx.DiGraph, user_map: dict[int, str]) -> dict | None:
    """Strongest bidirectional relationship."""
    best: dict | None = None
    best_weight = 0.0
    seen: set[tuple[int, int]] = set()

    for u, v, data in graph.edges(data=True):
        pair = (min(u, v), max(u, v))
        if pair in seen:
            continue
        seen.add(pair)
        if graph.has_edge(v, u):
            combined = data.get("weight", 0) + graph[v][u].get("weight", 0)
            if combined > best_weight:
                best_weight = combined
                best = {
                    "user_a": user_map.get(u, f"id_{u}"),
                    "user_b": user_map.get(v, f"id_{v}"),
                    "combined_weight": combined,
                }
    return best


def find_ignored_users(graph: nx.DiGraph, user_map: dict[int, str]) -> list[dict]:
    """Users with high outgoing push but minimal reciprocation."""
    ignored: list[dict] = []
    for node in graph.nodes():
        out_d = graph.out_degree(node)
        if out_d < 2:
            continue
        reciprocated = sum(1 for target in graph.successors(node) if graph.has_edge(target, node))
        neglected_rate = 1.0 - (reciprocated / out_d)
        if neglected_rate > 0.5:
            ignored.append({
                "user_id": node,
                "username": user_map.get(node, f"id_{node}"),
                "neglected_rate": round(neglected_rate, 3),
            })
    return sorted(ignored, key=lambda x: x["neglected_rate"], reverse=True)


# ---------------------------------------------------------------------------
# High-Level Handlers Data Payloads
# ---------------------------------------------------------------------------

async def get_group_anomalies(chat_id: int, days: int = 30) -> dict | None:
    """Telemetry payload for /pulse."""
    try:
        graph, user_map = await build_social_graph(chat_id, days)
        if graph.number_of_edges() < _MIN_INTERACTIONS:
            return None

        influence = await compute_influence(graph)
        secret = find_secret_dynamic(graph, user_map)
        ignored = find_ignored_users(graph, user_map)

        top_influencer = {"username": "N/A", "score": 0.0}
        if influence:
            top_uid = max(influence, key=influence.get)
            top_influencer = {"username": user_map.get(top_uid, f"id_{top_uid}"), "score": round(influence[top_uid], 2)}

        return {
            "top_influencer": top_influencer,
            "most_ignored": ignored[0] if ignored else None,
            "secret_dynamic": secret,
            "total_users": graph.number_of_nodes(),
            "total_interactions": graph.number_of_edges(),
        }
    except Exception:
        logger.exception("Failed to compute group anomalies for chat %s", chat_id)
        return None


async def get_group_leaderboard(chat_id: int, limit: int = 5) -> list[dict]:
    """Leaderboard payload for /top."""
    try:
        graph, user_map = await build_social_graph(chat_id, days=30)
        if graph.number_of_edges() < _MIN_INTERACTIONS:
            return []

        influence = await compute_influence(graph)
        gd = compute_gravity_desperation(graph)

        ranked = sorted(influence.items(), key=lambda x: x[1], reverse=True)[:limit]
        board = []
        for rank_idx, (uid, score) in enumerate(ranked, 1):
            board.append({
                "rank": rank_idx,
                "username": user_map.get(uid, f"id_{uid}"),
                "score": round(score, 1),
                "status": gd.get(uid, {}).get("label", "Сбалансированный"),
            })
        return board
    except Exception:
        logger.exception("Failed to compute leaderboard for chat %s", chat_id)
        return []


async def get_pair_metrics(chat_id: int, user_a_id: int, user_b_id: int, days: int = 30) -> dict | None:
    """Pairwise metrics for the /sync command."""
    try:
        graph, user_map = await build_social_graph(chat_id, days)
        w_a_to_b = graph[user_a_id][user_b_id].get("weight", 0.0) if graph.has_edge(user_a_id, user_b_id) else 0.0
        w_b_to_a = graph[user_b_id][user_a_id].get("weight", 0.0) if graph.has_edge(user_b_id, user_a_id) else 0.0
        total_w = w_a_to_b + w_b_to_a

        if total_w == 0:
            return None

        sync_percent = min(100.0, (min(w_a_to_b, w_b_to_a) / max(w_a_to_b, w_b_to_a, 0.1)) * 100.0) if total_w > 0 else 0.0

        u_a_name = user_map.get(user_a_id, f"id_{user_a_id}")
        u_b_name = user_map.get(user_b_id, f"id_{user_b_id}")

        if sync_percent > 70:
            verdict = "СИМБИОТИЧЕСКИЙ АЛЬЯНС"
            desc = "Полный паритет сигналов. Быстрые ответы и взаимное гравитационное притяжение."
        elif w_a_to_b > w_b_to_a * 2:
            verdict = f"ОДНОСТОРОННЯЯ ГРАВИТАЦИЯ (@{u_a_name})"
            desc = f"@{u_a_name} инициирует связь и тратит ресурсы. Второй узел держит холодную дистанцию."
        elif w_b_to_a > w_a_to_b * 2:
            verdict = f"ОДНОСТОРОННЯЯ ГРАВИТАЦИЯ (@{u_b_name})"
            desc = f"@{u_b_name} находится в зависимой орбите, генерируя основной объем импульсов."
        else:
            verdict = "УМЕРЕННЫЙ РЕЗОНАНС"
            desc = "Периодический обмен сигналами без выраженного доминирования одного из узлов."

        return {
            "user_a": u_a_name,
            "user_b": u_b_name,
            "weight_a_to_b": round(w_a_to_b, 1),
            "weight_b_to_a": round(w_b_to_a, 1),
            "sync_percent": round(sync_percent, 1),
            "verdict": verdict,
            "description": desc,
        }
    except Exception:
        logger.exception("Failed to compute pair metrics for chat %s", chat_id)
        return None


async def get_weekly_purge(chat_id: int) -> dict | None:
    """Special payload for Sunday Purge protocol.

    Designed to be fully defensive:
    - Returns ``None`` for groups with near-zero activity (< _MIN_INTERACTIONS edges).
    - DB errors during clown-reaction lookup are caught and defaulted.
    - Empty influence / ignored / secret dicts are handled without raising.
    """
    try:
        graph, user_map = await build_social_graph(chat_id, days=7)
    except Exception:
        logger.warning("Failed to build social graph for weekly purge in chat %s", chat_id)
        return None

    if graph.number_of_nodes() == 0 or graph.number_of_edges() < _MIN_INTERACTIONS:
        return None

    try:
        influence = await compute_influence(graph)
    except Exception:
        logger.warning("PageRank computation failed for chat %s during weekly purge", chat_id)
        influence = {}

    secret = find_secret_dynamic(graph, user_map)
    ignored = find_ignored_users(graph, user_map)

    # --- Clown of the week (toxic-reaction query) ---
    clown_user = "Никто не заслужил"
    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        async with get_session() as session:
            res_toxic = await session.execute(
                select(Reaction.target_user_id)
                .where(
                    Reaction.chat_id == chat_id,
                    Reaction.reaction_type.in_(["🤡", "💩", "🤮"]),
                    Reaction.timestamp >= cutoff,
                )
            )
            clown_targets = list(res_toxic.scalars().all())

        if clown_targets:
            top_clown_id = max(set(clown_targets), key=clown_targets.count)
            clown_user = (
                f"@{user_map.get(top_clown_id, f'id_{top_clown_id}')} "
                f"({clown_targets.count(top_clown_id)} реакций)"
            )
    except Exception:
        logger.warning("Clown-reaction query failed for chat %s, defaulting", chat_id)

    # --- Top influencer (safe against empty influence dict) ---
    top_user = "Не определён"
    if influence:
        try:
            top_uid = max(influence, key=influence.get)  # type: ignore[arg-type]
            top_user = f"@{user_map.get(top_uid, f'id_{top_uid}')}"
        except ValueError:
            pass  # empty dict edge case

    # --- Neglected user ---
    neglected_text = "Отсутствует"
    if ignored:
        neglected_text = f"@{ignored[0]['username']}"

    # --- Secret pair ---
    secret_text = "Не сформирована"
    if secret:
        secret_text = f"@{secret['user_a']} ↔ @{secret['user_b']}"

    return {
        "top_influencer": top_user,
        "clown_of_the_week": clown_user,
        "neglected": neglected_text,
        "secret_pair": secret_text,
    }


async def get_user_metrics(chat_id: int, user_id: int, days: int = 30) -> dict | None:
    """Per-user metrics for dossier."""
    try:
        graph, user_map = await build_social_graph(chat_id, days)
        if user_id not in graph or graph.number_of_edges() < _MIN_INTERACTIONS:
            return None

        influence = await compute_influence(graph)
        gd = compute_gravity_desperation(graph)

        user_inf = influence.get(user_id, 0.0)
        user_gd = gd.get(user_id, {"in_degree": 0, "out_degree": 0, "ratio": 0.0, "label": "Сбалансированный"})

        successors = list(graph.successors(user_id))
        top_target: str | None = None
        top_target_weight = 0.0
        for s in successors:
            w = graph[user_id][s].get("weight", 0)
            if w > top_target_weight:
                top_target_weight = w
                top_target = user_map.get(s, f"id_{s}")

        reciprocated = sum(1 for s in successors if graph.has_edge(s, user_id))
        out_d = user_gd["out_degree"]
        neglected_rate = (1.0 - reciprocated / out_d) if out_d > 0 else 0.0

        return {
            "username": user_map.get(user_id, f"id_{user_id}"),
            "user_id": user_id,
            "influence_score": round(user_inf, 2),
            "top_targeted_user": top_target,
            "neglected_rate": round(neglected_rate, 3),
            "in_degree": user_gd["in_degree"],
            "out_degree": user_gd["out_degree"],
            "gravity_label": user_gd["label"],
        }
    except Exception:
        logger.exception("Failed to compute user metrics for user %s in chat %s", user_id, chat_id)
        return None