"""Application service shared by Python, CLI, and MCP query-context adapters."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from config import VERSION

from .coordination import OBSOLETE_STATUSES, CoordinationStore, render_file_overlaps, render_work_context
from .failure_context import FailureContextService, render_failure_context

from ..config import REQLConfig, default_config
from ..domain.models import MemoryEdge, MemoryNode, MemoryQuery, MemorySubgraph, RankedNode
from ..domain.query_context import (
    CONTEXT_RESULT_SCHEMA_VERSION,
    Confidence,
    ContextResult,
    GraphFreshness,
    QueryContextRequest,
    SeenSourceRange,
    SourceRevision,
)
from ..artifacts.revision import ProjectRevision, RevisionRepository
from ..storage.graph_store import GraphStore
from ..shell import shell_option
from .retrieval import RetrievalEngine
from .retrieval.context.source import (
    _cached_source_snapshot,
    assigned_document_outlines,
    brief_task_next_action,
    capture_assigned_documentation,
    capture_source_excerpts,
    capture_task_edit_spans,
    capture_task_test_file,
    complete_pinned_task_file,
    limit_brief_source,
    pinned_code_context,
    task_setup_location,
    reuse_task_source,
)


class QueryContextService:
    """Own query-context retrieval, projection, metadata, and rendering."""

    def __init__(self, retrieval: RetrievalEngine, *, config: REQLConfig | None = None) -> None:
        self.retrieval = retrieval
        self.config = config or default_config()

    def _focus_assigned_source(
        self, payload: dict[str, Any], query: MemoryQuery, paths: list[str], revision: ProjectRevision,
        snapshots: dict[str, dict[str, Any]],
        seen_source: tuple[SeenSourceRange, ...] = (),
    ) -> set[str]:
        """Prefer the complete source of exact symbols in assigned files."""
        reads = self.retrieval.assigned_exact_reads(query, paths, revision)
        if not reads:
            return set()
        # Reuse an exact span. A containing body can include unrelated definitions.
        prior = [row for row in payload["snippets"]
                 if any(row["path"] == read["path"]
                        and row.get("artifact_id") == revision.manifest[read["path"]]["artifact_id"]
                        and row["line_start"] == read["line_start"] and row["line_end"] == read["line_end"]
                        for read in reads)]
        shown = [row for row in prior if row.get("source_status") == "verified"]
        pending = [read for read in reads if not any(
            row["path"] == read["path"] and row["line_start"] <= read["line_start"]
            and read["line_end"] <= row["line_end"] for row in shown
        )]
        extra = capture_source_excerpts(
            self.retrieval.store, pending, complete_only=True, snapshots=snapshots,
            revisions={revision.project_id: revision},
            max_chars=self.retrieval.policy.source.brief_max_source_chars,
            seen_source=seen_source,
        )
        selected = [*shown, *extra]
        payload["snippets"] = selected
        payload["task_focus_ranges"] = reads
        payload["task_source_paths"] = paths
        if selected:
            payload["source_mode"] = "verified"
        if not shown:
            payload.pop("linked_section", None)
        return {row["path"] for row in selected if row.get("source_status") == "stale"}

    def execute(
        self, request: QueryContextRequest, *, brief: bool = False, follow_up: bool = False,
        include_previews: bool = True,
    ) -> ContextResult:
        """Resolve context with request-local source evidence shared by its consumers."""
        if request.seen_source and not (brief and follow_up and request.task_id):
            raise ValueError("seen_source requires an assigned follow-up brief")
        store = self.retrieval.store
        storage_path = getattr(store, "path", None)
        work = CoordinationStore.for_project(storage_path, config=self.config) if storage_path is not None else None
        records = None
        assigned_task = None
        project_ids = None
        if request.task_id is not None:
            if work is None:
                raise ValueError("Task recovery requires project work storage")
            records = None if follow_up else work.records()
            by_id = {record["id"]: record for record in records} if records is not None else None
            assignment = work.show(request.task_id) if by_id is None else by_id.get(request.task_id)
            if assignment is None:
                raise ValueError(f"Work record not found: {request.task_id}")
            assigned_task = assignment
            if assignment["status"] in OBSOLETE_STATUSES and assignment.get("replaced_by"):
                if by_id is None:
                    by_id = {record["id"]: record for record in work.records()}
                assigned_task = work._current_replacement(by_id, request.task_id)
            project_assignment = assigned_task or assignment
            if project_assignment["kind"] == "task":
                project_ids = _assigned_projects(store, project_assignment["files"])
        brief_source_chars = self.retrieval.policy.source.brief_max_source_chars
        query = MemoryQuery(
            text=request.text,
            top_k=request.budget.top_k,
            max_depth=request.budget.max_depth,
            include_archived=request.include_archived,
            context_scopes=set(request.scopes) or None,
            project_ids=project_ids,
        )
        subgraph = self.retrieval.retrieve(query)
        revision = _source_revision(subgraph, store) if project_ids is None else next(
            (RevisionRepository(store).latest(project_id) for project_id in project_ids), None,
        )
        freshness = GraphFreshness(status="current" if revision is not None else "unknown")
        source_revision = SourceRevision(revision.id, revision.sequence, revision.tree_hash) if revision else None
        source_snapshots: dict[str, dict[str, Any]] = {}
        payload = dict(
            self.retrieval.query_context_payload(
                subgraph,
                max_items=request.budget.max_items,
                query_mode=request.mode.value,
                query_scopes=tuple(sorted(request.scopes)),
                include_source=request.include_source,
                include_previews=include_previews and not request.seen_source,
                brief=brief,
                source_snapshots=source_snapshots,
                source_revisions={revision.project_id: revision} if revision else {},
                seen_source=request.seen_source,
            )
        )
        confidence_payload = payload.pop("confidence", None)
        if not isinstance(confidence_payload, dict):
            raise ValueError("query_context projection did not return confidence metadata")
        payload.update(
            {
                "trace_id": subgraph.trace_id,
                "ranked_nodes": len(subgraph.ranked_nodes),
                "seed_node_ids": list(subgraph.seed_node_ids),
                "ranking_profile": VERSION,
            }
        )
        stale_paths = {item.get("path") for item in payload.get("snippets", []) if item.get("source_status") == "stale"}
        if not request.include_source and revision is not None and not brief:
            stale_paths.update(_stale_working_paths(store, revision, payload, source_snapshots))
            if stale_paths:
                payload["snippets"] = [row for row in payload.get("snippets", []) if row.get("path") not in stale_paths]
        if (follow_up and brief and not request.include_source and revision is not None
                and assigned_task is not None and assigned_task["kind"] == "task"
                and payload["kind"] == "code" and payload.get("query_mode") == "informative"
                and not payload.get("exact_paths")):
            stale_paths.update(self._focus_assigned_source(
                payload, query, _assigned_context_paths(store, assigned_task["files"], revision, query),
                revision, source_snapshots, request.seen_source,
            ))
        if work is not None and not follow_up:
            # The code projection already selected the relevant owners. Incidental
            # ranked neighbors must not pull unrelated work into that working set.
            if payload.get("kind") == "code":
                files = list(dict.fromkeys(str(row["path"]) for key in ("snippets", "working_set")
                                           for row in payload.get(key, []) if row.get("path")))
            else:
                files = list(dict.fromkeys(str(item.node.properties["relative_path"]) for item in subgraph.ranked_nodes
                                           if item.node.properties.get("relative_path")))
            if records is None:
                records = work.records()
            payload["engineering_context"] = work.context(
                request.text, files=files, record_id=request.task_id,
                _records=records, _source_lookup=brief,
            )
            if request.task_id is not None:
                payload["task_id"] = request.task_id
                assigned = next((row for row in payload["engineering_context"]["records"]
                                 if "task" in row["reasons"]), None)
                if assigned and assigned["kind"] == "task" and assigned["status"] in {"open", "in_progress"}:
                    overlaps = work.file_overlaps(assigned, records)
                    if overlaps["files"]:
                        payload["file_overlaps"] = overlaps
                if (brief and not request.include_source and revision is not None
                        and payload["kind"] == "code" and assigned and assigned["kind"] == "task"
                        and (assigned["execution_state"] in {"ready", "in_progress"}
                             or (assigned["status"] == "in_progress" and assigned["execution_state"] == "blocked"
                                 and assigned["waiting_on"]))):
                    allow_code = not request.scopes or "code" in request.scopes
                    task_paths = _assigned_context_paths(store, assigned["files"], revision, query)
                    payload["task_source_paths"] = task_paths
                    test_source = capture_task_test_file(
                        store, task_paths, revision, source_snapshots,
                        shown_paths={row["path"] for row in payload["snippets"]},
                        max_chars=brief_source_chars,
                    )
                    if test_source is not None:
                        payload["task_test_source"] = test_source
                    shown = list(payload["snippets"])
                    if test_source is not None:
                        shown.append(test_source)
                    edit_spans = capture_task_edit_spans(
                        store, task_paths, assigned["next_action"],
                        revision, source_snapshots,
                        max_chars=brief_source_chars,
                    )
                    if edit_spans:
                        payload["task_edit_spans"] = {"spans": edit_spans, "omitted": 0}
                    elif payload.get("query_mode") == "informative" and not payload.get("exact_paths"):
                        # Explicit edit spans drive the pinned-source workflow.
                        stale_paths.update(self._focus_assigned_source(
                            payload, query, task_paths, revision, source_snapshots,
                        ))
                        shown = [*payload["snippets"], *([test_source] if test_source else [])]
                    whole = complete_pinned_task_file(
                        store, revision, source_snapshots, edit_spans, max_chars=brief_source_chars,
                    )
                    if whole is not None:
                        payload["task_source_file"] = whole
                        payload["source_mode"] = "verified"
                    covered = (*shown, *([whole] if whole else []))
                    owner, local_reads = pinned_code_context(
                        self.retrieval.store, revision, source_snapshots, edit_spans,
                        f"{assigned['content']}\n{assigned['next_action']}", shown=covered,
                        max_chars=brief_source_chars,
                    )
                    if owner is not None:
                        payload["task_owner_source"] = owner
                    local_source = capture_source_excerpts(
                        self.retrieval.store, local_reads, complete_only=True, snapshots=source_snapshots,
                        revisions={revision.project_id: revision},
                        max_chars=brief_source_chars,
                    )
                    if local_source:
                        payload["task_local_source"] = local_source
                    doc_coverage = (*covered, *edit_spans)
                    if documentation := capture_assigned_documentation(
                        store, task_paths, assigned["content"], revision, source_snapshots,
                        shown=doc_coverage, max_chars=brief_source_chars,
                    ):
                        payload["task_documentation_source"] = documentation
                    elif outlines := assigned_document_outlines(
                        store, task_paths, revision, source_snapshots, shown=doc_coverage,
                        max_items=request.budget.max_items,
                    ):
                        if len(outlines) == 1:
                            payload["task_documentation_outline"] = outlines[0]
                        else:
                            payload["task_documentation_outlines"] = outlines
                    if (allow_code and payload.get("query_mode") == "informative" and not payload.get("task_focus_ranges")
                            and not payload.get("exact_paths")
                            and not any(row.get("text") for row in payload["snippets"])):
                        read = self.retrieval.assigned_source_read(request.text, task_paths, revision,
                                                                   (*shown, *edit_spans))
                        if read is not None:
                            excerpts = capture_source_excerpts(
                                self.retrieval.store, [read], complete_only=True, snapshots=source_snapshots,
                                revisions={revision.project_id: revision},
                                max_chars=brief_source_chars,
                            )
                            if excerpts:
                                payload["snippets"].extend(excerpts)
                                payload["source_mode"] = "verified"
                                if excerpts[0]["source_status"] == "stale":
                                    freshness = replace(freshness, status="stale",
                                                        pending_paths=max(freshness.pending_paths, 1))
                    companion_paths = set(task_paths) - ({whole["path"]} if whole else set())
                    declaration = self.retrieval.assigned_declaration_read(
                        payload["snippets"], companion_paths, f"{assigned['content']} {assigned['next_action']}",
                    )
                    if declaration is not None:
                        payload["snippets"].extend(capture_source_excerpts(
                            self.retrieval.store, [declaration], complete_only=True, snapshots=source_snapshots,
                            revisions={revision.project_id: revision},
                            max_chars=brief_source_chars,
                        ))
                    reuse_task_source(payload["snippets"], payload.get("linked_section"), revision,
                                      source_snapshots, shown=(*shown, *edit_spans),
                                      include_guidance=not any("pinned_line_end" in row for row in edit_spans),
                                      max_chars=brief_source_chars)
                    dependencies = set(assigned["depends_on"])
                    prerequisites = [row for row in payload["engineering_context"]["records"]
                                     if row["id"] in dependencies and row["kind"] in {"decision", "constraint"}
                                     and row["execution_state"] in {"active", "resolved", "done"}]
                    if allow_code and prerequisites:
                        contract_reads = self.retrieval.decision_contract_reads(
                            prerequisites, revision, [*payload["snippets"], *covered, *edit_spans, *local_source,
                                                      *([owner] if owner else [])],
                        )
                        contracts = capture_source_excerpts(
                            self.retrieval.store, contract_reads, complete_only=True, snapshots=source_snapshots,
                            revisions={revision.project_id: revision},
                            max_chars=brief_source_chars,
                        )
                        verified = [row for row in contracts if row["source_status"] == "verified"]
                        if verified:
                            payload["task_contract_source"] = verified
                    if allow_code and (setup := task_setup_location(
                        self.retrieval.store, revision, source_snapshots,
                        excluded_paths=assigned["files"], policy=self.retrieval.policy.source,
                    )):
                        payload["task_setup_location"] = setup
            if any(row["kind"] == "failure" for row in records):
                owners = [str(row["id"]) for row in payload.get("owner_candidates", []) if row.get("id")]
                failure = FailureContextService(work, self.retrieval.store).context(
                    request.text, files=files if request.task_id is None else None,
                    symbol_ids=owners if request.task_id is None else None, records=records,
                    record_id=request.task_id,
                    _source_lookup=brief,
                )
                existing = {row["id"] for row in payload["engineering_context"]["records"]}
                for row in failure["records"]:
                    if row["id"] in existing:
                        row.pop("content")
                        row.pop("rationale")
                        row["engineering_context_ref"] = row["id"]
                if failure["records"]:
                    payload["failure_context"] = failure
        if brief and not request.include_source and revision is not None:
            stale_paths.update(_stale_working_paths(store, revision, payload, source_snapshots))
        if stale_paths:
            freshness = replace(freshness, status="stale", pending_paths=len(stale_paths))
        if brief and not request.include_source and payload.get("kind") == "code":
            limit_brief_source(payload, brief_source_chars)
            if confidence_payload["status"] == "insufficient" and payload.get("task_focus_ranges") and any(
                row.get("source_status") == "verified" for row in payload.get("snippets", ())
            ):
                confidence_payload.update(
                    status="sufficient", targeted_rg_fallback_allowed=False,
                    reason="exact assigned symbol ranges match verified source",
                )
        if request.seen_source and payload.get("snippets"):
            payload["snippets"] = [row for row in payload["snippets"] if not row.get("already_seen")]
        return ContextResult(
            schema_version=CONTEXT_RESULT_SCHEMA_VERSION,
            graph_revision=_context_graph_revision(subgraph),
            confidence=Confidence.from_payload(confidence_payload),
            payload=payload,
            source_revision=source_revision,
            freshness=freshness,
        )

    def render(self, result: ContextResult, *, brief: bool = False, completion_command: str | None = None,
               allow_work_commands: bool = True) -> str:
        """Render agent text without altering the structured result."""
        payload = dict(result.payload)
        payload["confidence"] = result.confidence.to_dict()
        shown: list[dict[str, Any]] = []
        rendered = self.retrieval.render_context_payload(payload, brief=brief, displayed_source=shown if brief else None)
        work = payload.get("engineering_context")
        failures = payload.get("failure_context")
        failure_rows = failures["records"] if failures else []
        complete_records: set[tuple[str, int]] = set()
        failure_notes = {
            (row["id"], row["revision"]): f"evidence {row['freshness'].replace('_', ' ')}"
            for row in failure_rows if brief and row.get("engineering_context_ref")
        }
        if work and work["records"]:
            task_id = payload.get("task_id")
            if task_id:
                if brief and result.confidence.status == "sufficient" and payload.get("source_mode") == "verified":
                    work = {**work, "records": [
                        {**row, "next_action": brief_task_next_action(row["next_action"], shown)}
                        if "task" in row["reasons"] and row["kind"] == "task" else row
                        for row in work["records"]
                    ]}
                handoff = render_work_context(work, record_id=task_id,
                                              complete_records=complete_records, record_notes=failure_notes)
                overlaps = payload.get("file_overlaps") if brief else None
                if overlaps:
                    handoff += "\n\n" + render_file_overlaps(overlaps)
                if brief:
                    assigned = next((row for row in work["records"] if "task" in row["reasons"]), None)
                    if (assigned and assigned["kind"] == "task"
                            and assigned["execution_state"] in {"ready", "in_progress"}
                            and (assigned["id"], assigned["revision"]) in complete_records
                            and set(assigned["depends_on"]) <= {record_id for record_id, _ in complete_records}):
                        handoff += "\nSaved task and direct prerequisites shown in full."
                if brief and (setup := payload.get("task_setup_location")):
                    handoff += (f"\n\nIf setup is needed, read the relevant lines in `{setup['path']}` "
                                f"[{setup['line_start']}-{setup['line_end']}] ({setup['title']}).")
                if (allow_work_commands and result.source_revision is not None and result.freshness.status == "current"
                        and any(row.get("source_status") == "verified" and row.get("text") for row in shown)):
                    handoff += (
                        "\n\nApply shown line edits and refresh with `reql edit --plan -`. "
                        "Pipe a JSON array to stdin, even for one edit. "
                        "Use `{path,start_line,end_line,text}` for a verified range or `{path,find,text}` for unique complete lines."
                    )
                if allow_work_commands and completion_command:
                    handoff += (
                        "\n\nPass focused project checks before running broader checks. "
                        "After direct source edits, refresh with `reql project compile --lock-timeout 60`. "
                        f"Record the outcome and complete the task with `{completion_command}`."
                    )
                if brief and (completion_command or (shown and assigned and assigned["kind"] == "task") or (
                    assigned and assigned["status"] == "in_progress" and assigned["waiting_on"]
                    and not assigned["conflicts"] and not assigned["obsolete_dependencies"]
                )):
                    ranges = dict.fromkeys(
                        SeenSourceRange(row["path"], row["line_start"], row["line_end"], row["sha256"])
                        for row in shown
                    )
                    seen_flags = "".join(f" {shell_option('--seen-source', str(span))}" for span in ranges)
                    follow_up_command = f"reql query_context --task {task_id} --follow-up"
                    if overlaps:
                        follow_up_command += " --lock-timeout 60"
                    handoff += (f'\n\nFor missing code or tests: `{follow_up_command} '
                                f'--query "symbol or path" --code --test --brief{seen_flags}`.\n'
                                f'For missing documentation: `{follow_up_command} '
                                '--query "document path or heading" --docs --brief`.')
                rendered = handoff + "\n\n" + rendered
            else:
                rendered += "\n\n" + render_work_context(work, complete_records=complete_records,
                                                         record_notes=failure_notes, brief=brief)
        if failure_rows:
            work_by_revision = {(row["id"], row["revision"]): row for row in work["records"]} if work else {}
            visible_failures = []
            for row in failure_rows:
                key = (row["id"], row["revision"])
                if key in complete_records and key in failure_notes:
                    continue
                if row.get("engineering_context_ref") and key not in complete_records and key in work_by_revision:
                    source = work_by_revision[key]
                    row = {**row, "engineering_context_ref": None,
                           "content": source["content"], "rationale": source["rationale"]}
                visible_failures.append(row)
            if visible_failures:
                rendered += "\n\n" + render_failure_context({**failures, "records": visible_failures})
        if brief and result.freshness.status == "current":
            return rendered
        source = result.source_revision.id if result.source_revision else "unknown"
        return f"Graph freshness: {result.freshness.status}; source_revision={source}\n\n{rendered}"


def _stale_working_paths(
    store: GraphStore, revision: ProjectRevision, payload: dict[str, Any],
    snapshots: dict[str, dict[str, Any]],
) -> set[str]:
    """Check working-set freshness without loading bodies for location-only rows."""
    stale = set()
    for row in payload.get("working_set", ()):
        entry = revision.manifest.get(row["path"])
        if entry is None:
            continue
        artifact = store.get_node(entry["artifact_id"], clone=False)
        snapshot = _cached_source_snapshot(store, artifact, revision, snapshots, verify_only=True)
        if snapshot["status"] == "stale":
            stale.add(row["path"])
    return stale


def _source_revision(subgraph: MemorySubgraph, store: Any) -> ProjectRevision | None:
    """Find the committed revision represented by the retrieved nodes."""
    project_ids = {
        str(node.properties.get("project_id") or (node.id if node.type == "Project" else ""))
        for node in subgraph.nodes
    }
    project_ids.discard("")
    revisions = RevisionRepository(store)
    return next((revision for project_id in sorted(project_ids) if (revision := revisions.latest(project_id))), None)


def _assigned_projects(store: GraphStore, paths: list[str]) -> frozenset[str] | None:
    """Find the project common to the indexed paths in the assignment."""
    candidates: set[str] | None = None
    for path in paths:
        matches = {
            project_id for artifact in store.find_nodes_by_property(
                "relative_path", path, type_="SourceArtifact", limit=None, clone=False,
            )
            if artifact.status == "active" and isinstance(project_id := artifact.properties.get("project_id"), str)
        }
        if matches:
            candidates = matches if candidates is None else candidates & matches
            if not candidates:
                return frozenset()
    if candidates is None:
        candidates = {node.id for node in store.find_nodes_by_types(("Project",), clone=False)
                      if node.status == "active"}
        if not candidates:
            return None
    return frozenset(candidates) if len(candidates) == 1 else frozenset()


def _assigned_context_paths(store: GraphStore, paths: list[str], revision: ProjectRevision, query: MemoryQuery) -> list[str]:
    """Select assigned files for source recovery, including companions for code briefs."""
    scopes = query.context_scopes
    if not scopes or "code" in scopes:
        return paths
    return [
        path for path in paths if (entry := revision.manifest.get(path))
        and (artifact := store.get_node(entry["artifact_id"], clone=False)) is not None
        and artifact.properties.get("context_scope") in scopes
    ]


def _context_graph_revision(subgraph: MemorySubgraph) -> str:
    payload = {
        "nodes": [_node_revision_payload(node) for node in sorted(subgraph.nodes, key=lambda item: item.id)],
        "edges": [_edge_revision_payload(edge) for edge in sorted(subgraph.edges, key=lambda item: item.id)],
        "ranking": [_ranked_revision_payload(item) for item in sorted(subgraph.ranked_nodes, key=lambda item: item.node.id)],
    }
    encoded = json.dumps(_canonical_json_value(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _node_revision_payload(node: MemoryNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.type,
        "label": node.label,
        "text": node.text,
        "canonical_key": node.canonical_key,
        "properties": node.properties,
        "base_activation": node.base_activation,
        "salience": node.salience,
        "confidence": node.confidence,
        "stability": node.stability,
        "volatility": node.volatility,
        "utility": node.utility,
        "status": node.status,
        "updated_at": node.updated_at,
    }


def _edge_revision_payload(edge: MemoryEdge) -> dict[str, Any]:
    return {
        "id": edge.id,
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "type": edge.type,
        "weight": edge.weight,
        "confidence": edge.confidence,
        "polarity": edge.polarity,
        "origin": edge.origin,
        "properties": edge.properties,
        "updated_at": edge.updated_at,
    }


def _ranked_revision_payload(item: RankedNode) -> dict[str, Any]:
    return {
        "node_id": item.node.id,
        "score": item.score,
        "reasons": item.reasons,
    }


def _canonical_json_value(value: Any) -> Any:
    """Normalize non-JSON values; the revision encoder sorts the resulting keys."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _canonical_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted([_canonical_json_value(item) for item in value], key=lambda item: json.dumps(item, ensure_ascii=True, sort_keys=True, default=str))
    return str(value)
