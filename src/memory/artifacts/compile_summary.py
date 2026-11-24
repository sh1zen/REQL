"""Deterministic post-compile summaries for coding-agent verification."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable

from ..config.policies import TestDiscoveryPolicy
from ..domain.models import MemoryNode
from ..storage.graph_store import GraphStore
from .context_scope import is_test_context_path, matches_test_case_name
from .delta import GraphDelta
from .revision import ProjectRevision
from .testing_conventions import candidate_test_stems, project_test_paths, test_path_stem

CODE_SYMBOL_TYPES = {"Module", "Function", "Class", "Interface", "Method", "Endpoint"}
ASSOCIATION_EDGE_TYPES = {
    "CALLS",
    "IMPORTS",
    "IMPORTS_FROM",
    "INSTANTIATES",
    "OVERRIDES",
    "READS",
    "REFERENCES",
    "RE_EXPORTS",
    "RETURNS",
    "TESTS",
}
LEXICAL_ASSOCIATION_NODE_TYPES = {"Module", "Function", "Class", "Method", "Test", "SourceFragment"}
_LEXICAL_TEST_PATH_LIMIT = 8
_NON_SEMANTIC_SYMBOL_FIELDS = {
    "line",
    "column",
    "lineno",
    "end_lineno",
    "col_offset",
    "end_col_offset",
    "created_at",
    "updated_at",
    "source_path",
    "start_line",
    "end_line",
    "line_start",
    "line_end",
    "start_column",
    "end_column",
    "column_start",
    "column_end",
}

SymbolState = dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True)
class UpdatedSymbol:
    id: str
    type: str
    name: str
    relative_path: str
    status: str
    line_start: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "relative_path": self.relative_path,
            "status": self.status,
            "line_start": self.line_start,
        }


@dataclass(frozen=True, slots=True)
class AssociatedTest:
    path: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "reason": self.reason}


@dataclass(slots=True)
class CompilationSummary:
    changed_files: list[dict[str, object]] = field(default_factory=list)
    updated_symbols: list[UpdatedSymbol] = field(default_factory=list)
    associated_tests: list[AssociatedTest] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "changed_files": [dict(item) for item in self.changed_files],
            "updated_symbols": [item.to_dict() for item in self.updated_symbols],
            "associated_tests": [item.to_dict() for item in self.associated_tests],
        }


def build_compilation_summary(
    store: GraphStore,
    *,
    revision: ProjectRevision | None,
    delta: GraphDelta,
    previous_symbol_state: SymbolState | None = None,
    discovery: TestDiscoveryPolicy | None = None,
) -> CompilationSummary:
    """Build a summary from the revision and the graph delta just persisted."""
    if revision is None:
        return CompilationSummary()
    changed_files = [change.to_dict() for change in revision.changes]
    changed_paths = {_normalize_path(str(item["path"])) for item in changed_files}
    node_status = _node_statuses(delta)
    nodes = store.get_nodes(sorted(node_status), clone=False)
    changed_nodes = [
        node for node in nodes
        if node.properties.get("project_id") == revision.project_id and _relative_path(node) in changed_paths
    ]
    current_symbol_state = capture_symbol_state(
        store,
        (str(item.get("artifact_id") or "") for item in changed_files),
    )
    updated_symbols = [
        UpdatedSymbol(
            id=node.id,
            type=node.type,
            name=_symbol_name(node),
            relative_path=_relative_path(node),
            status=node_status[node.id],
            line_start=_line_start(node),
        )
        for node in changed_nodes
        if node.type in CODE_SYMBOL_TYPES
        and (
            node_status[node.id] != "updated"
            or previous_symbol_state is None
            or previous_symbol_state.get(node.id) != current_symbol_state.get(node.id)
        )
    ]
    updated_symbols.sort(key=lambda item: (item.relative_path, item.line_start or 0, item.type, item.name, item.id))
    associated_tests = _associated_tests(
        store, revision=revision, changed_paths=changed_paths, seed_nodes=changed_nodes,
        discovery=discovery,
    )
    return CompilationSummary(
        changed_files=changed_files,
        updated_symbols=updated_symbols,
        associated_tests=associated_tests,
    )


def capture_symbol_state(store: GraphStore, artifact_ids: Iterable[str]) -> SymbolState:
    """Capture semantic code-symbol state for a bounded set of artifacts."""
    nodes: dict[str, MemoryNode] = {}
    for artifact_id in sorted({str(item) for item in artifact_ids if item}):
        for node in store.find_nodes_by_property(
            "artifact_id",
            artifact_id,
            status="active",
            limit=100000,
            clone=False,
        ):
            nodes[node.id] = node

    bodies_by_symbol: dict[str, list[str]] = {}
    for node in nodes.values():
        if node.type != "SourceFragment":
            continue
        symbol_id = str(node.properties.get("symbol_id") or "")
        if symbol_id:
            bodies_by_symbol.setdefault(symbol_id, []).append(node.text)

    return {
        node.id: _symbol_state(node, bodies_by_symbol.get(node.id, []))
        for node in nodes.values()
        if node.type in CODE_SYMBOL_TYPES
    }


def _symbol_state(node: MemoryNode, bodies: list[str]) -> dict[str, object]:
    return {
        "type": node.type,
        "label": node.label,
        "text": node.text,
        "canonical_key": node.canonical_key,
        "status": node.status,
        "properties": _semantic_value(node.properties),
        "bodies": sorted(bodies),
    }


def _semantic_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _semantic_value(item)
            for key, item in value.items()
            if str(key) not in _NON_SEMANTIC_SYMBOL_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_value(item) for item in value]
    return value


def _node_statuses(delta: GraphDelta) -> dict[str, str]:
    statuses = dict.fromkeys(delta.added_nodes, "added")
    statuses.update(dict.fromkeys(delta.updated_nodes, "updated"))
    statuses.update(dict.fromkeys(delta.archived_nodes, "archived"))
    return statuses


def _associated_tests(
    store: GraphStore,
    *,
    revision: ProjectRevision,
    changed_paths: set[str],
    seed_nodes: list[MemoryNode],
    discovery: TestDiscoveryPolicy | None,
) -> list[AssociatedTest]:
    """Find test evidence within the project that produced the revision."""
    if not changed_paths:
        return []
    project_id = revision.project_id
    test_paths = set(project_test_paths(store, project_id))
    changed_tests = {path for path in changed_paths if path in test_paths
                     or (path not in revision.manifest and is_test_context_path(path, discovery))}
    reasons = dict.fromkeys(changed_tests, "changed test file")
    sources = changed_paths - changed_tests
    source_nodes = [node for node in seed_nodes if _relative_path(node) in sources]
    source_ids = {node.id for node in source_nodes}
    neighbors = {
        other for edge in store.incident_edges(sorted(source_ids), edge_types=ASSOCIATION_EDGE_TYPES,
                                               limit=None, clone=False)
        if edge.properties.get("status") != "archived"
        for other in (edge.from_id, edge.to_id) if other not in source_ids
    } if source_ids else set()
    for neighbor in store.get_nodes(sorted(neighbors), clone=False):
        path = _relative_path(neighbor)
        if neighbor.status == "active" and neighbor.properties.get("project_id") == project_id and path in test_paths:
            reasons.setdefault(path, "direct graph relationship")

    stems = {stem for path in sources for stem in candidate_test_stems(path)}
    for path in test_paths:
        if test_path_stem(path) in stems:
            reasons.setdefault(path, "matching test filename")

    terms = _association_terms(sources, source_nodes)
    if terms:
        lexical_tests = 0
        for node, _score in store.lexical_search(
            " ".join(terms),
            top_k=None,
            node_types=LEXICAL_ASSOCIATION_NODE_TYPES,
            project_id=project_id,
            clone=False,
        ):
            path = _relative_path(node)
            if path not in test_paths or path in reasons or not _node_matches_terms(node, terms):
                continue
            reasons[path] = "changed symbol match"
            lexical_tests += 1
            if lexical_tests >= _LEXICAL_TEST_PATH_LIMIT:
                break

    return [AssociatedTest(path=path, reason=reasons[path]) for path in sorted(reasons)]


def _association_terms(paths: set[str], nodes: list[MemoryNode]) -> list[str]:
    """Collect deterministic lexical terms from changed source paths and symbols."""
    terms = {PurePosixPath(path).stem for path in paths}
    for node in nodes:
        if node.type not in CODE_SYMBOL_TYPES:
            continue
        name = _symbol_name(node).rsplit(".", 1)[-1].strip("_")
        if len(name) >= 4 and not matches_test_case_name(name):
            terms.add(name)
    return sorted(terms)[:40]


def _node_matches_terms(node: MemoryNode, terms: list[str]) -> bool:
    haystack = " ".join(
        (
            _relative_path(node),
            _symbol_name(node),
            str(node.label or ""),
            str(node.text or ""),
        )
    ).casefold()
    return any(term.casefold() in haystack for term in terms)


def _relative_path(node: MemoryNode) -> str:
    return _normalize_path(str(node.properties.get("relative_path") or node.properties.get("source_file") or ""))


def _symbol_name(node: MemoryNode) -> str:
    properties: dict[str, Any] = node.properties
    return str(properties.get("qualified_name") or properties.get("name") or node.label or node.id)


def _line_start(node: MemoryNode) -> int | None:
    value = node.properties.get("line_start", node.properties.get("start_line"))
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")
