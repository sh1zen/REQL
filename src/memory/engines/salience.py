"""Salience scoring for graph-native memory."""
from __future__ import annotations

from typing import Any

from ..config import default_config
from ..config.models import SaliencePolicy
from ..domain.constants import INACTIVE_STATUSES
from ..domain.models import MemoryNode
from ..extraction.normalization import clamp
from ..domain.timeutils import seconds_since
from ..storage.graph_store import GraphStore


class SalienceEngine:
    """Numerical salience computation independent of LLM calls."""

    def __init__(self, store: GraphStore, *, policy: SaliencePolicy | None = None) -> None:
        self.store = store
        self.policy = policy if policy is not None else default_config().analysis.salience

    def compute_salience_signal(self, node: MemoryNode) -> dict[str, Any]:
        """Compute deterministic salience for code graph retrieval."""
        policy = self.policy
        props = node.properties
        novelty = clamp(float(props.get("novelty", 1.0 / max(1, node.usage_count + 1))))
        recency = self.recency_score(node)
        graph_centrality = clamp(self.store.degree(node.id) / policy.degree_saturation)
        code_priority = clamp(float(props.get("cleanup_rank", 0.0) or props.get("priority", 0.0) or 0.0) / policy.priority_saturation)
        evidence_count = clamp(node.evidence_count / policy.evidence_saturation)
        confidence = clamp(node.confidence)
        salience_score = clamp(
            policy.novelty_weight * novelty
            + policy.recency_weight * recency
            + policy.centrality_weight * graph_centrality
            + policy.code_priority_weight * code_priority
            + policy.evidence_weight * evidence_count
            + policy.confidence_weight * confidence
        )
        if node.status in INACTIVE_STATUSES:
            salience_score *= policy.inactive_factor
        return {
            "input": {
                "novelty": novelty,
                "recency": recency,
                "graph_centrality": graph_centrality,
                "code_priority": code_priority,
                "evidence_count": evidence_count,
            },
            "output": {"salience_score": clamp(salience_score)},
            "used_by": ["retrieval", "code_context"],
        }

    def compute_node_salience(self, node: MemoryNode) -> float:
        signal = self.compute_salience_signal(node)
        return clamp(
            self.policy.type_priors.get(node.type, self.policy.default_prior)
            + self.policy.signal_weight * float(signal["output"]["salience_score"])
            - self.policy.volatility_weight * clamp(node.volatility)
        )

    def recency_score(self, node: MemoryNode, *, half_life_days: float | None = None) -> float:
        days = self.policy.half_life_days if half_life_days is None else half_life_days
        half_life = max(1.0, days * 86400.0)
        return clamp(0.5 ** (seconds_since(node.updated_at) / half_life))
