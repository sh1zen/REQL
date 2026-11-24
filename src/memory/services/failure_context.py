"""Recover relevant failed approaches from existing coordination records."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..artifacts.revision import RevisionRepository
from ..storage.graph_store import GraphStore
from .coordination import CoordinationStore, RELATIONS, TERMINAL, _paths, _tokens, source_work_text_match
from .retrieval.context.source import _source_snapshot


class FailureContextService:
    """Rank active failures by causal, file, verified symbol and text matches."""

    def __init__(self, work: CoordinationStore, graph: GraphStore | None = None) -> None:
        self.work = work
        self.graph = graph

    def context(self, query: str = "", *, files: list[str] | None = None, symbol_ids: list[str] | None = None,
                record_id: str | None = None, workstream: str | None = None, limit: int | None = None,
                records: list[dict[str, Any]] | None = None, _source_lookup: bool = False) -> dict[str, Any]:
        limit = self.work.context_limit(limit)
        if not isinstance(query, str):
            raise ValueError("Failure context query must be a string")
        if symbol_ids is not None and (not isinstance(symbol_ids, list)
                                       or any(not isinstance(value, str) or not value for value in symbol_ids)):
            raise ValueError("symbol_ids must be a list of nonempty ids")
        paths = set(_paths(files if files is not None else []))
        requested = set(symbol_ids or ())
        tokens = _tokens(query)
        candidates = self.work.records() if records is None else records
        by_id = {row["id"]: row for row in candidates}
        anchor = self.work._current_replacement(by_id, record_id)
        if record_id and record_id not in by_id:
            raise ValueError(f"Work record not found: {record_id}")
        causal = _related_ids(anchor) if anchor else set()
        anchor_id = anchor["id"] if anchor else None
        ranked = []
        for row in candidates:
            if (row["kind"] != "failure" or row["status"] in TERMINAL
                    or (workstream is not None and row["workstream"] != workstream)):
                continue
            connected = bool(anchor_id and (row["id"] == anchor_id or row["id"] in causal or anchor_id in _related_ids(row)))
            matched_paths = paths & set(row["files"])
            overlap = tokens & _tokens(f"{row['content']} {row['rationale']}")
            evidence = row.get("failure_evidence") or {}
            strong_text = (
                source_work_text_match(query, tokens, overlap) and len(overlap) * 2 >= len(tokens)
                if _source_lookup else bool(overlap)
            )
            direct_match = connected or matched_paths or (not record_id and strong_text)
            if not direct_match and not requested.intersection(evidence.get("symbol_ids", [])):
                continue
            verified, freshness = self._check_evidence(evidence, row["files"])
            matched_symbols = requested & set(verified.get("symbol_ids", []))
            if not (direct_match or matched_symbols):
                continue
            reasons = (["task or causal link"] if connected else [])
            if matched_paths:
                reasons.append("file match " + ", ".join(sorted(matched_paths)))
            if matched_symbols:
                reasons.append("verified symbol match")
            if overlap:
                reasons.append("text match " + ", ".join(sorted(overlap)))
            item = {
                "id": row["id"], "revision": row["revision"], "kind": "failure", "status": row["status"],
                "content": row["content"], "rationale": row["rationale"],
                "files": row["files"], "reasons": reasons, "freshness": freshness,
            }
            if verified:
                item["failure_evidence"] = verified
            ranked.append(((connected, len(matched_paths), len(matched_symbols), len(overlap),
                            freshness != "potentially_obsolete", row["updated_at"], row["id"]), item))
        selected = [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:limit]]
        revision = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
        return {"format": "reql-failure-context-v1", "revision": revision, "query": query,
                "records": selected, "total_matches": len(ranked), "omitted": len(ranked) - len(selected)}

    def _check_evidence(self, evidence: dict[str, Any], files: list[str]) -> tuple[dict[str, Any], str]:
        if not evidence or self.graph is None:
            return {}, "unverified" if evidence else "unknown"
        repository = RevisionRepository(self.graph)
        revision = repository.get(evidence["source_revision"]) if evidence.get("source_revision") else None
        if revision is None:
            return {}, "unverified"
        verified: dict[str, Any] = {"source_revision": revision.id}
        latest = repository.latest(revision.project_id)
        source_states: dict[str, str] = {}

        def source_state(path: str, artifact_id: str) -> str:
            if path not in source_states:
                artifact = self.graph.get_node(artifact_id, clone=False)
                source_states[path] = _source_snapshot(self.graph, artifact, latest)["status"]
            return source_states[path]

        for key in ("symbol_ids", "test_ids"):
            ids = []
            for item in evidence.get(key, []):
                node = self.graph.get_node(item, clone=False)
                if node and node.status == "active" and node.properties.get("project_id") == revision.project_id:
                    if key == "test_ids" and node.properties.get("context_scope") != "test":
                        continue
                    if key == "symbol_ids" and node.type not in {"Class", "Function", "Interface", "Method", "Variable", "Endpoint", "Schema"}:
                        continue
                    path = node.properties.get("relative_path")
                    entry = revision.manifest.get(path)
                    if (entry and latest and entry == latest.manifest.get(path)
                            and node.properties.get("artifact_id") == entry.get("artifact_id")
                            and source_state(path, entry["artifact_id"]) == "verified"):
                        ids.append(item)
            if ids:
                verified[key] = ids
        if evidence.get("observed_checks"):
            verified["observed_checks"] = list(evidence["observed_checks"])
        if not latest or not files:
            return verified, "unverified"
        checked = 0
        for path in files:
            old = revision.manifest.get(path)
            if old is None:
                continue
            if old != latest.manifest.get(path):
                return verified, "potentially_obsolete"
            state = source_state(path, old["artifact_id"])
            if state == "stale":
                return verified, "potentially_obsolete"
            checked += state == "verified"
        return verified, "current" if checked == len(files) else "unverified"


def _related_ids(row: dict[str, Any]) -> set[str]:
    return {target for relation in RELATIONS for target in
            ([row["parent"]] if relation == "parent" else row[relation]) if target}


def render_failure_context(payload: dict[str, Any]) -> str:
    """Display the selected records and their match evidence."""
    lines = ["## Relevant failures"]
    for item in payload["records"]:
        reasons = ", ".join(item["reasons"])
        lines.append(f"- {item['id']} r{item['revision']} ({item['freshness']}, {reasons})")
        if item.get("engineering_context_ref"):
            continue
        lines.append(f"  {item['content']}")
        if item.get("rationale"):
            lines.append(f"  {item['rationale']}")
    if not payload["records"]:
        lines.append("- No related active failures.")
    if payload.get("omitted"):
        lines.append(f"- {payload['omitted']} matching failure(s) omitted. Increase --limit to see more.")
    return "\n".join(lines)
