"""Pure MCP tool handlers for REQL.

This module is deliberately independent from any MCP transport or SDK. It
accepts plain dictionaries and returns JSON-serializable dictionaries, so unit
tests and lightweight integrations can exercise the same behavior as the stdio
server.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from api.memory_graph import MemoryGraph
from memory.artifacts.options import CompilationOptions
from memory.config import (
    ConfigError,
    REQLConfig,
    load_effective_config,
    resolve_config_path,
)
from memory.domain.exceptions import REQLError
from memory.domain.query_context import (
    DEFAULT_MAX_DEPTH as DEFAULT_CONTEXT_MAX_DEPTH,
)
from memory.domain.query_context import (
    DEFAULT_MAX_ITEMS as DEFAULT_CONTEXT_MAX_ITEMS,
)
from memory.domain.query_context import (
    DEFAULT_TOP_K as DEFAULT_CONTEXT_TOP_K,
)
from memory.domain.query_context import (
    MAX_DEPTH as MAX_CONTEXT_DEPTH,
)
from memory.domain.query_context import (
    MAX_ITEMS as MAX_QUERY_CONTEXT_ITEMS,
)
from memory.domain.query_context import (
    MAX_TOP_K as MAX_QUERY_CONTEXT_TOP_K,
)
from memory.domain.query_context import (
    MIN_DEPTH as MIN_CONTEXT_DEPTH,
)
from memory.domain.query_context import (
    MIN_ITEMS as MIN_QUERY_CONTEXT_ITEMS,
)
from memory.domain.query_context import (
    MIN_TOP_K as MIN_QUERY_CONTEXT_TOP_K,
)
from memory.domain.query_context import (
    QueryContextRequest,
)
from memory.security import SecurityError, sanitize_agent_text, validate_mcp_path
from memory.services.coordination import CoordinationStore, KINDS

MAX_TOP_K = 50
MAX_DEPTH = 5
MAX_ITEMS = 50
MAX_QUERY_ROWS = 200
MAX_MEMORY_TEXT_CHARS = 2000
MAX_CONTEXT_NODES = 200
MAX_CONTEXT_EDGES = 400
MUTATING_REQL_COMMANDS = {"COMMUNITIES", "HUBS"}


class MCPToolError(REQLError):
    """Raised when an MCP tool request is invalid or cannot be completed."""


ToolHandler = Callable[..., dict[str, Any]]


READ_ONLY_TOOLS = {
    "reql_tests_for",
    "query_context",
    "query_explore",
    "query_graph",
    "query_memories",
    "reql_query",
    "reql_project_status",
    "reql_work_context",
    "reql_work_overview",
}

WRITE_TOOLS = {
    "reql_tests_import",
    "reql_compile_project",
    "reql_hubs",
    "reql_work_record",
    "reql_work_claim_next",
}


def _work_store(storage_path: str) -> CoordinationStore:
    """Validate both the project boundary and its derived engineering store."""
    selected = _tool_storage(storage_path)
    dashboard = validate_mcp_path(selected.with_name("agent-dashboard.reql"), name="dashboard_storage")
    return CoordinationStore(dashboard)


def reql_work_context(*, storage_path: str, query: str = "", files: list[str] | None = None,
                      workstream: str | None = None, record_id: str | None = None,
                      limit: int = 8, include_history: bool = False, kinds: list[str] | None = None) -> dict[str, Any]:
    """Read focused causal work without opening the canonical graph or a session."""
    try:
        work = _work_store(storage_path)
        if kinds == ["failure"] and not include_history:
            from memory.services.failure_context import FailureContextService

            path = work.path.with_name(Path(storage_path).name)
            arguments = {"files": files, "workstream": workstream, "record_id": record_id, "limit": limit}
            if path.is_file() or path.with_name(f"{path.name}.wal").is_file():
                with _open_graph(str(path), _load_tool_config(None, None), read_only=True) as graph:
                    return graph.failure_context(query, **arguments)
            return FailureContextService(work).context(query, **arguments)
        return work.context(query, files=files, workstream=workstream, record_id=record_id, limit=limit,
                            include_history=include_history, kinds=kinds)
    except ValueError as exc:
        raise MCPToolError(str(exc)) from exc


def reql_work_overview(*, storage_path: str, limit: int = 5) -> dict[str, Any]:
    """Read compact goals, trajectory, blockers and ready work across sessions."""
    try:
        return _work_store(storage_path).overview(limit=limit)
    except ValueError as exc:
        raise MCPToolError(str(exc)) from exc


def reql_tests_for(*, storage_path: str, symbol_id: str | None = None, file: str | None = None,
                   include_candidates: bool = False, limit: int = 20) -> dict[str, Any]:
    """Return the same association rows exposed by the Python API."""
    config = _load_tool_config(None, None, start_dir=_project_root_for_storage(Path(storage_path)))
    with _open_graph(storage_path, config, read_only=True) as graph:
        rows = graph.find_tests_for(symbol_id=symbol_id, file=file, include_candidates=include_candidates, limit=limit)
        return {"schema_version": 1, "associations": [asdict(row) for row in rows]}


def reql_tests_import(*, storage_path: str, path: str, input: str, format: str = "coverage-json",
                      confirm: bool = False) -> dict[str, Any]:
    """Import a report after the caller explicitly authorizes the sidecar write."""
    if confirm is not True:
        raise MCPToolError("Test evidence import requires confirm=true")
    if format != "coverage-json":
        raise MCPToolError("Test evidence import supports coverage-json")
    root = validate_mcp_path(_required_path_text(path, "path"), name="path")
    report = validate_mcp_path(Path(root) / _required_path_text(input, "input"), name="input")
    config = _load_tool_config(None, None, start_dir=root)
    with _open_graph(storage_path, config, read_only=True) as graph:
        evidence = graph.test_intelligence.evidence
        if evidence is not None:
            validate_mcp_path(evidence.path, name="test_evidence_storage")
        return graph.test_intelligence.import_coverage(report, project_path=root)


def reql_work_record(*, storage_path: str, kind: str, content: str, agent_id: str,
                     session_id: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reconcile one shared record with explicit caller provenance and revision."""
    if fields is not None and not isinstance(fields, dict):
        raise MCPToolError("fields must be an object")
    try:
        return _work_store(storage_path).put(kind, content, agent_id=_required_text(agent_id, "agent_id"),
                                             session_id=_required_text(session_id, "session_id"), **(fields or {}))
    except (TypeError, ValueError) as exc:
        raise MCPToolError(str(exc)) from exc


