"""Deterministic cross-community bridge candidate detection."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

from ..config import AnalysisConfig, default_config
from ..config.models import BridgePolicy
from ..domain.constants import ACTIVE_STATUSES
from ..domain.models import MemoryNode
from ..storage.graph_store import GraphStore
from .communities import CommunityDetector, stored_community_membership
from .specificity import SpecificityScorer


@dataclass(frozen=True, slots=True)
class BridgeCandidate:
    bridge_node_id: str
    bridge_label: str
    bridge_type: str
    community_ids: tuple[str, ...]
    community_labels: tuple[str, ...]
    evidence_node_ids: tuple[str, ...]
    evidence_labels: tuple[str, ...]
    cross_community_distance: float
    novelty: float
    potential_utility: float
    evidence_strength: float
    bridge_centrality: float
    specificity: float
    generic_penalty: float
    randomness_penalty: float
    bridge_score: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class BridgeDetector:
    """Finds specific nodes that bridge multiple existing communities."""

    def __init__(self, store: GraphStore, *, config: AnalysisConfig | None = None) -> None:
        config = config if config is not None else default_config().analysis
        self.store = store
        self.policy = config.bridges
        self.communities = CommunityDetector(store, policy=config.communities)
        self.specificity = SpecificityScorer(store, policy=config.specificity)

    def find_candidates(
        self,
        *,
        project_id: str | None = None,
        limit: int | None = 50,
    ) -> list[BridgeCandidate]:
        """Score all eligible bridges and apply the requested result count."""
        policy = self.policy
        membership = stored_community_membership(self.store, project_id=project_id)
        if not membership:
            membership = self.communities.detect(project_id=project_id, limit=None).membership
        community_labels = self._community_labels(set(membership.values()))
        ignored_edge_types = set(policy.ignored_edge_types)
        connectivity, contributions = _community_connectivity(self.store, membership, ignored_edge_types)
        nodes = self._candidate_nodes(project_id=project_id)
        candidates: list[BridgeCandidate] = []
        for node in nodes:
            incident = self._incident_evidence(node, ignored_edge_types)
            by_community: dict[str, list[tuple[MemoryNode, float]]] = defaultdict(list)
            for neighbor, strength in incident:
                community_id = membership.get(neighbor.id)
                if community_id:
                    by_community[community_id].append((neighbor, strength))
            if len(by_community) < policy.min_communities:
                continue

            evidence = _select_evidence(by_community)
            evidence_strength = sum(strength for _, strength in evidence) / len(evidence)

            spec = self.specificity.score(node)
            if spec.generic_penalty >= policy.max_generic_penalty:
                continue
            distance = _cross_community_distance(set(by_community), connectivity, contributions.get(node.id, {}))
            bridge_centrality = min(
                1.0, len(by_community) / policy.community_saturation * policy.centrality_community_weight
                + min(1.0, len(incident) / policy.incident_saturation) * policy.centrality_incident_weight,
            )
            novelty = min(
                1.0, distance * policy.novelty_distance_weight
                + min(1.0, len(by_community) / policy.novelty_community_saturation) * policy.novelty_community_weight,
            )
            utility = _potential_utility(node, spec.specificity, evidence_strength, policy)
            randomness_penalty = _randomness_penalty(evidence_strength, len(evidence), distance, spec.specificity, policy)
            score = _bridge_score(
                policy,
                cross_community_distance=distance,
                novelty=novelty,
                potential_utility=utility,
                evidence_strength=evidence_strength,
                bridge_centrality=bridge_centrality,
                specificity=spec.specificity,
                randomness_penalty=randomness_penalty,
                generic_penalty=spec.generic_penalty,
            )
            if score < policy.min_score:
                continue

            community_ids = tuple(sorted(by_community))
            candidates.append(
                BridgeCandidate(
                    bridge_node_id=node.id,
                    bridge_label=_node_label(node),
                    bridge_type=node.type,
                    community_ids=community_ids,
                    community_labels=tuple(community_labels.get(cid, cid) for cid in community_ids),
                    evidence_node_ids=tuple(neighbor.id for neighbor, _ in evidence),
                    evidence_labels=tuple(_node_label(neighbor) for neighbor, _ in evidence),
                    cross_community_distance=distance,
                    novelty=novelty,
                    potential_utility=utility,
                    evidence_strength=evidence_strength,
                    bridge_centrality=bridge_centrality,
                    specificity=spec.specificity,
                    generic_penalty=spec.generic_penalty,
                    randomness_penalty=randomness_penalty,
                    bridge_score=score,
                )
            )
        candidates.sort(key=lambda item: (item.bridge_score, item.specificity, item.evidence_strength), reverse=True)
        return candidates[:limit]

    def _community_labels(self, community_ids: set[str]) -> dict[str, str]:
        return {node.id: node.label or node.text or node.id
                for node in self.store.get_nodes(sorted(community_ids), clone=False)}

    def _candidate_nodes(self, *, project_id: str | None) -> list[MemoryNode]:
        return [
            node
            for node, _, _ in self.store.top_nodes_by_degree(
                limit=None,
                statuses=set(ACTIVE_STATUSES),
                exclude_types=set(self.policy.excluded_node_types),
                ignored_edge_types=set(self.policy.ignored_edge_types),
                project_id=project_id,
                include_global_project=True,
                clone=False,
            )
        ]

    def _incident_evidence(self, node: MemoryNode, ignored_edge_types: set[str]) -> list[tuple[MemoryNode, float]]:
        evidence: dict[str, tuple[MemoryNode, float]] = {}
        for edge in self.store.incident_edges([node.id], ignored_edge_types=ignored_edge_types, limit=None, clone=False):
            neighbor_id = edge.to_id if edge.from_id == node.id else edge.from_id
            neighbor = self.store.get_node(neighbor_id, clone=False)
            if not neighbor or neighbor.status in {"archived", "deleted", "rejected"}:
                continue
            strength = max(0.0, min(1.0, edge.weight * edge.confidence))
            existing = evidence.get(neighbor.id)
            if existing is None or strength > existing[1]:
                evidence[neighbor.id] = (neighbor, strength)
        return list(evidence.values())


def _select_evidence(by_community: dict[str, list[tuple[MemoryNode, float]]]) -> list[tuple[MemoryNode, float]]:
    """Retain the strongest representative of each community."""
    selected = [
        max(items, key=lambda item: (item[1], item[0].salience, _node_label(item[0])))
        for items in by_community.values()
    ]
    return sorted(selected, key=lambda item: (item[1], item[0].salience, item[0].id), reverse=True)


def _community_connectivity(
    store: GraphStore, membership: dict[str, str], ignored_edge_types: set[str],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[tuple[str, str], float]]]:
    """Compute pair strengths once and track what each bridge contributes."""
    pairs: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    contributions: dict[str, dict[tuple[str, str], float]] = defaultdict(lambda: defaultdict(float))
    for edge in store.incident_edges(sorted(membership), ignored_edge_types=ignored_edge_types, limit=None, clone=False):
        source = membership.get(edge.from_id)
        target = membership.get(edge.to_id)
        if source is None or target is None or source == target:
            continue
        strength = max(0.0, edge.weight * edge.confidence)
        if not strength:
            continue
        left, right = sorted((source, target))
        pairs[left][right] += strength
        pair = (left, right)
        contributions[edge.from_id][pair] += strength
        contributions[edge.to_id][pair] += strength
    return pairs, contributions


def _cross_community_distance(
    communities: set[str], connectivity: dict[str, dict[str, float]], own: dict[tuple[str, str], float],
) -> float:
    """Measure direct pair strength after excluding the candidate's edges."""
    pair_count = len(communities) * (len(communities) - 1) // 2
    direct_strength = sum(
        min(1.0, max(0.0, strength - own.get((left, right), 0.0)))
        for left in sorted(communities)
        for right, strength in connectivity.get(left, {}).items()
        if right in communities
    )
    return 1.0 - direct_strength / pair_count


