"""Lightweight deterministic community detection."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from heapq import heapify, heappop, heappush
import math
from typing import Any

from ..config import default_config
from ..config.models import CommunityPolicy
from ..domain.ids import stable_id
from ..domain.models import MemoryEdge, MemoryNode
from ..domain.timeutils import utcnow_iso
from ..storage.graph_store import GraphStore


@dataclass(slots=True)
class CommunityResult:
    community_nodes: list[MemoryNode]
    membership: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {"communities": [node.to_dict() for node in self.community_nodes], "membership": dict(self.membership)}


class CommunityDetector:
    """Deterministic label propagation suitable for local graphs."""

    def __init__(self, store: GraphStore, *, policy: CommunityPolicy | None = None) -> None:
        self.store = store
        self.policy = policy if policy is not None else default_config().analysis.communities

    def detect(
        self,
        *,
        project_id: str | None = None,
        limit: int | None = None,
        options: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> CommunityResult:
        """Compute communities and optionally write them and their relationships."""
        policy = self.policy
        options = options or {}
        max_iterations = int(options["max_iterations"]) if "max_iterations" in options else None
        min_size = int(options.get("min_size", policy.min_size))
        nodes = _candidate_nodes(self.store, project_id, policy.excluded_node_types)
        node_ids = {node.id for node in nodes}
        edges = self.store.incident_edges(
            sorted(node_ids), ignored_edge_types=set(policy.ignored_edge_types), limit=None, clone=False,
        )
        adjacency = _adjacency(edges, node_ids)
        labels = _propagate_labels(adjacency, max_iterations=max_iterations)

        grouped: dict[str, list[str]] = defaultdict(list)
        for node_id, label in labels.items():
            grouped[label].append(node_id)
        communities = [sorted(member_ids) for member_ids in grouped.values() if len(member_ids) >= min_size]
        communities.sort(key=lambda members: (-len(members), members[0]))
        if limit is not None:
            communities = communities[:limit]

        membership: dict[str, str] = {}
        community_nodes: list[MemoryNode] = []
        nodes_by_id = {node.id: node for node in nodes}
        with self.store.transaction() if persist and communities else nullcontext():
            for index, member_ids in enumerate(communities, start=1):
                community_id = stable_id("community", project_id or "global", ",".join(member_ids))
                member_nodes = [nodes_by_id[node_id] for node_id in member_ids]
                label = _community_label(member_nodes, index)
                density = _density(member_ids, adjacency)
                salience = sum(node.salience for node in member_nodes) / len(member_ids)
                now = utcnow_iso()
                community_node = MemoryNode(
                    id=community_id,
                    type="Community",
                    label=label,
                    canonical_key=community_id,
                    properties={
                        "id": community_id,
                        "project_id": project_id,
                        "label": label,
                        "size": len(member_ids),
                        "density": density,
                        "salience": salience,
                        "status": "active",
                        "created_at": now,
                        "updated_at": now,
                        "member_ids": member_ids,
                    },
                    salience=salience,
                    confidence=1.0,
                    status="active",
                )
                if persist:
                    community_node, _ = self.store.upsert_node(community_node)
                community_nodes.append(community_node)
                membership.update(dict.fromkeys(member_ids, community_node.id))
                if not persist:
                    continue
                for node_id in member_ids:
                    self.store.upsert_edge(
                        MemoryEdge(
                            id=stable_id("edge", node_id, "BELONGS_TO_COMMUNITY", community_node.id),
                            from_id=node_id,
                            to_id=community_node.id,
                            type="BELONGS_TO_COMMUNITY",
                            weight=1.0,
                            confidence=1.0,
                            origin="deterministic",
                            properties={"project_id": project_id, "community_id": community_node.id},
                        )
                    )

            if persist and membership:
                self._mark_bridges(project_id=project_id, adjacency=adjacency, membership=membership)
        return CommunityResult(community_nodes=community_nodes, membership=membership)

    def _mark_bridges(self, *, project_id: str | None, adjacency: dict[str, dict[str, float]], membership: dict[str, str]) -> None:
        for node_id, neighbors in adjacency.items():
            node_community = membership.get(node_id)
            neighbor_communities = {
                community_id
                for neighbor in neighbors
                if (community_id := membership.get(neighbor)) and community_id != node_community
            }
            for community_id in sorted(neighbor_communities):
                self.store.upsert_edge(
                    MemoryEdge(
                        id=stable_id("edge", node_id, "BRIDGES_COMMUNITY", community_id),

                        from_id=node_id,
                        to_id=community_id,
                        type="BRIDGES_COMMUNITY",
                        weight=self.policy.bridge_weight,
                        confidence=self.policy.bridge_confidence,
                        origin="deterministic",
                        properties={"project_id": project_id, "community_id": community_id},
                    )
                )


def _candidate_nodes(
    store: GraphStore, project_id: str | None, excluded_types: tuple[str, ...],
) -> list[MemoryNode]:
    node_types = sorted(set(store.node_type_counts()) - set(excluded_types))
    out = []
    for node in store.find_nodes_by_types(node_types, clone=False):
        if node.status in {"archived", "deleted", "rejected"}:
            continue
        if project_id and node.properties.get("project_id") not in {project_id, None}:
            continue
        out.append(node)
    return out


def _adjacency(
    edges: list[MemoryEdge], node_ids: set[str],
) -> dict[str, dict[str, float]]:
    adjacency: dict[str, dict[str, float]] = {node_id: {} for node_id in node_ids}
    for edge in edges:
        if edge.from_id not in node_ids or edge.to_id not in node_ids:
            continue
        weight = max(0.0, edge.weight * edge.confidence)
        adjacency[edge.from_id][edge.to_id] = adjacency[edge.from_id].get(edge.to_id, 0.0) + weight
        adjacency[edge.to_id][edge.from_id] = adjacency[edge.to_id].get(edge.from_id, 0.0) + weight
    # The shared binary scale keeps each label move exact. A move must improve
    # the graph's agreement or choose a smaller label at equal agreement.
    scale = 1
    for neighbors in adjacency.values():
        for weight in neighbors.values():
            if not math.isfinite(weight):
                raise ValueError("Community analysis requires finite relationship weights")
            scale = max(scale, weight.as_integer_ratio()[1])
    for neighbors in adjacency.values():
        for neighbor, weight in neighbors.items():
            numerator, denominator = weight.as_integer_ratio()
            neighbors[neighbor] = numerator * (scale // denominator)
    return adjacency


def _propagate_labels(adjacency: dict[str, dict[str, float]], *, max_iterations: int | None) -> dict[str, str]:
    """Visit changed neighborhoods in node order until their labels settle."""
    labels = {node_id: node_id for node_id in adjacency}
    pending = set(adjacency)
    iteration = 0
    while pending and (max_iterations is None or iteration < max_iterations):
        queue = list(pending)
        heapify(queue)
        next_pass: set[str] = set()
        while queue:
            node_id = heappop(queue)
            pending.remove(node_id)
            current = labels[node_id]
            scores: Counter[str] = Counter({current: 0})
            for neighbor, weight in adjacency[node_id].items():
                scores[labels[neighbor]] += weight
            best = min(scores, key=lambda label: (-scores[label], label))
            if best == current:
                continue
            labels[node_id] = best
            for neighbor in adjacency[node_id]:
                if neighbor < node_id:
                    next_pass.add(neighbor)
                elif neighbor > node_id and neighbor not in pending:
                    pending.add(neighbor)
                    heappush(queue, neighbor)
        pending = next_pass
        iteration += 1
    return labels


def _density(member_ids: list[str], adjacency: dict[str, dict[str, float]]) -> float:
    size = len(member_ids)
    if size < 2:
        return 0.0
    possible = size * (size - 1) / 2
    actual = 0
    members = set(member_ids)
    for node_id in member_ids:
        actual += sum(1 for neighbor in adjacency[node_id] if neighbor in members)
    return min(1.0, (actual / 2) / possible)


def _community_label(nodes: list[MemoryNode], index: int) -> str:
    """Name the community after a representative member."""
    for node in nodes:
        value = node.properties.get("name") or node.label or node.text or node.canonical_key
        if value and (label := next(iter(str(value).splitlines()), "").strip()):
            return label
    return f"Community {index}"


def stored_community_membership(store: GraphStore, *, project_id: str | None) -> dict[str, str]:
    """Read all memberships and prefer the latest link within the requested scope."""
    membership: dict[str, str] = {}
    ranks: dict[str, tuple[bool, str, str]] = {}
    for edge in store.get_edges(type_="BELONGS_TO_COMMUNITY", limit=None, clone=False):
        scope = edge.properties.get("project_id")
        if project_id is not None and scope not in (project_id, None):
            continue
        if edge.properties.get("status") == "archived":
            continue
        rank = project_id is not None and scope == project_id, edge.updated_at, edge.id
        if edge.from_id not in ranks or rank > ranks[edge.from_id]:
            ranks[edge.from_id] = rank
            membership[edge.from_id] = edge.to_id
    return membership
