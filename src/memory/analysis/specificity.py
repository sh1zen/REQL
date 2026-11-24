"""Semantic specificity and generic-node penalties."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from ..config import default_config
from ..config.models import SpecificityPolicy
from ..domain.models import MemoryNode
from ..storage.graph_store import GraphStore

@dataclass(frozen=True, slots=True)
class SpecificityScore:
    node_id: str
    specificity: float
    generic_penalty: float
    reasons: list[str]


class SpecificityScorer:
    def __init__(self, store: GraphStore, *, policy: SpecificityPolicy | None = None) -> None:
        self.store = store
        self.policy = policy if policy is not None else default_config().analysis.specificity

    def score(self, node: MemoryNode) -> SpecificityScore:
        """Score the node's vocabulary and complete neighborhood."""
        policy = self.policy
        text = " ".join(str(value or "") for value in (node.label, node.text, node.canonical_key, node.properties.get("name"), node.properties.get("qualified_name"))).strip()
        tokens = _tokens(text)
        reasons: list[str] = []
        specificity = policy.base_score
        penalty = 0.0

        if policy.concise_min_tokens <= len(tokens) <= policy.concise_max_tokens:
            specificity += policy.concise_bonus
            reasons.append("concise specific label")
        elif len(tokens) >= policy.overlong_min_tokens:
            penalty += policy.overlong_penalty
            reasons.append("overlong label")
        elif len(tokens) <= 1:
            specificity += policy.single_bonus if tokens and len(tokens[0]) >= policy.single_min_chars else 0.0

        if tokens:
            weak_ratio = sum(1 for token in tokens if _weak_token(token, policy.weak_max_chars)) / len(tokens)
            penalty += policy.weak_token_weight * weak_ratio
            if weak_ratio > policy.weak_ratio_threshold:
                reasons.append("weak token pattern")
            if any(_specific_token(token, policy.topical_min_chars) for token in tokens):
                specificity += policy.topical_bonus
                reasons.append("topical terms")

        if node.type in policy.specific_types:
            specificity += policy.specific_type_bonus
            reasons.append(f"specific node type {node.type}")
        if node.type in policy.generic_types:
            penalty += policy.generic_type_penalty
            reasons.append(f"generic node type {node.type}")
        if node.type == "CodeSymbol" and (node.properties.get("external") or node.properties.get("synthetic") or node.properties.get("kind") in {"external", "decorator"}):
            penalty += policy.external_symbol_penalty
            reasons.append("external code symbol")

        neighbor_types = _neighbor_type_counts(self.store, node)
        entropy = _entropy(list(neighbor_types.values()))
        if entropy > policy.entropy_threshold:
            penalty += policy.entropy_penalty
            reasons.append("high neighbor type entropy")
        elif neighbor_types:
            specificity += policy.focused_bonus
            reasons.append("focused neighborhood")

        degree = sum(neighbor_types.values())
        if degree > policy.high_degree_threshold and specificity < policy.low_focus_threshold:
            penalty += min(policy.max_degree_penalty, degree / policy.degree_saturation)
            reasons.append("high degree with low topical focus")

        artifact_type = str(node.properties.get("artifact_type") or "")
        if artifact_type in policy.generic_artifact_types:
            penalty += policy.generic_artifact_penalty
        elif artifact_type in policy.specific_artifact_types:
            specificity += policy.specific_artifact_bonus

        usage_success = float(node.properties.get("usage_success") or 0)
        if usage_success > 0:
            specificity += min(policy.max_usage_bonus, usage_success / policy.usage_saturation)

        return SpecificityScore(node.id, max(0.0, min(1.0, specificity)), max(0.0, min(1.0, penalty)), reasons or ["neutral specificity"])


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in re.findall(r"[A-Za-z0-9_]+", text)]


def _weak_token(token: str, max_chars: int) -> bool:
    return len(token) <= max_chars and not any(char.isdigit() for char in token) and "_" not in token


def _specific_token(token: str, min_chars: int) -> bool:
    return len(token) >= min_chars or any(char.isdigit() for char in token) or "_" in token


def _neighbor_type_counts(store: GraphStore, node: MemoryNode) -> Counter[str]:
    counts: Counter[str] = Counter()
    for edge in store.get_edges(from_id=node.id, limit=None, clone=False):
        neighbor = store.get_node(edge.to_id, clone=False)
        if neighbor:
            counts[neighbor.type] += 1
    for edge in store.get_edges(to_id=node.id, limit=None, clone=False):
        neighbor = store.get_node(edge.from_id, clone=False)
        if neighbor:
            counts[neighbor.type] += 1
    return counts


def _entropy(values: list[int]) -> float:
    total = sum(values)
    if total <= 0:
        return 0.0
    entropy = 0.0
    for value in values:
        p = value / total
        entropy -= p * math.log2(p)
    return entropy
