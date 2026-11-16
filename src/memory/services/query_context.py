"""Application service shared by Python, CLI, and MCP query-context adapters."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any
from pathlib import Path

from .coordination import CoordinationStore, render_file_overlaps, render_work_context
from .failure_context import FailureContextService, render_failure_context

from ..domain.models import MemoryEdge, MemoryNode, MemoryQuery, MemorySubgraph, RankedNode
from ..domain.query_context import (
    CONTEXT_RESULT_SCHEMA_VERSION,
    Confidence,
    ContextResult,
    GraphFreshness,
    QueryContextRequest,
    SourceRevision,
)
from ..artifacts.revision import ProjectRevision, RevisionRepository
from .retrieval import RetrievalEngine
from .retrieval.engine import RANKING_PROFILE
from .retrieval.context.source import (
    MAX_SOURCE_FILES,
    _BRIEF_SOURCE_LINES,
    _source_snapshot,
    assigned_document_outline,
    brief_task_next_action,
    capture_assigned_documentation,
    capture_source_excerpts,
    capture_task_edit_spans,
    capture_task_test_file,
    complete_pinned_task_file,
    pinned_code_context,
    readme_setup_location,
    reuse_task_source,
)


class QueryContextService:
    """Own query-context retrieval, projection, metadata, and rendering."""

    def __init__(self, retrieval: RetrievalEngine) -> None:
        self.retrieval = retrieval

    def _focus_assigned_source(
        self, payload: dict[str, Any], query: str, task: dict[str, Any], revision: ProjectRevision,
        snapshots: dict[str, dict[str, Any]],
    ) -> set[str]:
        """Prefer exact in-scope source and retain locations when bodies do not fit."""
        reads = self.retrieval.assigned_exact_reads(query, task["files"], revision)
        if not reads:
            return set()
        paths = set(task["files"])
        # Keep an existing preview only if it already covers an exact owner.
        prior = [row for row in payload["snippets"] if row["path"] in paths
                 and any(row["path"] == read["path"] and row["line_start"] <= read["line_start"]
                         and read["line_end"] <= row["line_end"] for read in reads)]
        shown = [row for row in prior if row.get("source_status") == "verified" and row.get("text")]
        pending = [read for read in reads if read["line_end"] - read["line_start"] < _BRIEF_SOURCE_LINES and not any(
            row["path"] == read["path"] and row["line_start"] <= read["line_start"]
            and read["line_end"] <= row["line_end"] for row in shown
        )]
        extra = capture_source_excerpts(
            self.retrieval.store, pending, complete_only=True, snapshots=snapshots,
            revisions={revision.project_id: revision}, shown=shown,
            new_file_budget=max(0, MAX_SOURCE_FILES - len({row["path"] for row in shown})),
        )
        selected = [*shown, *extra]
        payload["snippets"] = selected
        payload["task_focus_ranges"] = reads
        payload["task_source_paths"] = task["files"]
        if selected:
            payload["source_mode"] = "verified"
        if not shown:
            payload.pop("linked_section", None)
        return {row["path"] for row in selected if row.get("source_status") == "stale"}

    def execute(self, request: QueryContextRequest, *, brief: bool = False, follow_up: bool = False) -> ContextResult:
        """Resolve context with request-local source evidence shared by its consumers."""
        query = MemoryQuery(
            text=request.text,
            top_k=request.budget.top_k,
            max_depth=request.budget.max_depth,
            include_archived=request.include_archived,
            context_scopes=set(request.scopes) or None,
        )
        subgraph = self.retrieval.retrieve(query)
        revision = _source_revision(subgraph, self.retrieval.store)
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
                brief=brief,
                source_snapshots=source_snapshots,
                source_revisions={revision.project_id: revision} if revision else {},
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
                "ranking_profile": RANKING_PROFILE,
            }
        )
        stale_paths = {item.get("path") for item in payload.get("snippets", []) if item.get("source_status") == "stale"}
        if not request.include_source and revision is not None:
            for row in payload.get("working_set", [])[:8]:
                entry = revision.manifest.get(row["path"])
                if entry is None:
                    continue
                artifact_id = entry["artifact_id"]
                snapshot = source_snapshots.get(artifact_id)
                if snapshot is None:
                    artifact = self.retrieval.store.get_node(artifact_id, clone=False)
                    snapshot = _source_snapshot(self.retrieval.store, artifact, revision)
                    source_snapshots[artifact_id] = snapshot
                if snapshot["status"] == "stale":
                    stale_paths.add(row["path"])
            if stale_paths and not brief:
                payload["snippets"] = [row for row in payload.get("snippets", []) if row.get("path") not in stale_paths]
        storage_path = getattr(self.retrieval.store, "path", None)
        if request.task_id is not None and storage_path is None:
            raise ValueError("Task recovery requires project work storage")
        if follow_up and storage_path is not None and request.task_id is not None:
            task = CoordinationStore(Path(storage_path).with_name("agent-dashboard.reql")).show(request.task_id)
            if (brief and not request.include_source and revision is not None and task["kind"] == "task"
                    and payload["kind"] == "code" and payload.get("query_mode") == "informative"):
                stale_paths.update(self._focus_assigned_source(payload, request.text, task, revision, source_snapshots))
        if storage_path is not None and not follow_up:
            work = CoordinationStore(Path(storage_path).with_name("agent-dashboard.reql"))
            # The code projection already selected the relevant owners. Incidental
            # ranked neighbors must not pull unrelated work into that working set.
            if payload.get("kind") == "code":
                files = list(dict.fromkeys(str(row["path"]) for key in ("snippets", "working_set")
                                           for row in payload.get(key, []) if row.get("path")))[:8]
            else:
                files = list(dict.fromkeys(str(item.node.properties["relative_path"]) for item in subgraph.ranked_nodes
                                           if item.node.properties.get("relative_path")))[:8]
            records = work.records()
            payload["engineering_context"] = work.context(
                request.text, files=files, record_id=request.task_id, limit=min(8, request.budget.max_items),
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
                    payload["task_source_paths"] = assigned["files"]
                    test_source = capture_task_test_file(
                        self.retrieval.store, assigned["files"], revision, source_snapshots,
                        shown_paths={row["path"] for row in payload["snippets"]},
                    )
                    if test_source is not None:
                        payload["task_test_source"] = test_source
                    shown = list(payload["snippets"])
                    if test_source is not None:
                        shown.append(test_source)
                    edit_spans, omitted = capture_task_edit_spans(
                        self.retrieval.store, assigned["files"], assigned["next_action"],
                        revision, source_snapshots, shown=shown, replaceable=payload["snippets"],
                    )
                    if edit_spans or omitted:
                        payload["task_edit_spans"] = {"spans": edit_spans, "omitted": omitted}
                    elif payload.get("query_mode") == "informative":
                        # Explicit edit spans drive the pinned-source workflow.
                        stale_paths.update(self._focus_assigned_source(
                            payload, request.text, assigned, revision, source_snapshots,
                        ))
                        shown = [*payload["snippets"], *([test_source] if test_source else [])]
                    whole = complete_pinned_task_file(self.retrieval.store, revision, source_snapshots, edit_spans)
                    if whole is not None:
                        payload["task_source_file"] = whole
                        payload["source_mode"] = "verified"
                    covered = (*shown, *([whole] if whole else []))
                    owner, local_reads = pinned_code_context(
                        self.retrieval.store, revision, source_snapshots, edit_spans,
                        f"{assigned['content']}\n{assigned['next_action']}", shown=covered,
                    )
                    if owner is not None:
                        payload["task_owner_source"] = owner
                    local_source = capture_source_excerpts(
                        self.retrieval.store, local_reads, complete_only=True, snapshots=source_snapshots,
                        revisions={revision.project_id: revision},
                    )
                    if local_source:
                        payload["task_local_source"] = local_source
                    doc_coverage = (*covered, *edit_spans)
                    if documentation := capture_assigned_documentation(
                        self.retrieval.store, assigned["files"], assigned["content"], revision, source_snapshots,
                        shown=doc_coverage,
                    ):
                        payload["task_documentation_source"] = documentation
                    elif outline := assigned_document_outline(
                        self.retrieval.store, assigned["files"], revision, source_snapshots, shown=doc_coverage,
                    ):
                        payload["task_documentation_outline"] = outline
                    if (payload.get("query_mode") == "informative" and not payload.get("task_focus_ranges")
                            and not any(row.get("text") for row in payload["snippets"])):
                        read = self.retrieval.assigned_source_read(request.text, assigned["files"], revision,
                                                                   (*shown, *edit_spans))
                        if read is not None:
                            excerpts = capture_source_excerpts(
                                self.retrieval.store, [read], complete_only=True, snapshots=source_snapshots,
                                revisions={revision.project_id: revision},
                            )
                            if excerpts:
                                payload["snippets"].extend(excerpts)
                                payload["source_mode"] = "verified"
                                if excerpts[0]["source_status"] == "stale":
                                    freshness = replace(freshness, status="stale",
                                                        pending_paths=max(freshness.pending_paths, 1))
                    companion_paths = set(assigned["files"]) - ({whole["path"]} if whole else set())
                    declaration = self.retrieval.assigned_declaration_read(
                        payload["snippets"], companion_paths, f"{assigned['content']} {assigned['next_action']}",
                    )
                    if declaration is not None:
                        payload["snippets"].extend(capture_source_excerpts(
                            self.retrieval.store, [declaration], complete_only=True, snapshots=source_snapshots,
                            revisions={revision.project_id: revision}, shown=payload["snippets"],
                        ))
                    reuse_task_source(payload["snippets"], payload.get("linked_section"), revision,
                                      source_snapshots, shown=(*shown, *edit_spans),
                                      include_guidance=not any("pinned_line_end" in row for row in edit_spans))
                    dependencies = set(assigned["depends_on"])
                    prerequisites = [row for row in payload["engineering_context"]["records"]
                                     if row["id"] in dependencies and row["kind"] in {"decision", "constraint"}
                                     and row["execution_state"] in {"active", "resolved", "done"}]
                    if prerequisites:
                        contract_reads = self.retrieval.decision_contract_reads(
                            prerequisites, revision, [*payload["snippets"], *covered, *edit_spans, *local_source,
                                                      *([owner] if owner else [])],
                        )
                        contracts = capture_source_excerpts(
                            self.retrieval.store, contract_reads, complete_only=True, snapshots=source_snapshots,
                            revisions={revision.project_id: revision}, new_file_budget=2,
                        )
                        verified = [row for row in contracts if row["source_status"] == "verified"]
                        if verified:
                            payload["task_contract_source"] = verified
                    from ..testing.evidence_store import TestEvidenceStore
                    from ..testing.verification import verification_handoff
                    project = self.retrieval.store.get_node(revision.project_id, clone=False)
                    root_path = project.properties.get("root_path") if project else None
                    if root_path:
                        payload.update(verification_handoff(Path(root_path).resolve(), TestEvidenceStore(Path(storage_path)),
                                                            assigned["id"]))
                        if test_paths := payload.get("verification_test_paths"):
                            already_shown = {row["path"] for row in (*shown, *edit_spans)
                                             if row.get("whole_file") and row.get("source_status") == "verified"}
                            if test := capture_task_test_file(
                                self.retrieval.store, test_paths, revision, source_snapshots, shown_paths=already_shown,
                                extra_file_budget=1, require_test_scope=False,
                            ):
                                payload["verification_test_source"] = test
                        if ("README.md" not in assigned["files"]
                                and (payload.get("verified_checks") or payload.get("baseline_failure"))
                                and (setup := readme_setup_location(self.retrieval.store, revision, source_snapshots))):
                            payload["task_setup_location"] = setup
            if any(row["kind"] == "failure" for row in records):
                owners = [str(row["id"]) for row in payload.get("owner_candidates", []) if row.get("id")]
                failure = FailureContextService(work, self.retrieval.store).context(
                    request.text, files=files if request.task_id is None else None,
                    symbol_ids=owners if request.task_id is None else None, records=records,
                    record_id=request.task_id,
                    limit=min(3, request.budget.max_items), _source_lookup=brief,
                )
                existing = {row["id"] for row in payload["engineering_context"]["records"]}
                for row in failure["records"]:
                    if row["id"] in existing:
                        row.pop("content")
                        row.pop("rationale")
                        row["engineering_context_ref"] = row["id"]
                if failure["records"]:
                    payload["failure_context"] = failure
        if stale_paths:
            freshness = replace(freshness, status="stale", pending_paths=len(stale_paths))
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
        rendered = self.retrieval.render_context_payload(payload, brief=brief)
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
                shown = []
                if brief:
                    shown = list(payload.get("snippets", []))
                    shown.extend(payload.get("task_edit_spans", {}).get("spans", []))
                    shown.extend(payload[key] for key in ("task_source_file", "task_owner_source", "task_test_source", "linked_section",
                                                           "task_documentation_source") if key in payload)
                if brief and result.confidence.status == "sufficient" and payload.get("source_mode") == "verified":
                    work = {**work, "records": [
                        {**row, "next_action": brief_task_next_action(row["next_action"], shown)}
                        if "task" in row["reasons"] and row["kind"] == "task" else row
                        for row in work["records"]
                    ]}
                handoff = render_work_context(work, max_chars=3600, record_id=task_id,
                                              complete_records=complete_records, record_notes=failure_notes)
                if brief and (overlaps := payload.get("file_overlaps")):
                    handoff += "\n\n" + render_file_overlaps(overlaps)
                if brief:
                    assigned = next((row for row in work["records"] if "task" in row["reasons"]), None)
                    if (assigned and assigned["kind"] == "task"
                            and assigned["execution_state"] in {"ready", "in_progress"}
                            and (assigned["id"], assigned["revision"]) in complete_records
                            and set(assigned["depends_on"]) <= {record_id for record_id, _ in complete_records}):
                        handoff += "\nSaved task and direct prerequisites shown in full."
                plan = payload.get("verification_plan")
                if plan:
                    from ..testing.verification import SOURCE_CHECK_NAME, cargo_check_commands

                    receipt = payload.get("verified_checks")
                    baseline = payload.get("baseline_failure")
                    compact_checks = brief and allow_work_commands and bool(completion_command)
                    counts = {check["name"]: check["tests"] for check in receipt["checks"] if "tests" in check} if receipt else {}
                    commands = [(name, f"`python -m unittest discover -s tests -p '{name}'`") for name in plan["patterns"]]
                    commands.extend((name, "`cargo " + " ".join(arguments) + "`") for name, arguments in
                                    cargo_check_commands(plan.get("cargo", ()), plan.get("cargo_test", ())))
                    if plan["diff"]:
                        commands.append((SOURCE_CHECK_NAME, SOURCE_CHECK_NAME))
                    checks = ", ".join(command + (f" ({counts[name]} tests)" if name in counts else "")
                                       for name, command in commands)
                    if compact_checks:
                        observation = receipt or baseline
                        statuses = {check["name"]: check["passed"] for check in observation["checks"]} if observation else {}
                        labels = [name + (f" ({'passed' if statuses[name] else 'failed'})" if name in statuses else "")
                                  for name, _ in commands]
                        title = "Recorded baseline on matching inputs" if observation else "Saved checks"
                        handoff += f"\n\n{title}: {', '.join(labels)}."
                        if observation and not observation["environment_matches"]:
                            handoff += " Earlier process environment."
                        if observation and observation.get("runtime_checked") is False:
                            handoff += " Toolchain identity is checked at verification."
                    elif receipt:
                        handoff += f"\n\n## Verified baseline on matching source\n- Passed: {checks}"
                        if not receipt["environment_matches"]:
                            handoff += " (earlier process environment)"
                        if receipt.get("runtime_checked") is False:
                            handoff += "; finish checks Cargo runtime before reuse"
                    else:
                        handoff += f"\n\nSaved checks: {checks}."
                        if baseline:
                            failed = ", ".join(check["name"] for check in baseline["checks"] if not check["passed"])
                            handoff += f"\nRecorded failures on matching inputs: {failed}."
                            if not baseline["environment_matches"]:
                                handoff += " From an earlier process environment."
                            if baseline.get("runtime_checked") is False:
                                handoff += " Toolchain identity is checked at verification."
                    if baseline:
                        failure = next((check["failure"] for check in baseline["checks"] if "failure" in check), None)
                        if failure:
                            case = ".".join(failure["case"].split(".")[-2:])
                            location = f" at `{failure['path']}:{failure['line']}`" if "path" in failure else ""
                            outcome = "failure" if failure["outcome"] == "FAIL" else "error"
                            handoff += f"\nRecorded {outcome}: `{case}`{location}."
                if brief and (setup := payload.get("task_setup_location")):
                    handoff += (f"\n\nIf setup is needed, read the relevant lines in `{setup['path']}` "
                                f"[{setup['line_start']}-{setup['line_end']}] ({setup['title']}).")
                checks_source_diff = bool(completion_command and plan and plan["diff"])
                if (allow_work_commands and result.source_revision is not None and result.freshness.status == "current"
                        and not checks_source_diff
                        and any(row.get("source_status") == "verified" and row.get("text") for row in shown)):
                    handoff += (
                        "\n\nApply shown line edits and refresh with `reql edit --plan -`. "
                        "Pipe a JSON array to stdin, even for one edit. Use `{path,find,text}` with unique exact full lines."
                    )
                if allow_work_commands and completion_command:
                    if plan:
                        handoff += f"\n\nAfter reviewing your diff: `{completion_command}` runs saved checks and completes the task."
                        if checks_source_diff:
                            handoff += " Edit the files directly and finish before refreshing so the source check sees the changes."
                    else:
                        handoff += f"\n\nAfter your checks pass: `{completion_command}` completes the task."
                elif allow_work_commands and plan:
                    handoff += "\n\nFor direct file edits, run `reql project compile` after your checks pass."
                if brief and (completion_command or (
                    assigned and assigned["status"] == "in_progress" and assigned["waiting_on"]
                    and not assigned["conflicts"] and not assigned["obsolete_dependencies"]
                )):
                    handoff += (f'\n\nFor missing code only: `reql query_context --task {task_id} --follow-up '
                                '--query "literal terms" --code --brief`.')
                rendered = handoff + "\n\n" + rendered
            else:
                rendered += "\n\n" + render_work_context(work, max_chars=2400, complete_records=complete_records,
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


def _source_revision(subgraph: MemorySubgraph, store: Any) -> ProjectRevision | None:
    """Find the committed revision represented by the retrieved nodes."""
    project_ids = {
        str(node.properties.get("project_id") or (node.id if node.type == "Project" else ""))
        for node in subgraph.nodes
    }
    project_ids.discard("")
    revisions = RevisionRepository(store)
    return next((revision for project_id in sorted(project_ids) if (revision := revisions.latest(project_id))), None)


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
