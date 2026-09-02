"""Social-graph analytics engine built on NetworkX.

Constructs directed weighted graphs from message replies and reactions,
then computes influence (PageRank), gravity/desperation ratios,
mutual closeness ("secret dynamic"), and ignored-user metrics.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

import networkx as nx
from sqlalchemy import select

from database import Message, Reaction, get_session

logger = logging.getLogger(__name__)

# Minimum interactions to produce meaningful analytics
_MIN_MESSAGES = 5


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

async def build_social_graph(
    chat_id: int,
    days: int = 30,
) -> tuple[nx.DiGraph, dict[int, str]]:
    """Build a directed weighted graph from reply + reaction data.

    Returns
    -------
    graph : nx.DiGraph
        Nodes are ``user_id``; edges carry a ``weight`` attribute.
    user_map : dict[int, str]
        ``{user_id: display_name}`` for labelling.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    session = get_session()
    try:
        # --- Load messages ---------------------------------------------------
        result = await session.execute(
            select(Message)
            .where(Message.chat_id == chat_id, Message.timestamp >= cutoff)
            .order_by(Message.timestamp)
        )
        messages: list[Message] = list(result.scalars().all())

        # --- Load reactions ---------------------------------------------------
        result = await session.execute(
            select(Reaction)
            .where(Reaction.chat_id == chat_id, Reaction.timestamp >= cutoff)
        )
        reactions: list[Reaction] = list(result.scalars().all())
    finally:
        await session.close()

    user_map: dict[int, str] = {}
    graph = nx.DiGraph()

    # Collect per-message timestamps for speed-factor calculation
    msg_timestamps: dict[tuple[int, int], datetime] = {}  # (message_id, chat_id) -> ts
    msg_user: dict[tuple[int, int], int] = {}              # -> user_id

    for msg in messages:
        uid = msg.user_id
        user_map.setdefault(uid, msg.username or f"id_{uid}")
        graph.add_node(uid)
        msg_timestamps[(msg.message_id, msg.chat_id)] = msg.timestamp
        msg_user[(msg.message_id, msg.chat_id)] = uid

    # --- Reply edges --------------------------------------------------------
    # edge_data[from_uid][to_uid] = {"count": int, "fast_count": int}
    edge_data: dict[int, dict[int, dict]] = defaultdict(lambda: defaultdict(lambda: {"count": 0, "fast_count": 0}))

    # Build lookup: user_id -> list of their message timestamps (for speed calc)
    user_msg_times: dict[int, list[datetime]] = defaultdict(list)
    for msg in messages:
        user_msg_times[msg.user_id].append(msg.timestamp)

    for msg in messages:
        if msg.reply_to_user_id is None:
            continue
        from_uid = msg.user_id
        to_uid = msg.reply_to_user_id
        if from_uid == to_uid:
            continue  # self-replies don't count

        # Ensure target node exists
        if to_uid not in user_map:
            user_map[to_uid] = f"id_{to_uid}"
            graph.add_node(to_uid)

        edge_data[from_uid][to_uid]["count"] += 1

        # Speed factor: check if this reply came within 60s of the target's
        # most recent prior message.
        target_times = user_msg_times.get(to_uid, [])
        for t in reversed(target_times):
            if t < msg.timestamp:
                if (msg.timestamp - t).total_seconds() < 60:
                    edge_data[from_uid][to_uid]["fast_count"] += 1
                break

    # --- Reaction edges (additive weight) ------------------------------------
    reaction_weight: dict[tuple[int, int], int] = defaultdict(int)
    for rxn in reactions:
        from_uid = rxn.from_user_id
        to_uid = rxn.target_user_id
        if from_uid == to_uid:
            continue
        reaction_weight[(from_uid, to_uid)] += 1
        # Ensure nodes exist
        for uid in (from_uid, to_uid):
            if uid not in user_map:
                user_map[uid] = f"id_{uid}"
                graph.add_node(uid)

    # --- Compute final edge weights -----------------------------------------
    # weight = count * speed_factor + reactions
    # speed_factor per edge: 2.0 if >50 % of replies were fast, else 1.0
    all_edges: set[tuple[int, int]] = set()
    for from_uid, targets in edge_data.items():
        for to_uid in targets:
            all_edges.add((from_uid, to_uid))
    for pair in reaction_weight:
        all_edges.add(pair)

    for from_uid, to_uid in all_edges:
        ed = edge_data.get(from_uid, {}).get(to_uid, {"count": 0, "fast_count": 0})
        count = ed["count"]
        fast = ed["fast_count"]
        speed_factor = 2.0 if (count > 0 and fast / count > 0.5) else 1.0
        rxn_w = reaction_weight.get((from_uid, to_uid), 0)
        weight = count * speed_factor + rxn_w
        if weight > 0:
            graph.add_edge(from_uid, to_uid, weight=weight)

    return graph, user_map


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