def reql_work_claim_next(*, storage_path: str, agent_id: str, session_id: str,
                         workstream: str | None = None) -> dict[str, Any]:
    """Claim ready work with host-supplied provenance and return a causal handoff."""
    try:
        work = _work_store(storage_path)
        result = work.claim_next(agent_id=_required_text(agent_id, "agent_id"),
                                 session_id=_required_text(session_id, "session_id"), workstream=workstream)
        if task := result.get("claimed_task"):
            result["work_context"] = work.context(record_id=task["id"], limit=6)
        return result
    except ValueError as exc:
        raise MCPToolError(str(exc)) from exc


def query_graph(
    *,
    storage_path: str,
    query: str,
    top_k: int = 12,
    max_depth: int = 2,
    max_nodes: int = 80,
    max_edges: int = 160,
    max_sources: int = 20,
    max_items: int = 20,
    filter_generic: bool = True,
    include_archived: bool = False,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a structured query-centered subgraph with sources and agent context."""
    config = _load_tool_config(config_path, config_overrides)
    query = _required_text(query, "query")
    top_k = _bounded_int(top_k, "top_k", minimum=1, maximum=MAX_TOP_K)
    max_depth = _bounded_int(max_depth, "max_depth", minimum=0, maximum=MAX_DEPTH)
    max_nodes = _bounded_int(max_nodes, "max_nodes", minimum=1, maximum=MAX_CONTEXT_NODES)
    max_edges = _bounded_int(max_edges, "max_edges", minimum=0, maximum=MAX_CONTEXT_EDGES)
    max_sources = _bounded_int(max_sources, "max_sources", minimum=0, maximum=MAX_ITEMS)
    max_items = _bounded_int(max_items, "max_items", minimum=1, maximum=MAX_ITEMS)
    with _open_graph(storage_path, config, read_only=True) as graph:
        return graph.query_graph(
            query,
            top_k=top_k,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            max_sources=max_sources,
            max_items=max_items,
            filter_generic=bool(filter_generic),
            include_archived=bool(include_archived),
        )


def query_context(
    *,
    storage_path: str,
    query: str,
    mode: str = "informative",
    scopes: list[str] | None = None,
    code: bool = False,
    docs: bool = False,
    test: bool = False,
    top_k: int = DEFAULT_CONTEXT_TOP_K,
    max_depth: int = DEFAULT_CONTEXT_MAX_DEPTH,
    max_items: int = DEFAULT_CONTEXT_MAX_ITEMS,
    include_archived: bool = False,
    include_source: bool = False,
    task_id: str | None = None,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a bounded deterministic context block for a task/query."""
    storage_path = str(validate_mcp_path(_required_path_text(storage_path, "storage_path"), name="storage_path"))
    config = _load_tool_config(config_path, config_overrides, start_dir=_project_root_for_storage(Path(storage_path)))
    query = _required_text(query, "query")
    selected_scopes = _optional_string_list(scopes, "scopes") or []
    for enabled, scope in ((code, "code"), (docs, "docs"), (test, "test")):
        if enabled and scope not in selected_scopes:
            selected_scopes.append(scope)
    try:
        request = QueryContextRequest.from_raw(
            text=query,
            mode=mode,
            scopes=selected_scopes,
            top_k=top_k,
            max_depth=max_depth,
            max_items=max_items,
            include_archived=include_archived,
            include_source=include_source,
            task_id=task_id,
        )
    except (TypeError, ValueError) as exc:
        raise MCPToolError(str(exc)) from exc
    with _open_graph(storage_path, config, read_only=True) as graph:
        result = graph.query_context_result(request)
        return result.to_dict()


def query_explore(
    *,
    storage_path: str,
    query: str,
    views: list[str] | None = None,
    top_k: int = 12,
    max_depth: int = 3,
    limit: int = 12,
    max_items: int = 18,
    include_archived: bool = False,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return dependency and structural-refactor query slices for coding agents."""
    config = _load_tool_config(config_path, config_overrides)
    query = _required_text(query, "query")
    views = _optional_string_list(views, "views")
    top_k = _bounded_int(top_k, "top_k", minimum=1, maximum=MAX_TOP_K)
    max_depth = _bounded_int(max_depth, "max_depth", minimum=0, maximum=MAX_DEPTH)
    limit = _bounded_int(limit, "limit", minimum=1, maximum=MAX_ITEMS)
    max_items = _bounded_int(max_items, "max_items", minimum=1, maximum=MAX_ITEMS)
    with _open_graph(storage_path, config, read_only=True) as graph:
        return graph.query_explore(
            query,
            views=views,
            top_k=top_k,
            max_depth=max_depth,
            limit=limit,
            max_items=max_items,
            include_archived=bool(include_archived),
        )


def query_memories(
    *,
    storage_path: str,
    query: str,
    top_k: int = 12,
    max_depth: int = 2,
    limit: int = 12,
    include_sources: bool = True,
    filter_generic: bool = True,
    max_text_chars: int = 600,
    include_archived: bool = False,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a compact list of relevant memory/source texts for an agent query."""
    config = _load_tool_config(config_path, config_overrides)
    query = _required_text(query, "query")
    top_k = _bounded_int(top_k, "top_k", minimum=1, maximum=MAX_TOP_K)
    max_depth = _bounded_int(max_depth, "max_depth", minimum=0, maximum=MAX_DEPTH)
    limit = _bounded_int(limit, "limit", minimum=1, maximum=MAX_ITEMS)
    max_text_chars = _bounded_int(max_text_chars, "max_text_chars", minimum=80, maximum=MAX_MEMORY_TEXT_CHARS)
    with _open_graph(storage_path, config, read_only=True) as graph:
        return graph.query_memories_payload(
            query,
            top_k=top_k,
            max_depth=max_depth,
            limit=limit,
            include_sources=bool(include_sources),
            filter_generic=bool(filter_generic),
            max_text_chars=max_text_chars,
            include_archived=bool(include_archived),
        )


def reql_query(
    *,
    storage_path: str,
    statement: str,
    limit: int | None = None,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a REQL statement and return bounded tabular results."""
    config = _load_tool_config(config_path, config_overrides)
    statement = _required_text(statement, "statement")
    if _reql_statement_requires_write(statement):
        raise MCPToolError("This REQL statement persists analysis results; use an approved write/update tool instead")
    output_limit = _bounded_optional_int(limit, "limit", minimum=0, maximum=MAX_QUERY_ROWS)
    if output_limit is None:
        output_limit = MAX_QUERY_ROWS
    with _open_graph(storage_path, config, read_only=True) as graph:
        result = graph.query(statement)
        rows = [_jsonable(row) for row in result.rows[:output_limit]]
        original_count = len(result.rows)
        return {
            "columns": list(result.columns),
            "rows": rows,
            "metadata": {
                "statement": result.statement,
                "command": result.command,
                "diagnostics": _jsonable(result.diagnostics),
                "row_count": original_count,
                "rows_returned": len(rows),
                "output_limit": output_limit,
                "output_limited": original_count > len(rows),
                "limit_applied": limit is not None,
            },
        }


def reql_project_status(
    *,
    storage_path: str,
    path: str,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return registered project/cache status for a path."""
    path = str(validate_mcp_path(_required_path_text(path, "path"), name="path"))
    config = _load_tool_config(config_path, config_overrides, start_dir=path)
    with _open_graph(storage_path, config, read_only=True) as graph:
        status = graph.project_status(path, config_path=resolve_config_path(config_path, start_dir=path))
        if status is None:
            return {"found": False, "path": path}
        payload = _jsonable(status)
        payload["found"] = True
        return payload


def reql_hubs(
    *,
    storage_path: str,
    project_id: str | None = None,
    limit: int = 10,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a bounded list of high-value hub nodes."""
    config = _load_tool_config(config_path, config_overrides)
    if not config.analysis.enable_hubs:
        raise MCPToolError("Hub analysis is disabled by configuration")
    limit = _bounded_int(limit, "limit", minimum=1, maximum=MAX_TOP_K)
    with _open_graph(storage_path, config) as graph:
        report = graph.analyze_hubs(project_id=project_id, limit=limit)
        hubs = [
            {
                "id": hub.node_id,
                "type": hub.node_type,
                "label": hub.label,
                "score": hub.hub_score,
                "reasons": list(hub.reasons),
                "metadata": {
                    "rank": hub.hub_rank,
                    "is_hub": hub.is_hub,
                    "centrality_score": hub.centrality_score,
                    "specificity_score": hub.specificity_score,
                    "community_bridge_score": hub.community_bridge_score,
                    "generic_penalty": hub.generic_penalty,
                },
            }
            for hub in report.hubs[:limit]
        ]
        return {"hubs": _jsonable(hubs), "warnings": list(report.warnings)}


def reql_compile_project(
    *,
    storage_path: str,
    path: str,
    cache_enabled: bool | None = None,
    config_path: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a project into the graph. Intended for client-side approval."""
    path = str(validate_mcp_path(_required_path_text(path, "path"), name="path"))
    config = _load_tool_config(config_path, config_overrides, start_dir=path)
    with _open_graph(storage_path, config) as graph:
        result = graph.compile_project(
            path,
            max_file_size_bytes=config.scan.max_file_size_bytes,
            include_patterns=config.scan.include,
            exclude_patterns=config.scan.exclude,
            config_path=resolve_config_path(config_path, start_dir=path),
            cache_enabled=config.cache.enabled if cache_enabled is None else bool(cache_enabled),
            parsing_options=CompilationOptions.from_config(config),
        )
        run = result.run
        return {
            "summary": {
                "project_id": result.scan.project.id,
                "run_id": run.id,
                "status": run.status,
                "created": {"nodes": run.nodes_created, "edges": run.edges_created},
                "updated": {"nodes": run.nodes_updated, "edges": run.edges_updated},
                "archived": {
                    "nodes": len(result.delta.archived_nodes),
                    "edges": len(result.delta.archived_edges),
                    "files_deleted": run.files_deleted,
                },
                "errors": list(run.errors),
                "delta_id": result.delta.id,
                "files_seen": run.files_seen,
                "files_changed": run.files_changed,
                "files_skipped": run.files_skipped,
            }
        }


TOOL_HANDLERS: dict[str, ToolHandler] = {
    "reql_tests_for": reql_tests_for,
    "reql_tests_import": reql_tests_import,
    "reql_work_context": reql_work_context,
    "reql_work_overview": reql_work_overview,
    "reql_work_record": reql_work_record,
    "reql_work_claim_next": reql_work_claim_next,
    "query_graph": query_graph,
    "query_context": query_context,
    "query_explore": query_explore,
    "query_memories": query_memories,
    "reql_query": reql_query,
    "reql_project_status": reql_project_status,
    "reql_hubs": reql_hubs,
    "reql_compile_project": reql_compile_project,
}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "reql_tests_for",
        "description": "Read-only: observed, static and optional candidate test associations.",
        "inputSchema": {
            "type": "object", "required": ["storage_path"],
            "oneOf": [{"required": ["symbol_id"]}, {"required": ["file"]}],
            "properties": {
                "storage_path": {"type": "string"}, "symbol_id": {"type": "string"},
                "file": {"type": "string"}, "include_candidates": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "reql_tests_import",
        "description": "Write: import project-local coverage JSON into the test evidence store after explicit approval.",
        "inputSchema": {
            "type": "object", "required": ["storage_path", "path", "input", "confirm"],
            "properties": {
                "storage_path": {"type": "string"}, "path": {"type": "string"}, "input": {"type": "string"},
                "format": {"const": "coverage-json"}, "confirm": {"const": True},
            },
        },
    },
    {
        "name": "reql_work_context",
        "description": "Read-only: focused engineering work, decisions, constraints, failures and dependencies across sessions.",
        "inputSchema": {"type": "object", "required": ["storage_path"], "properties": {
            "storage_path": {"type": "string"}, "query": {"type": "string", "default": ""},
            "files": {"type": "array", "items": {"type": "string"}},
            "workstream": {"type": "string"}, "record_id": {"type": "string"},
            "kinds": {"type": "array", "items": {"type": "string", "enum": sorted(KINDS)}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 40, "default": 8},
            "include_history": {"type": "boolean", "default": False}}},
    },
    {
        "name": "reql_work_overview",
        "description": "Read-only: compact project goals, workstreams, direction, completion, blockers and next ready outcomes.",
        "inputSchema": {"type": "object", "required": ["storage_path"], "properties": {
            "storage_path": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}}},
    },
    {
        "name": "reql_work_record",
        "description": "Write: upsert durable work. Reuse ids; updates require fields.record_id and fields.expected_revision. Decisions/failures require fields.rationale. Provide stable agent/session provenance; record checkpoints at session boundaries.",
        "inputSchema": {"type": "object", "required": ["storage_path", "kind", "content", "agent_id", "session_id"], "properties": {
            "storage_path": {"type": "string"}, "kind": {"type": "string", "enum": sorted(KINDS)},
            "content": {"type": "string"}, "agent_id": {"type": "string"}, "session_id": {"type": "string"},
            "fields": {"type": "object", "additionalProperties": False, "properties": {
                "key": {"type": "string"}, "record_id": {"type": "string"}, "expected_revision": {"type": "integer"},
                "status": {"type": "string"}, "rationale": {"type": "string"}, "workstream": {"type": "string"},
                "parent": {"type": "string"}, "next_action": {"type": "string"}, "importance": {"type": "integer"},
                "failure_evidence": {"type": "object", "additionalProperties": False, "properties": {
                    "source_revision": {"type": "string"},
                    **{name: {"type": "array", "items": {"type": "string"}}
                       for name in ("symbol_ids", "test_ids", "observed_checks")},
                }},
                **{name: {"type": "array", "items": {"type": "string"}} for name in ("files", "depends_on", "contradicts", "supersedes", "summarizes")}}}}},
    },
    {
        "name": "reql_work_claim_next",
        "description": "Write: atomically claim a ready task or resume this session's current claim. Returns scoped work and overlapping assignments.",
        "inputSchema": {"type": "object", "required": ["storage_path", "agent_id", "session_id"], "properties": {
            "storage_path": {"type": "string"}, "agent_id": {"type": "string"},
            "session_id": {"type": "string"}, "workstream": {"type": "string"}}},
    },
    {
        "name": "query_context",
        "description": "Read-only: source owners, tests, impact and scoped work, with optional verified source excerpts.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "query"],
            "properties": {
                "storage_path": {"type": "string"},
                "query": {"type": "string"},
                "task_id": {"type": ["string", "null"], "default": None, "description": "Attach this work record and its causal context."},
                "mode": {"type": "string", "enum": ["informative", "cleanup"], "default": "informative"},
                "scopes": {
                    "type": ["array", "null"],
                    "items": {"type": "string", "enum": ["code", "docs", "test"]},
                    "uniqueItems": True,
                    "default": None,
                },
                "include_source": {"type": "boolean", "default": False, "description": "Embed bounded current source verified against the committed graph. Requires code or test scopes without docs."},
                "code": {"type": "boolean", "default": False},
                "docs": {"type": "boolean", "default": False},
                "test": {"type": "boolean", "default": False},
                "top_k": {
                    "type": "integer",
                    "default": DEFAULT_CONTEXT_TOP_K,
                    "minimum": MIN_QUERY_CONTEXT_TOP_K,
                    "maximum": MAX_QUERY_CONTEXT_TOP_K,
                },
                "max_depth": {
                    "type": "integer",
                    "default": DEFAULT_CONTEXT_MAX_DEPTH,
                    "minimum": MIN_CONTEXT_DEPTH,
                    "maximum": MAX_CONTEXT_DEPTH,
                },
                "max_items": {
                    "type": "integer",
                    "default": DEFAULT_CONTEXT_MAX_ITEMS,
                    "minimum": MIN_QUERY_CONTEXT_ITEMS,
                    "maximum": MAX_QUERY_CONTEXT_ITEMS,
                },
                "include_archived": {"type": "boolean", "default": False},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "query_graph",
        "description": "Read-only: nodes, edges and sources for graph debugging. Prefer query_context for coding tasks.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "query"],
            "properties": {
                "storage_path": {"type": "string"},
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 12, "maximum": MAX_TOP_K},
                "max_depth": {"type": "integer", "default": 2, "maximum": MAX_DEPTH},
                "max_nodes": {"type": "integer", "default": 80, "maximum": MAX_CONTEXT_NODES},
                "max_edges": {"type": "integer", "default": 160, "maximum": MAX_CONTEXT_EDGES},
                "max_sources": {"type": "integer", "default": 20, "maximum": MAX_ITEMS},
                "max_items": {"type": "integer", "default": 18, "maximum": MAX_ITEMS},
                "filter_generic": {"type": "boolean", "default": True},
                "include_archived": {"type": "boolean", "default": False},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "query_explore",
        "description": "Read-only: missing dependency/refactor evidence or template duplicates. Select views; reuse existing query_context impact.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "query"],
            "properties": {
                "storage_path": {"type": "string"},
                "query": {"type": "string"},
                "views": {
                    "type": ["array", "null"],
                    "default": None,
                    "items": {"type": "string", "enum": ["all", "owners", "callers", "public_surface", "serialization_paths", "docs_mentions", "structural_duplicates", "code"]},
                },
                "top_k": {"type": "integer", "default": 12, "maximum": MAX_TOP_K},
                "max_depth": {"type": "integer", "default": 3, "maximum": MAX_DEPTH},
                "limit": {"type": "integer", "default": 12, "maximum": MAX_ITEMS},
                "max_items": {"type": "integer", "default": 18, "maximum": MAX_ITEMS},
                "include_archived": {"type": "boolean", "default": False},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "query_memories",
        "description": "Read-only: short ranked memory/source texts without graph-debug fields.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "query"],
            "properties": {
                "storage_path": {"type": "string"},
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 12, "maximum": MAX_TOP_K},
                "max_depth": {"type": "integer", "default": 2, "maximum": MAX_DEPTH},
                "limit": {"type": "integer", "default": 12, "maximum": MAX_ITEMS},
                "include_sources": {"type": "boolean", "default": True},
                "filter_generic": {"type": "boolean", "default": True},
                "max_text_chars": {"type": "integer", "default": 600, "maximum": MAX_MEMORY_TEXT_CHARS},
                "include_archived": {"type": "boolean", "default": False},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "reql_query",
        "description": "Read-only: execute a REQL statement and return bounded rows.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "statement"],
            "properties": {
                "storage_path": {"type": "string"},
                "statement": {"type": "string"},
                "limit": {"type": ["integer", "null"], "default": None, "maximum": MAX_QUERY_ROWS},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "reql_project_status",
        "description": "Read-only: inspect registered project and cache status for a path.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "path"],
            "properties": {
                "storage_path": {"type": "string"},
                "path": {"type": "string"},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "reql_hubs",
        "description": "Write/update: analyze graph hubs and persist hub scores. Use with client-side approval.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path"],
            "properties": {
                "storage_path": {"type": "string"},
                "project_id": {"type": ["string", "null"], "default": None},
                "limit": {"type": "integer", "default": 10, "maximum": MAX_TOP_K},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
    {
        "name": "reql_compile_project",
        "description": "Write/update: scan and incrementally compile a project into the graph. Use with client-side approval.",
        "inputSchema": {
            "type": "object",
            "required": ["storage_path", "path"],
            "properties": {
                "storage_path": {"type": "string"},
                "path": {"type": "string"},
                "cache_enabled": {"type": ["boolean", "null"], "default": None},
                "config_path": {"type": ["string", "null"], "default": None},
                "config_overrides": {"type": ["object", "null"], "default": None},
            },
        },
    },
]


def list_tools(*, include_write: bool = True) -> list[dict[str, Any]]:
    """Return MCP tool descriptors."""
    if include_write:
        return list(TOOL_SCHEMAS)
    return [schema for schema in TOOL_SCHEMAS if schema["name"] in READ_ONLY_TOOLS]


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dispatch a tool call to a pure handler."""
    if name not in TOOL_HANDLERS:
        raise MCPToolError(f"Unknown REQL MCP tool: {name}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise MCPToolError("Tool arguments must be an object")
    try:
        return _agent_safe_jsonable(_jsonable(TOOL_HANDLERS[name](**arguments)))
    except TypeError as exc:
        raise MCPToolError(f"Invalid arguments for {name}: {exc}") from exc
    except SecurityError as exc:
        raise MCPToolError(str(exc)) from exc


def _load_tool_config(
    config_path: str | None,
    config_overrides: dict[str, Any] | None,
    *,
    start_dir: str | Path | None = None,
) -> REQLConfig:
    if config_overrides is not None and not isinstance(config_overrides, dict):
        raise MCPToolError("config_overrides must be an object when provided")
    if config_path is not None:
        config_path = str(validate_mcp_path(config_path, name="config_path", must_exist=True))
    try:
        return load_effective_config(config_path, start_dir=start_dir, overrides=config_overrides or None)
    except ConfigError as exc:
        raise MCPToolError(str(exc)) from exc


def _reql_statement_requires_write(statement: str) -> bool:
    first = statement.strip().split(None, 1)[0].rstrip(";").upper() if statement.strip() else ""
    return first in MUTATING_REQL_COMMANDS


def _project_root_for_storage(storage: Path) -> Path:
    """Infer the project directory for stores in the default .reql location."""
    directory = storage.expanduser().resolve().parent
    return directory.parent if directory.name == ".reql" else Path.cwd()


def _tool_storage(storage_path: str) -> Path:
    return validate_mcp_path(_required_path_text(storage_path, "storage_path"), name="storage_path")


@contextmanager
def _open_graph(storage_path: str, config: REQLConfig, *, read_only: bool = False) -> Iterator[MemoryGraph]:
    storage = _tool_storage(storage_path)
    graph = MemoryGraph.open(
        storage,
        config=config,
        read_only=read_only,
        defer_lexical_index=read_only,
        lock_timeout_seconds=0.05 if read_only else None,
    )
    try:
        yield graph
    finally:
        graph.close()


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MCPToolError(f"{name} must be a non-empty string")
    return value


def _required_path_text(value: Any, name: str) -> str:
    text = _required_text(value, name)
    if "\x00" in text:
        raise MCPToolError(f"{name} cannot contain NUL bytes")
    return text


def _optional_string_list(value: Any, name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise MCPToolError(f"{name} must be an array of strings when provided")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MCPToolError(f"{name} must contain only non-empty strings")
        result.append(item)
    return result


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise MCPToolError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MCPToolError(f"{name} must be an integer") from exc
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _bounded_optional_int(value: Any, name: str, *, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    return _bounded_int(value, name, minimum=minimum, maximum=maximum)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if is_dataclass(value):
        return _jsonable(asdict(value))
    return str(value)


def _agent_safe_jsonable(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_agent_text(value)
    if isinstance(value, dict):
        return {str(key): _agent_safe_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_agent_safe_jsonable(item) for item in value]
    return value