def _potential_utility(node: MemoryNode, specificity: float, evidence_strength: float, policy: BridgePolicy) -> float:
    stored = max(float(node.utility or 0.0), float(node.properties.get("usage_success") or 0.0), float(node.properties.get("retrieval_usefulness") or 0.0))
    graph_signal = (
        node.salience * policy.utility_salience_weight + specificity * policy.utility_specificity_weight
        + evidence_strength * policy.utility_evidence_weight
    )
    return max(0.0, min(1.0, max(stored, graph_signal)))


def _randomness_penalty(evidence_strength: float, evidence_count: int, distance: float, specificity: float, policy: BridgePolicy) -> float:
    penalty = 0.0
    if evidence_count < policy.randomness_min_evidence:
        penalty += policy.randomness_count_penalty
    if evidence_strength < policy.randomness_min_strength:
        penalty += policy.randomness_strength_penalty
    if specificity < policy.randomness_min_specificity:
        penalty += policy.randomness_specificity_penalty
    if distance < policy.randomness_min_distance:
        penalty += policy.randomness_distance_penalty
    return max(0.0, min(1.0, penalty))


def _bridge_score(
    policy: BridgePolicy,
    *,
    cross_community_distance: float,
    novelty: float,
    potential_utility: float,
    evidence_strength: float,
    bridge_centrality: float,
    specificity: float,
    randomness_penalty: float,
    generic_penalty: float,
) -> float:
    score = (
        policy.distance_weight * cross_community_distance
        + policy.novelty_weight * novelty
        + policy.utility_weight * potential_utility
        + policy.evidence_weight * evidence_strength
        + policy.centrality_weight * bridge_centrality
        + policy.specificity_weight * specificity
        - policy.randomness_weight * randomness_penalty
        - policy.generic_penalty_weight * generic_penalty
    )
    return max(0.0, min(1.0, score))


def _node_label(node: MemoryNode) -> str:
    return node.label or node.text or node.canonical_key or node.id