async def compute_influence(graph: nx.DiGraph) -> dict[int, float]:
    """PageRank scaled to 0.0 – 10.0."""
    if graph.number_of_nodes() == 0:
        return {}
    try:
        raw = nx.pagerank(graph, weight="weight")
    except nx.NetworkXError:
        return {}
    max_pr = max(raw.values()) if raw else 1.0
    if max_pr == 0:
        max_pr = 1.0
    return {uid: (score / max_pr) * 10.0 for uid, score in raw.items()}


def compute_gravity_desperation(graph: nx.DiGraph) -> dict[int, dict]:
    """In-degree / out-degree ratio per node.

    Returns ``{user_id: {in_degree, out_degree, ratio, label}}``.
    """
    result: dict[int, dict] = {}
    for node in graph.nodes():
        in_d = graph.in_degree(node)
        out_d = graph.out_degree(node)
        if out_d == 0:
            ratio = float("inf") if in_d > 0 else 0.0
        else:
            ratio = in_d / out_d

        if ratio > 1.5 or (in_d > 0 and out_d == 0):
            label = "Gravitational"
        elif ratio < 0.5:
            label = "Desperate"
        else:
            label = "Balanced"

        result[node] = {
            "in_degree": in_d,
            "out_degree": out_d,
            "ratio": ratio,
            "label": label,
        }
    return result


def find_secret_dynamic(
    graph: nx.DiGraph,
    user_map: dict[int, str],
) -> dict | None:
    """Highest-weighted bidirectional pair, or ``None``."""
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
                    "user_a_id": u,
                    "user_b_id": v,
                    "combined_weight": combined,
                }
    return best


def find_ignored_users(
    graph: nx.DiGraph,
    user_map: dict[int, str],
) -> list[dict]:
    """Users with high out-degree but near-zero reciprocation."""
    ignored: list[dict] = []
    for node in graph.nodes():
        out_d = graph.out_degree(node)
        if out_d < 3:
            continue
        reciprocated = sum(1 for target in graph.successors(node) if graph.has_edge(target, node))
        neglected_rate = 1.0 - (reciprocated / out_d)
        if neglected_rate > 0.6:
            ignored.append({
                "user_id": node,
                "username": user_map.get(node, f"id_{node}"),
                "out_degree": out_d,
                "reciprocated": reciprocated,
                "neglected_rate": round(neglected_rate, 3),
            })
    return sorted(ignored, key=lambda x: x["neglected_rate"], reverse=True)


# ---------------------------------------------------------------------------
# High-level queries
# ---------------------------------------------------------------------------

