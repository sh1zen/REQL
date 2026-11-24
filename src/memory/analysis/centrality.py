"""Deterministic centrality metrics for the local property graph."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import default_config
from ..config.models import CentralityPolicy
from ..domain.models import MemoryEdge, MemoryNode
from ..storage.graph_store import GraphStore


@dataclass(frozen=True, slots=True)
class CentralityMetrics:
    node_id: str
    degree_centrality: float
    weighted_degree: float
    in_degree: int
    out_degree: int
    bridge_score: float
    community_bridge_score: float
    activation_frequency: float
    retrieval_usefulness: float


class CentralityCalculator:
    """Computes dependency-light graph centrality approximations."""

    def __init__(self, store: GraphStore, *, policy: CentralityPolicy | None = None) -> None:
        self.store = store
        self.policy = policy if policy is not None else default_config().analysis.centrality

    def calculate(
        self,
        *,
        project_id: str | None = None,
        community_by_node: dict[str, str] | None = None,
        candidate_nodes: list[MemoryNode] | None = None,
        edge_limit: int | None = None,
    ) -> dict[str, CentralityMetrics]:
        """Calculate centrality from the scoped edges unless the caller sets an edge limit."""
        policy = self.policy
        ignored_edges = set(policy.ignored_edge_types)
        nodes = candidate_nodes if candidate_nodes is not None else _candidate_nodes(
            self.store.all_nodes(), project_id, policy.excluded_node_types,
        )
        node_ids = {node.id for node in nodes}
        if edge_limit is None and candidate_nodes is None:
            edge_records = self.store.all_edges()
        else:
            edge_records = self.store.incident_edges(
                sorted(node_ids), ignored_edge_types=ignored_edges, limit=edge_limit, clone=False,
            )
        in_edges: dict[str, list[MemoryEdge]] = {node_id: [] for node_id in node_ids}
        out_edges: dict[str, list[MemoryEdge]] = {node_id: [] for node_id in node_ids}
        neighbors: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
        for edge in edge_records:
            if edge.from_id not in node_ids or edge.to_id not in node_ids or edge.type in ignored_edges:
                continue
            out_edges[edge.from_id].append(edge)
            in_edges[edge.to_id].append(edge)
            neighbors[edge.from_id].add(edge.to_id)
            neighbors[edge.to_id].add(edge.from_id)

        max_degree = max((len(neighbors[node_id]) for node_id in node_ids), default=1) or 1
        weighted_by_node = {
            node_id: sum(edge.weight for edge in in_edges[node_id]) + sum(edge.weight for edge in out_edges[node_id])
            for node_id in node_ids
        }
        max_weighted = max(weighted_by_node.values(), default=1.0) or 1.0
        community_by_node = community_by_node or {}
        out: dict[str, CentralityMetrics] = {}
        for node in nodes:
            degree = len(neighbors[node.id])
            weighted = weighted_by_node[node.id]
            out[node.id] = CentralityMetrics(
                node_id=node.id,
                degree_centrality=degree / max_degree,
                weighted_degree=weighted / max_weighted,
                in_degree=len(in_edges[node.id]),
                out_degree=len(out_edges[node.id]),
                bridge_score=self._bridge_score(node.id, neighbors),
                community_bridge_score=self._community_bridge_score(node.id, neighbors, community_by_node),
                activation_frequency=_activation_frequency(node, policy.activation_saturation),
                retrieval_usefulness=_retrieval_usefulness(node),
            )
        return out

    def _bridge_score(self, node_id: str, neighbors: dict[str, set[str]]) -> float:
        """Count connected neighbor pairs directly, including in sparse neighborhoods."""
        adjacent = neighbors[node_id]
        if len(adjacent) < 2:
            return 0.0
        connected_pairs = sum(len(adjacent & neighbors[left]) - (left in neighbors[left]) for left in adjacent) // 2
        total_pairs = len(adjacent) * (len(adjacent) - 1) // 2
        return (total_pairs - connected_pairs) / total_pairs

    def _community_bridge_score(self, node_id: str, neighbors: dict[str, set[str]], community_by_node: dict[str, str]) -> float:
        communities = {community_by_node.get(neighbor) for neighbor in neighbors.get(node_id, set()) if community_by_node.get(neighbor)}
        if not communities:
            return 0.0
        return min(1.0, (len(communities) - 1) / self.policy.community_saturation)


def _candidate_nodes(
    nodes: list[MemoryNode], project_id: str | None, excluded_types: tuple[str, ...],
) -> list[MemoryNode]:
    blocked = set(excluded_types)
    out = []
    for node in nodes:
        if node.status in {"archived", "deleted", "rejected"} or node.type in blocked:
            continue
        if project_id and node.properties.get("project_id") not in {project_id, None}:
            continue
        out.append(node)
    return out


def _activation_frequency(node: MemoryNode, saturation: float) -> float:
    raw = float(node.usage_count or 0) + float(node.properties.get("activation_count") or 0)
    return min(1.0, raw / saturation)


def _retrieval_usefulness(node: MemoryNode) -> float:
    raw = float(node.properties.get("retrieval_usefulness") or node.properties.get("usage_success") or 0.0)
    return max(0.0, min(1.0, raw))
