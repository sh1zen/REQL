"""Structured dictionary projections used by JSON-facing retrieval APIs."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterable, Sequence

from .....artifacts.context_scope import application_surface_kind
from .....code_analysis.markup import markup_structure
from .....domain.constants import INACTIVE_STATUSES
from .....domain.models import MemoryEdge, MemoryNode, MemoryQuery, MemorySubgraph, RankedNode
from ...common import (
    QUERY_EXPLORE_ALL_VIEWS,
    QUERY_EXPLORE_DEFAULT_VIEWS,
    QUERY_EXPLORE_VIEWS,
)


class JsonContextRendererMixin:
    def _node_context_payload(self, node: MemoryNode, subgraph: MemorySubgraph) -> dict[str, Any]:
        ranked = next((item for item in subgraph.ranked_nodes if item.node.id == node.id), None)
        payload = {
            "id": node.id,
            "type": node.type,
            "label": node.label,
            "text": self._compact_text(node.text or ""),
            "canonical_key": node.canonical_key,
            "status": node.status,
            "salience": node.salience,
            "confidence": node.confidence,
            "properties": dict(node.properties),
        }
        if ranked is not None:
            payload["score"] = ranked.score
            payload["reasons"] = dict(ranked.reasons)
        return payload

    def _ranked_payload(self, item: RankedNode) -> dict[str, Any]:
        return {
            "id": item.node.id,
            "type": item.node.type,
            "label": item.node.label,
            "text": self._compact_text(item.node.text or item.node.label or item.node.canonical_key or ""),
            "score": item.score,
            "reasons": dict(item.reasons),
        }

    def _edge_context_payload(self, edge: MemoryEdge, nodes: OrderedDict[str, MemoryNode]) -> dict[str, Any]:
        from_label = self._node_label(nodes[edge.from_id]) if edge.from_id in nodes else edge.from_id
        to_label = self._node_label(nodes[edge.to_id]) if edge.to_id in nodes else edge.to_id
        return {
            "id": edge.id,
            "type": edge.type,
            "directed": True,
            "direction": "outgoing",
            "from_id": edge.from_id,
            "from_label": from_label,
            "to_id": edge.to_id,
            "to_label": to_label,
            "source_id": edge.from_id,
            "source_label": from_label,
            "target_id": edge.to_id,
            "target_label": to_label,
            "weight": edge.weight,
            "confidence": edge.confidence,
            "polarity": edge.polarity,
            "origin": edge.origin,
            "properties": dict(edge.properties),
        }

    def _edge_direction_index(
        self,
        nodes: OrderedDict[str, MemoryNode],
        edges: Any,
        *,
        per_node_limit: int = 12,
    ) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for node_id, node in nodes.items():
            index[node_id] = {
                "node_id": node_id,
                "label": self._node_label(node),
                "incoming": [],
                "outgoing": [],
            }
        for edge in edges:
            if edge.from_id in index and edge.to_id in nodes and len(index[edge.from_id]["outgoing"]) < per_node_limit:
                index[edge.from_id]["outgoing"].append(self._direction_edge_ref(edge, nodes, direction="outgoing"))
            if edge.to_id in index and edge.from_id in nodes and len(index[edge.to_id]["incoming"]) < per_node_limit:
                index[edge.to_id]["incoming"].append(self._direction_edge_ref(edge, nodes, direction="incoming"))
        return {
            node_id: payload
            for node_id, payload in index.items()
            if payload["incoming"] or payload["outgoing"]
        }

    def _direction_edge_ref(
        self,
        edge: MemoryEdge,
        nodes: OrderedDict[str, MemoryNode] | dict[str, MemoryNode],
        *,
        direction: str,
    ) -> dict[str, Any]:
        other_id = edge.to_id if direction == "outgoing" else edge.from_id
        other = nodes.get(other_id)
        return {
            "edge_id": edge.id,
            "type": edge.type,
            "direction": direction,
            "from_id": edge.from_id,
            "to_id": edge.to_id,
            "other_id": other_id,
            "other_label": self._node_label(other) if other is not None else other_id,
            "weight": edge.weight,
            "confidence": edge.confidence,
        }

    def _direction_summary_lines(
        self,
        nodes: dict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        max_items: int,
    ) -> list[str]:
        incoming: dict[str, list[MemoryEdge]] = {}
        outgoing: dict[str, list[MemoryEdge]] = {}
        for edge in edges:
            if edge.from_id in nodes and edge.to_id in nodes:
                outgoing.setdefault(edge.from_id, []).append(edge)
                incoming.setdefault(edge.to_id, []).append(edge)
        lines: list[str] = []
        for node_id, node in nodes.items():
            node_in = incoming.get(node_id, [])
            node_out = outgoing.get(node_id, [])
            if not node_in and not node_out:
                continue
            lines.append(f"- {self._node_label(node)}: {len(node_out)} outgoing, {len(node_in)} incoming")
            if len(lines) >= max_items:
                break
        return lines

    @staticmethod
    def _normalize_query_explore_views(views: Sequence[str] | None) -> tuple[str, ...]:
        if not views:
            return QUERY_EXPLORE_DEFAULT_VIEWS
        normalized: list[str] = []
        aliases = {
            "all": "__all__",
            "owner": "owners",
            "owners_only": "owners",
            "caller": "callers",
            "callers_only": "callers",
            "surface": "public_surface",
            "public": "public_surface",
            "public_only": "public_surface",
            "public_surface_only": "public_surface",
            "serialization": "serialization_paths",
            "serialization_only": "serialization_paths",
            "serialization_paths_only": "serialization_paths",
            "docs": "docs_mentions",
            "docs_only": "docs_mentions",
            "docs_mentions_only": "docs_mentions",
            "duplicates": "structural_duplicates",
            "structural": "structural_duplicates",
            "structural_duplicates_only": "structural_duplicates",
            "code_only": "code",
        }
        for view in views:
            key = str(view).strip().casefold().replace("-", "_")
            if not key:
                continue
            value = aliases.get(key, key)
            if value == "__all__":
                return QUERY_EXPLORE_ALL_VIEWS
            if value not in QUERY_EXPLORE_VIEWS:
                valid = ", ".join(sorted(QUERY_EXPLORE_VIEWS | {"all"}))
                raise ValueError(f"unknown query_explore view '{view}'. Choose from: {valid}")
            if value not in normalized:
                normalized.append(value)
        return tuple(normalized or QUERY_EXPLORE_DEFAULT_VIEWS)

    def _query_explore_seed_nodes(self, subgraph: MemorySubgraph, *, limit: int) -> OrderedDict[str, MemoryNode]:
        seeds: OrderedDict[str, MemoryNode] = OrderedDict()
        for node_id in subgraph.seed_node_ids:
            node = self.store.get_node(node_id)
            if node is not None and self._is_code_context_node(node):
                seeds[node.id] = node
            if len(seeds) >= limit:
                return seeds
        for item in subgraph.ranked_nodes:
            if self._is_code_context_node(item.node):
                seeds.setdefault(item.node.id, item.node)
            if len(seeds) >= limit:
                break
        return seeds

    def _query_explore_owners(
        self,
        seed_ids: set[str],
        nodes: OrderedDict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_nodes: set[str] = set()
        for node_id in seed_ids:
            node = nodes.get(node_id)
            if node is None or not self._is_owner_symbol_node(node):
                continue
            seen_nodes.add(node.id)
            rows.append(
                {
                    "role": "owner",
                    "owner": self._query_explore_node_payload(node),
                    "target": self._query_explore_node_payload(node),
                    "edge": None,
                    "reason": "seed owner symbol",
                }
            )
            if len(rows) >= limit:
                return rows
        seen: set[tuple[str, str]] = set()
        for edge in edges:
            if edge.type not in self.policy.explore.owner_edges or edge.to_id not in seed_ids:
                continue
            owner = nodes.get(edge.from_id)
            target = nodes.get(edge.to_id)
            if owner is None or target is None:
                continue
            if owner.id in seen_nodes:
                continue
            if target.id in seen_nodes and owner.type in {"File", "SourceArtifact"}:
                continue
            key = (owner.id, edge.id)
            if key in seen:
                continue
            seen.add(key)
            seen_nodes.add(owner.id)
            rows.append(
                {
                    "role": "owner",
                    "owner": self._query_explore_node_payload(owner),
                    "target": self._query_explore_node_payload(target),
                    "edge": self._query_explore_edge_payload(edge, nodes),
                    "reason": f"{edge.type} incoming to seed",
                }
            )
            if len(rows) >= limit:
                break
        if rows:
            return rows
        for node_id in seed_ids:
            node = nodes.get(node_id)
            if node is None:
                continue
            rows.append(
                {
                    "role": "owner",
                    "owner": self._query_explore_node_payload(node),
                    "target": self._query_explore_node_payload(node),
                    "edge": None,
                    "reason": "seed owner symbol",
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _is_owner_symbol_node(self, node: MemoryNode) -> bool:
        return node.type in self.policy.explore.owner_nodes

    def _query_explore_callers(
        self,
        seed_ids: set[str],
        nodes: OrderedDict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for edge in edges:
            if edge.type not in self.policy.explore.caller_edges or edge.to_id not in seed_ids:
                continue
            caller = nodes.get(edge.from_id)
            target = nodes.get(edge.to_id)
            if caller is None or target is None or edge.id in seen:
                continue
            seen.add(edge.id)
            rows.append(
                {
                    "role": "caller",
                    "caller": self._query_explore_node_payload(caller),
                    "target": self._query_explore_node_payload(target),
                    "edge": self._query_explore_edge_payload(edge, nodes),
                    "reason": f"incoming {edge.type}",
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _query_explore_public_surface(
        self,
        seed_ids: set[str],
        nodes: OrderedDict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None]] = set()
        for edge in edges:
            if edge.type in self.policy.explore.public_surface_edges and (edge.from_id in seed_ids or edge.to_id in seed_ids):
                node = nodes.get(edge.from_id if edge.from_id not in seed_ids else edge.to_id)
                if node is None:
                    continue
                key = (node.id, edge.id)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "surface": self._query_explore_node_payload(node),
                        "edge": self._query_explore_edge_payload(edge, nodes),
                        "reason": f"{edge.type} near seed",
                    }
                )
            if len(rows) >= limit:
                return rows
        for node_id in seed_ids:
            node = nodes.get(node_id)
            if node is None or not self._is_public_surface_node(node):
                continue
            key = (node.id, None)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "surface": self._query_explore_node_payload(node),
                    "edge": None,
                    "reason": "seed is public API-shaped",
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _query_explore_serialization_paths(
        self,
        query: MemoryQuery,
        seed_ids: set[str],
        nodes: OrderedDict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None]] = set()
        frontier = set(seed_ids)
        visited = set(seed_ids)
        if not self.policy.explore.serialization_edges:
            return rows
        prefetched = edges
        depth = 0
        max_depth = max(1, query.max_depth) if query.max_depth is not None else None
        while frontier and (max_depth is None or depth < max_depth):
            depth += 1
            current_edges = prefetched if depth == 1 else self.store.incident_edges(
                sorted(frontier),
                edge_types=set(self.policy.explore.serialization_edges),
                limit=max(1000, limit * 120),
            )
            next_frontier: set[str] = set()
            for edge in current_edges:
                if edge.type not in self.policy.explore.serialization_edges:
                    continue
                if edge.from_id in frontier:
                    other_id = edge.to_id
                    direction = "outgoing"
                elif edge.to_id in frontier:
                    other_id = edge.from_id
                    direction = "incoming"
                else:
                    continue
                node = nodes.get(other_id) or self.store.get_node(other_id)
                if node is None:
                    continue
                nodes.setdefault(node.id, node)
                key = (node.id, edge.id)
                if key not in seen:
                    seen.add(key)
                    rows.append(
                        {
                            "node": self._query_explore_node_payload(node),
                            "edge": self._query_explore_edge_payload(edge, nodes),
                            "depth": depth,
                            "direction": direction,
                            "reason": f"{edge.type} data-flow edge at depth {depth}",
                        }
                    )
                    if len(rows) >= limit:
                        return rows
                if other_id not in visited:
                    visited.add(other_id)
                    next_frontier.add(other_id)
            frontier = next_frontier
        return rows

    def _query_explore_docs_mentions(
        self,
        query: MemoryQuery,
        seed_ids: set[str],
        nodes: OrderedDict[str, MemoryNode],
        edges: list[MemoryEdge],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None]] = set()
        for edge in edges:
            if edge.type not in self.policy.explore.docs_edges:
                continue
            if edge.from_id not in seed_ids and edge.to_id not in seed_ids:
                continue
            other_id = edge.to_id if edge.from_id in seed_ids else edge.from_id
            node = nodes.get(other_id)
            if node is None or not self._is_docs_mention_node(node):
                continue
            key = (node.id, edge.id)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "mention": self._query_explore_node_payload(node),
                    "edge": self._query_explore_edge_payload(edge, nodes),
                    "reason": f"{edge.type} linked source mention",
                }
            )
            if len(rows) >= limit:
                return rows
        if not self.policy.explore.docs_nodes:
            return rows
        for node, score in self.store.lexical_search(
            query.text, top_k=max(limit * 2, 20), node_types=set(self.policy.explore.docs_nodes),
            include_archived=query.include_archived,
        ):
            if not self._is_docs_mention_node(node):
                continue
            key = (node.id, None)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "mention": self._query_explore_node_payload(node),
                    "edge": None,
                    "score": score,
                    "reason": "document source lexical match",
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _query_explore_structural_duplicates(
        self,
        seed_nodes: Sequence[MemoryNode],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        policy = self.policy.explore
        seed_paths = {
            (str(node.properties.get("project_id") or ""), path)
            for node in seed_nodes
            if (path := self._node_relative_path(node)) and application_surface_kind(path) == "template"
        }
        if not seed_paths:
            return []

        projects = {project for project, _ in seed_paths}
        nodes: Iterable[MemoryNode]
        if "" in projects:
            nodes = self.store.find_nodes_by_types(("File", "SourceArtifact", "SourceFragment"), clone=False)
        else:
            nodes = (
                node for project in sorted(projects) for kind in ("File", "SourceArtifact", "SourceFragment")
                for node in self.store.find_nodes_by_property("project_id", project, type_=kind, limit=None, clone=False)
            )

        fragments: dict[tuple[str, str], list[MemoryNode]] = {}
        representatives: dict[tuple[str, str], MemoryNode] = {}
        for node in nodes:
            if node.status in INACTIVE_STATUSES:
                continue
            path = self._node_relative_path(node)
            if not path or application_surface_kind(path) != "template":
                continue
            key = (str(node.properties.get("project_id") or ""), path)
            if key[0] not in projects:
                continue
            current = representatives.get(key)
            if current is None or self._template_representative_rank(node) > self._template_representative_rank(current):
                representatives[key] = node
            if node.type == "SourceFragment" and node.text:
                fragments.setdefault(key, []).append(node)

        signatures: dict[tuple[str, str], tuple[int, set[str]]] = {}
        for path, items in fragments.items():
            ordered = sorted(items, key=lambda item: int(item.properties.get("line_start") or 0))
            signatures[path] = markup_structure("\n".join(item.text or "" for item in ordered))

        rows: list[dict[str, Any]] = []
        seen_pairs: set[tuple[tuple[str, str], tuple[str, str]]] = set()
        for source_path in sorted(seed_paths):
            source_signature = signatures.get(source_path)
            source_node = representatives.get(source_path)
            if source_signature is None or source_node is None:
                continue
            source_count, source_features = source_signature
            if source_count < policy.duplicate_min_tags or not source_features:
                continue
            for duplicate_path, (duplicate_count, duplicate_features) in signatures.items():
                if (duplicate_path == source_path or duplicate_path[0] != source_path[0]
                        or duplicate_count < policy.duplicate_min_tags):
                    continue
                pair = (source_path, duplicate_path) if source_path < duplicate_path else (duplicate_path, source_path)
                if pair in seen_pairs:
                    continue
                shared = source_features & duplicate_features
                similarity = len(shared) / max(1, len(source_features | duplicate_features))
                if similarity < policy.duplicate_similarity or len(shared) < policy.duplicate_min_features:
                    continue
                duplicate_node = representatives.get(duplicate_path)
                if duplicate_node is None:
                    continue
                seen_pairs.add(pair)
                shared_patterns = [
                    pattern
                    for prefix in ("sequence:", "edge:", "node:")
                    for pattern in sorted(item for item in shared if item.startswith(prefix))[:3]
                ]
                rows.append(
                    {
                        "source": self._query_explore_node_payload(source_node),
                        "duplicate": self._query_explore_node_payload(duplicate_node),
                        "similarity": round(similarity, 4),
                        "source_tag_count": source_count,
                        "duplicate_tag_count": duplicate_count,
                        "shared_patterns": shared_patterns[:8],
                        "reason": "shared markup hierarchy and tag sequence",
                    }
                )
        rows.sort(
            key=lambda row: (
                -float(row["similarity"]),
                str(row["source"].get("location") or ""),
                str(row["duplicate"].get("location") or ""),
            )
        )
        return rows[:limit]

    @staticmethod
    def _template_representative_rank(node: MemoryNode) -> int:
        return {"File": 3, "SourceArtifact": 2, "SourceFragment": 1}.get(node.type, 0)


    def _query_explore_followups(self, query_text: str, views: Sequence[str]) -> list[dict[str, str]]:
        query = self._reql_string(query_text)
        followups = [
                {
                    "label": "Full dependency slices",
                    "command": f"reql query_explore --query {query} --json",
                    "purpose": "all dependency-oriented views",
                },
                {
                    "label": "Structured graph",
                    "command": f"reql query_graph --query {query} --max-depth 3 --json",
                    "purpose": "expanded edge details",
                },
        ]
        for view in views:
            followups.append(
                {
                    "label": f"{view} only",
                    "command": f"reql query_explore --query {query} --view {view} --json",
                    "purpose": f"{view} view only",
                }
            )
            if len(followups) >= 6:
                break
        return followups