async def get_group_anomalies(chat_id: int, days: int = 30) -> dict | None:
    """Full analytics payload for the ``/pulse`` command.

    Returns ``None`` when the group has fewer than ``_MIN_MESSAGES`` interactions.
    """
    graph, user_map = await build_social_graph(chat_id, days)

    total_interactions = graph.number_of_edges()
    total_users = graph.number_of_nodes()

    if total_interactions < _MIN_MESSAGES:
        return None

    influence = await compute_influence(graph)
    gd = compute_gravity_desperation(graph)
    secret = find_secret_dynamic(graph, user_map)
    ignored = find_ignored_users(graph, user_map)

    # Top influencer
    if influence:
        top_uid = max(influence, key=influence.get)  # type: ignore[arg-type]
        top_influencer = {
            "username": user_map.get(top_uid, f"id_{top_uid}"),
            "score": round(influence[top_uid], 2),
        }
    else:
        top_influencer = {"username": "N/A", "score": 0.0}

    most_ignored = ignored[0] if ignored else None

    return {
        "top_influencer": top_influencer,
        "most_ignored": most_ignored,
        "secret_dynamic": secret,
        "total_users": total_users,
        "total_interactions": total_interactions,
    }


async def get_user_metrics(
    chat_id: int,
    user_id: int,
    days: int = 30,
) -> dict | None:
    """Per-user metrics for the dossier card.

    Returns ``None`` when the user is absent from the graph or data is sparse.
    """
    graph, user_map = await build_social_graph(chat_id, days)

    if user_id not in graph:
        return None

    if graph.number_of_edges() < _MIN_MESSAGES:
        return None

    influence = await compute_influence(graph)
    gd = compute_gravity_desperation(graph)

    user_inf = influence.get(user_id, 0.0)
    user_gd = gd.get(user_id, {"in_degree": 0, "out_degree": 0, "ratio": 0.0, "label": "Unknown"})

    # Top targeted user
    successors = list(graph.successors(user_id))
    top_target: str | None = None
    top_target_weight = 0.0
    for s in successors:
        w = graph[user_id][s].get("weight", 0)
        if w > top_target_weight:
            top_target_weight = w
            top_target = user_map.get(s, f"id_{s}")

    # Reciprocity: what % of user's targets reply back
    reciprocated = sum(1 for s in successors if graph.has_edge(s, user_id))
    reciprocity = (reciprocated / len(successors) * 100) if successors else 0.0

    # Neglected rate
    out_d = user_gd["out_degree"]
    neglected_rate = (1.0 - reciprocated / out_d) if out_d > 0 else 0.0

    # Response lag avg (seconds) – average time between user's message and their
    # reply targets' previous message.  Approximated from the graph build data.
    cutoff = datetime.utcnow() - timedelta(days=days)
    session = get_session()
    try:
        result = await session.execute(
            select(Message)
            .where(
                Message.chat_id == chat_id,
                Message.user_id == user_id,
                Message.reply_to_user_id.isnot(None),
                Message.timestamp >= cutoff,
            )
            .order_by(Message.timestamp)
        )
        reply_msgs = list(result.scalars().all())
    finally:
        await session.close()

    lags: list[float] = []
    session2 = get_session()
    try:
        for rm in reply_msgs:
            # Find the latest message from the target user before this reply
            result = await session2.execute(
                select(Message.timestamp)
                .where(
                    Message.chat_id == chat_id,
                    Message.user_id == rm.reply_to_user_id,
                    Message.timestamp < rm.timestamp,
                )
                .order_by(Message.timestamp.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                lag = (rm.timestamp - row).total_seconds()
                if lag >= 0:
                    lags.append(lag)
    finally:
        await session2.close()

    avg_lag = sum(lags) / len(lags) if lags else 0.0

    # Total messages by this user
    session3 = get_session()
    try:
        result = await session3.execute(
            select(Message)
            .where(
                Message.chat_id == chat_id,
                Message.user_id == user_id,
                Message.timestamp >= cutoff,
            )
        )
        total_messages = len(result.scalars().all())
    finally:
        await session3.close()

    return {
        "username": user_map.get(user_id, f"id_{user_id}"),
        "user_id": user_id,
        "influence_score": round(user_inf, 2),
        "response_lag_avg_seconds": round(avg_lag, 1),
        "top_targeted_user": top_target,
        "target_reciprocity_percent": round(reciprocity, 1),
        "neglected_rate": round(neglected_rate, 3),
        "in_degree": user_gd["in_degree"],
        "out_degree": user_gd["out_degree"],
        "gravity_label": user_gd["label"],
        "total_messages": total_messages,
    }
