"""Neural-like spreading activation runtime."""
from __future__ import annotations

import heapq
from collections import defaultdict

from ..config import default_config
from ..config.models import ActivationPolicy
from ..domain.constants import INACTIVE_STATUSES
from ..domain.models import ActivationOptions, ActivationResult, MemoryEdge, MemoryNode
from ..extraction.normalization import clamp
from ..domain.timeutils import utcnow_iso
from ..storage.graph_store import GraphStore


class ActivationEngine:
    """Propagates activation through the property graph.

    The engine visits eligible adjacent edges from the seeds, applies inhibition
    via negative polarity and stores the resulting activation values.
    """

    def __init__(self, store: GraphStore, *, policy: ActivationPolicy | None = None) -> None:
        self.store = store
        self.policy = policy if policy is not None else default_config().analysis.activation

    def activate(self, seed_node_ids: list[str], options: ActivationOptions) -> ActivationResult:
        """Propagate activation across every eligible relationship at the requested depth."""
        if not seed_node_ids:
            return ActivationResult(active_nodes=[], fired_edges=[], activation_by_node={})

        policy = self.policy
        blocked = set(options.blocked_edge_types or ()).union(policy.blocked_edge_types)
        activation: dict[str, float] = defaultdict(float)
        best_depth: dict[str, int] = {}
        fired_edges: dict[str, MemoryEdge] = {}
        heap: list[tuple[float, int, str]] = []

        for seed_id in seed_node_ids:
            node = self.store.get_node(seed_id)
            if node is None or node.status in INACTIVE_STATUSES:
                continue
            value = clamp(max(options.seed_boost, node.base_activation, node.activation * options.persistence))
            activation[seed_id] = max(activation[seed_id], value)
            best_depth[seed_id] = 0
            heapq.heappush(heap, (-value, 0, seed_id))

        while heap:
            neg_value, depth, current_id = heapq.heappop(heap)
            current_value = -neg_value
            if options.max_depth is not None and depth >= options.max_depth:
                continue
            if current_value < options.min_activation:
                continue
            if abs(current_value - activation[current_id]) > 1e-9:
                continue

            neighbors = self.store.neighbors(
                current_id,
                direction="both" if options.traverse_incoming else "out",
                edge_types=options.allowed_edge_types,
                min_weight=policy.min_edge_weight,
                limit=None,
            )
            depth_factor = options.depth_decay ** (depth + 1)
            for edge, neighbor in neighbors:
                if edge.type in blocked:
                    continue
                if neighbor.status in INACTIVE_STATUSES:
                    continue
                if options.allowed_edge_types and edge.type not in options.allowed_edge_types:
                    continue
                status_factor = policy.latent_factor if neighbor.status in {"latent", "candidate"} else 1.0
                signal = current_value * edge.weight * edge.confidence * depth_factor * status_factor
                if edge.polarity < 0 or edge.type in policy.negative_edge_types:
                    signal *= -policy.negative_factor
                previous = activation.get(neighbor.id, 0.0)
                new_value = clamp(previous + signal)
                if abs(new_value - previous) <= 1e-9:
                    continue
                activation[neighbor.id] = new_value
                fired_edges[edge.id] = edge
                prev_depth = best_depth.get(neighbor.id)
                next_depth = depth + 1
                if prev_depth is None or next_depth < prev_depth or new_value >= options.min_activation:
                    best_depth[neighbor.id] = next_depth
                    heapq.heappush(heap, (-new_value, next_depth, neighbor.id))

        active_items = sorted(activation.items(), key=lambda item: item[1], reverse=True)
        active_nodes: list[MemoryNode] = []
        now = utcnow_iso()
        if options.update_store:
            if active_items:
                with self.store.transaction():
                    for node_id, value in active_items:
                        node = self.store.get_node(node_id)
                        if not node:
                            continue
                        blended = clamp(max(value, node.activation * options.persistence))
                        updated = self.store.update_node_fields(
                            node_id,
                            activation=blended,
                            last_activated_at=now,
                            usage_count=node.usage_count + 1,
                        )
                        if updated:
                            active_nodes.append(updated)
                    for edge in fired_edges.values():
                        self.store.update_edge_fields(
                            edge.id,
                            co_activation_count=edge.co_activation_count + 1,
                            last_fired_at=now,
                        )
        else:
            active_nodes = [n for n in self.store.get_nodes([node_id for node_id, _ in active_items]) if n]

        # Preserve score ordering after updates.
        active_nodes.sort(key=lambda node: activation.get(node.id, 0.0), reverse=True)
        return ActivationResult(
            active_nodes=active_nodes,
            fired_edges=list(fired_edges.values()),
            activation_by_node=dict(activation),
        )

