"""Application facade for the graph-native memory layer."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from memory.config import REQLConfig, default_config
from memory.diagnostics import PerformanceLogger
from memory.domain.models import (
    ActivationOptions,
    ActivationResult,
    MemoryEdge,
    MemoryNode,
    MemoryQuery,
    MemorySubgraph,
)
from memory.domain.query_context import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_ITEMS,
    DEFAULT_TOP_K,
    ContextResult,
    QueryContextRequest,
)
from memory.engines.activation import ActivationEngine
from memory.engines.salience import SalienceEngine
from memory.extraction.deterministic import DeterministicExtractor
from memory.storage import BlockGraphStore, GraphStore, SemanticExtractor
from memory.reporting.html_graph import write_graph_html
from memory.reporting.project_report import ProjectReportFiles, ProjectReportGenerator
from memory.analysis.communities import CommunityResult
from memory.analysis.hubs import HubReport
from memory.artifacts.models import ScanError, ScanResult
from memory.artifacts.project import ProjectRegistry
from memory.artifacts.revision import ProjectRevision, RevisionRepository
from memory.artifacts.scanner import ProjectScanner
from memory.artifacts.fingerprint import normalize_relative_lookup_path
from memory.artifacts.options import CompilationOptions
from memory.services.incremental_compilation import CompileProjectResult, IncrementalCompilationService
from memory.services.query_context import QueryContextService
from memory.services.coordination import CoordinationStore
from memory.services.failure_context import FailureContextService
from memory.services.retrieval import RetrievalEngine
from memory.explanation import RepositoryExplanation, RepositoryExplanationService
from memory.pipeline import ProjectPipeline, ProjectPipelineService
from memory.testing import TestAssociation


MemoryGraphT = TypeVar("MemoryGraphT", bound="MemoryGraph")


def _scan_workspace(
    root: Path, revision: ProjectRevision | None, scanner: ProjectScanner | None,
) -> tuple[dict[str, Any], ScanResult | None]:
    """Compare a complete live scan with a revision and retain it for further checks."""
    workspace: dict[str, Any] = {
        "freshness": "unknown", "source_revision": revision.id if revision else None, "changes": [], "errors": [],
    }
    if scanner is None:
        return workspace, None

    try:
        scan = scanner.scan(root)
    except OSError as exc:
        workspace["errors"] = [ScanError(str(root), "", str(exc)).to_dict()]
        return workspace, None
    if scan.errors:
        workspace["errors"] = [error.to_dict() for error in scan.errors]
        return workspace, None
    if revision is not None:
        changes = revision.compare(scan.artifacts)
        workspace["freshness"] = "stale" if changes else "current"
        workspace["changes"] = [change.to_dict() for change in changes]
    return workspace, scan


class MemoryGraph:
    """Stable public facade over the memory subsystem.

    The facade wires the domain services without leaking storage details. The
    bundled ``open`` constructor uses ``BlockGraphStore`` for local persistence,
    but callers may inject any ``GraphStore`` implementation that satisfies the
    port.
    """

    def __init__(
        self,
        store: GraphStore,
        extractor: SemanticExtractor | None = None,
        *,
        config: REQLConfig | None = None,
        profile_logger: PerformanceLogger | None = None,
    ) -> None:
        self.store = store
        self.config = config or default_config()
        self.profile_logger = profile_logger
        self.extractor = extractor or DeterministicExtractor(
            policy=self.config.lexical.seeds, normalization=self.config.lexical.normalization,
        )
        analysis = self.config.analysis
        self.activation = ActivationEngine(store, policy=analysis.activation)
        self.retrieval = RetrievalEngine(store, self.extractor, profile_logger=profile_logger, config=self.config)
        self.query_context_service = QueryContextService(self.retrieval, config=self.config)
        self.salience = SalienceEngine(store, policy=analysis.salience)
        self.projects = ProjectRegistry(store)
        self.test_intelligence = self.retrieval.test_intelligence
        self.revisions = RevisionRepository(store)
        self.repository_explainer = RepositoryExplanationService(store, config=analysis)
        self.project_pipeline_service = ProjectPipelineService(store, config=analysis)
        self.incremental = IncrementalCompilationService(
            store,
            compile_options=CompilationOptions.from_config(self.config),
            scan_config=self.config.scan,
            cache_enabled=self.config.cache.enabled,
            retention_commits=self.config.retention.commits,
            profile_logger=profile_logger,
        )
        self.project_reporter = ProjectReportGenerator(store, compilation=self.incremental, config=self.config)
        from memory.analysis.communities import CommunityDetector
        from memory.analysis.hubs import HubAnalyzer

        self.community_detector = CommunityDetector(store, policy=analysis.communities)
        self.hub_analyzer = HubAnalyzer(store, config=analysis)

    @classmethod
    def open(
        cls: type[MemoryGraphT],
        path: str | Path,
        *,
        extractor: SemanticExtractor | None = None,
        config: REQLConfig | None = None,
        profile_logger: PerformanceLogger | None = None,
        read_only: bool = False,
        defer_lexical_index: bool = False,
        lock_timeout_seconds: float | None = None,
        allow_empty_read_only: bool = False,
        wait_for_lock: bool = False,
    ) -> MemoryGraphT:
        """Open graph storage with the selected access and lock policy."""
        selected_config = config or default_config()
        store = BlockGraphStore(
            path, read_only=read_only, allow_empty_read_only=allow_empty_read_only,
            defer_lexical_index=defer_lexical_index, lock_timeout_seconds=lock_timeout_seconds,
            wait_for_lock=wait_for_lock, policy=selected_config.storage, lexical_config=selected_config.lexical,
        )
        try:
            return cls(store, extractor=extractor, config=selected_config, profile_logger=profile_logger)
        except Exception:
            store.close()
            raise
    def enable_profile_log(self, path: str | Path, *, command: str | None = None) -> PerformanceLogger:
        logger = PerformanceLogger(path, command=command)
        self.profile_logger = logger
        self.retrieval.profile_logger = logger
        self.incremental.profile_logger = logger
        return logger

    def close(self) -> None:
        if self.profile_logger:
            with self.profile_logger.span("graph.close"):
                self.store.close()
            return
        self.store.close()

    def retrieve(
        self,
        text: str,
        *,
        top_k: int = 20,
        max_depth: int = 3,
        min_activation: float = 0.03,
        include_archived: bool = False,
        node_types: set[str] | None = None,
        edge_types: set[str] | None = None,
    ) -> MemorySubgraph:
        """Return the raw retrieved subgraph for callers that need graph records."""
        return self.retrieval.retrieve(
            MemoryQuery(
                text=text,
                top_k=top_k,
                max_depth=max_depth,
                min_activation=min_activation,
                include_archived=include_archived,
                node_types=node_types,
                edge_types=edge_types,
            )
        )

    def compose_context(
        self,
        text: str,
        *,
        top_k: int = 20,
        max_depth: int = 3,
        max_items: int = 18,
        include_archived: bool = False,
    ) -> str:
        subgraph = self.retrieval.retrieve(
            MemoryQuery(
                text=text,

                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
            )
        )
        return self.retrieval.compose_context(subgraph, max_items=max_items)

    def query_graph(
        self,
        text: str,
        *,
        top_k: int = 12,
        max_depth: int = 2,
        max_nodes: int = 80,
        max_edges: int = 160,
        max_sources: int = 20,
        max_items: int = 18,
        filter_generic: bool = True,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return self.retrieval.query_graph(
            MemoryQuery(
                text=text,

                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
            ),
            max_nodes=max_nodes,
            max_edges=max_edges,
            max_sources=max_sources,
            max_items=max_items,
            filter_generic=filter_generic,
        )

    def query_explore(
        self,
        text: str,
        *,
        views: list[str] | None = None,
        top_k: int = 12,
        max_depth: int = 3,
        limit: int = 12,
        max_items: int = 18,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Return dependency and structural-refactor query slices for coding agents."""
        return self.retrieval.query_explore(
            MemoryQuery(
                text=text,

                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
            ),
            views=views,
            limit=limit,
            max_items=max_items,
        )

    def query_context(
        self,
        text: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_items: int = DEFAULT_MAX_ITEMS,
        mode: str = "informative",
        scopes: list[str] | set[str] | tuple[str, ...] | None = None,
        include_archived: bool = False,
        include_source: bool = False,
        task_id: str | None = None,
    ) -> str:
        """Return the compact deterministic context block for a query."""
        result = self.query_context_result(
            QueryContextRequest.from_raw(
                text=text,
                mode=mode,
                scopes=scopes,
                top_k=top_k,
                max_depth=max_depth,
                max_items=max_items,
                include_archived=include_archived,
                include_source=include_source,
                task_id=task_id,
            )
        )
        return self.query_context_service.render(result)

    def query_context_result(self, request: QueryContextRequest) -> ContextResult:
        """Execute the canonical typed query-context request."""
        if not isinstance(request, QueryContextRequest):
            raise TypeError("request must be a QueryContextRequest")
        return self.query_context_service.execute(request)

    def query_context_payload(
        self,
        text: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_items: int = DEFAULT_MAX_ITEMS,
        mode: str = "informative",
        scopes: list[str] | set[str] | tuple[str, ...] | None = None,
        include_archived: bool = False,
        include_source: bool = False,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the versioned structured query-context envelope."""
        return self.query_context_result(
            QueryContextRequest.from_raw(
                text=text,
                mode=mode,
                scopes=scopes,
                top_k=top_k,
                max_depth=max_depth,
                max_items=max_items,
                include_archived=include_archived,
                include_source=include_source,
                task_id=task_id,
            )
        ).to_dict()

    def query_memories(
        self,
        text: str,
        *,
        top_k: int = 12,
        max_depth: int = 2,
        limit: int = 12,
        include_sources: bool = True,
        filter_generic: bool = True,
        max_text_chars: int = 600,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return self.retrieval.query_memories(
            MemoryQuery(
                text=text,

                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
            ),
            limit=limit,
            include_sources=include_sources,
            filter_generic=filter_generic,
            max_text_chars=max_text_chars,
        )

    def query_memories_payload(
        self,
        text: str,
        *,
        top_k: int = 12,
        max_depth: int = 2,
        limit: int = 12,
        include_sources: bool = True,
        filter_generic: bool = True,
        max_text_chars: int = 600,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return self.retrieval.query_memories_payload(
            MemoryQuery(
                text=text,

                top_k=top_k,
                max_depth=max_depth,
                include_archived=include_archived,
            ),
            limit=limit,
            include_sources=include_sources,
            filter_generic=filter_generic,
            max_text_chars=max_text_chars,
        )

    def activate(
        self,
        seed_node_ids: list[str],
        *,
        max_depth: int = 3,
        min_activation: float = 0.03,
    ) -> ActivationResult:
        return self.activation.activate(
            seed_node_ids,
            ActivationOptions(max_depth=max_depth, min_activation=min_activation),
        )

    def project_report(
        self, path: str | Path, *, output_dir: str | Path, config_path: str | Path | None = None,
    ) -> ProjectReportFiles:
        """Write reports using the same source and cache policy as compilation."""
        return self.project_reporter.write_reports(path, output_dir=output_dir, config_path=config_path)

    def export_json(self) -> dict[str, Any]:
        return self.store.export_json()

    def export_html(self, output_path: str | Path) -> Path:
        return write_graph_html(self.export_json(), output_path, config=self.config)

    def query(self, statement: str):
        """Execute a REQL statement.

        REQL is parsed into an AST and evaluated against the graph facade, so the
        public query layer remains independent from the physical storage adapter.
        """
        from memory.query.evaluator import execute_reql

        if self.profile_logger:
            with self.profile_logger.span("query.total", statement_length=len(statement)):
                return execute_reql(self, statement)
        return execute_reql(self, statement)

    def project_status(self, path: str | Path, *, config_path: Path | None = None) -> dict[str, Any] | None:
        """Report compilation history and compare the live source with its revision."""
        status = self.projects.project_status(path)
        if status is not None:
            status["workspace"] = self.workspace_status(path, config_path=config_path)
        return status

    def project_diagnostics(
        self, path: str | Path, *, config_path: Path | None = None, max_file_size_mb: float | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, object] | None]:
        """Read compilation, source freshness, and cache status using one live scan."""
        root = Path(path).expanduser().resolve()
        project = self.projects.project_status(root)
        revision = self.revisions.latest(project["project"]["id"]) if project else None
        scan_config = self.config.scan
        if max_file_size_mb is not None:
            scan_config = replace(scan_config, max_file_size_mb=max_file_size_mb)
        scanner = ProjectScanner.from_config(scan_config, config_path=config_path)
        workspace, scan = _scan_workspace(root, revision, scanner)
        cache = (
            self.incremental.cache_status_for_scan(scan, cache_enabled=self.config.cache.enabled)
            if scan is not None else None
        )
        return project, workspace, cache

    def list_workspaces(self) -> list[dict[str, str | None]]:
        """List registered project roots and their head revisions without scanning source."""
        workspaces = []
        for project in self.store.find_nodes_by_types(("Project",), clone=False):
            root = project.properties.get("root_path")
            if isinstance(root, str) and root:
                workspaces.append({
                    "project_id": project.id,
                    "root_path": root,
                    "name": project.label,
                    "source_revision": self.revisions.latest_id(project.id),
                })
        return sorted(workspaces, key=lambda workspace: workspace["root_path"] or "")

    def workspace_status(self, path: str | Path = ".", *, config_path: Path | None = None) -> dict[str, Any]:
        """Compare this folder with the latest committed REQL manifest."""
        root = Path(path).expanduser().resolve()
        project = self.store.get_node_by_key("Project", root.as_posix())
        latest = self.revisions.latest(project.id) if project else None
        scanner = ProjectScanner.from_config(self.config.scan, config_path=config_path) if latest else None
        workspace, _ = _scan_workspace(root, latest, scanner)
        return workspace

    def find_tests_for(self, *, symbol_id: str | None = None, file: str | None = None,
                       include_candidates: bool = False, limit: int = 20) -> list[TestAssociation]:
        """Find runtime and static test evidence for a symbol or relative file."""
        return self.test_intelligence.find(symbol_id=symbol_id, file=file,
                                           include_candidates=include_candidates, limit=limit)

    def failure_context(self, query: str = "", *, files: list[str] | None = None,
                        symbol_ids: list[str] | None = None, record_id: str | None = None,
                        workstream: str | None = None, limit: int | None = None) -> dict[str, Any]:
        """Recover scoped failure records from the associated engineering store."""
        storage = getattr(self.store, "path", None)
        if storage is None:
            raise ValueError("Failure context requires a persistent engineering store")
        work = CoordinationStore.for_project(storage, config=self.config)
        return FailureContextService(work, self.store).context(query, files=files, symbol_ids=symbol_ids,
                                                               record_id=record_id, workstream=workstream, limit=limit)

    def explain_project(
        self,
        path: str | Path,
        *,
        focus: str | None = None,
        max_capabilities: int = 12,
        max_workflows: int = 8,
    ) -> RepositoryExplanation:
        """Return a deterministic business-oriented explanation of a project."""

        status = self.projects.project_status(path)
        if status is None:
            raise ValueError(f"Project not found: {path}")
        project = self.store.get_node(str(status["project"]["id"]), clone=False)
        if project is None:
            raise ValueError(f"Project not found: {path}")
        return self.repository_explainer.explain(
            project,
            focus=focus,
            max_capabilities=max_capabilities,
            max_workflows=max_workflows,
        )

    def project_pipeline(self, path: str | Path) -> ProjectPipeline:
        """Return a deterministic high-level flow projection for a project."""

        status = self.projects.project_status(path)
        if status is None:
            raise ValueError(f"Project not found: {path}")
        project = self.store.get_node(str(status["project"]["id"]), clone=False)
        if project is None:
            raise ValueError(f"Project not found: {path}")
        return self.project_pipeline_service.build(project)

    def project_history(self, path: str | Path, *, limit: int = 20) -> list[ProjectRevision]:
        """Return newest-first immutable revisions for a registered project."""
        status = self.projects.project_status(path)
        if status is None:
            return []
        project_id = str(status["project"]["id"])
        return self.revisions.list(project_id=project_id, limit=limit)

    def project_revision(self, revision_id: str) -> ProjectRevision | None:
        """Return one project revision by its content-addressed id."""
        return self.revisions.get(revision_id)

    def compile_project(
        self,
        path: str | Path,
        *,
        max_file_size_bytes: int | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        config_path: str | Path | None = None,
        cache_enabled: bool | None = None,
        parsing_options: CompilationOptions | None = None,
    ) -> CompileProjectResult:
        return self.incremental.compile_path(
            path,
            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            config_path=config_path,
            cache_enabled=cache_enabled,
            parsing_options=parsing_options,
        )

    def update_project(
        self,
        path: str | Path,
        *,
        max_file_size_bytes: int | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        config_path: str | Path | None = None,
        cache_enabled: bool | None = None,
        parsing_options: CompilationOptions | None = None,
    ) -> CompileProjectResult:
        """Incrementally update a project through the compile pipeline."""
        return self.compile_project(
            path,
            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            config_path=config_path,
            cache_enabled=cache_enabled,
            parsing_options=parsing_options,
        )

    def cache_status(
        self,
        path: str | Path,
        *,
        max_file_size_bytes: int | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        config_path: str | Path | None = None,
        cache_enabled: bool | None = None,
        parsing_options: CompilationOptions | None = None,
    ) -> dict[str, object]:
        return self.incremental.cache_status(
            path,
            max_file_size_bytes=max_file_size_bytes,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            config_path=config_path,
            cache_enabled=cache_enabled,
            parsing_options=parsing_options,
        )

    def clear_cache(self, path: str | Path) -> dict[str, object]:
        return self.incremental.clear_cache(path)

    def list_deltas(self, *, limit: int = 20):
        return self.incremental.list_deltas(limit=limit)

    def show_delta(self, delta_id: str):
        return self.incremental.show_delta(delta_id)

    def detect_communities(
        self,
        project_id: str | None = None,
        options: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> CommunityResult:
        return self.community_detector.detect(project_id=project_id, options=options, limit=limit)

    def analyze_hubs(
        self,
        project_id: str | None = None,
        limit: int | None = 20,
        node_types: set[str] | None = None,
    ) -> HubReport:
        return self.hub_analyzer.analyze(project_id=project_id, limit=limit, node_types=node_types)

    def explain_hub(self, node_id: str, *, complete: bool = False):
        return self.hub_analyzer.explain(node_id, complete=complete)

    def add_node(self, node: MemoryNode) -> tuple[MemoryNode, bool]:
        return self.store.upsert_node(node)

    def add_edge(self, edge: MemoryEdge) -> tuple[MemoryEdge, bool]:
        return self.store.upsert_edge(edge)

    def get_node(self, node_id: str) -> MemoryNode | None:
        return self.store.get_node(node_id)

    def get_edges(self, **kwargs: Any) -> list[MemoryEdge]:
        return self.store.get_edges(**kwargs)

    def locate(self, path: str, *, include_archived: bool = False) -> dict[str, Any]:
        """Resolve a known project-relative path through exact property indexes."""
        relative_path = normalize_relative_lookup_path(path)
        if not relative_path:
            raise ValueError("locate path must not be empty")
        normalized = relative_path.casefold()
        suffixes = (".md", ".markdown", ".txt", ".rst") if not Path(relative_path).suffix else ()
        key_candidates = [normalized, *(f"{normalized}{suffix}" for suffix in suffixes)]
        matches: dict[str, MemoryNode] = {}
        for candidate in key_candidates:
            for node in self.store.find_nodes_by_property(
                "relative_path_key",
                candidate,
                type_="SourceArtifact",
                limit=100,
                clone=False,
            ):
                if include_archived or node.status not in {"archived", "deleted"}:
                    matches[node.id] = node
        rows = []
        for node in sorted(matches.values(), key=lambda item: str(item.properties.get("relative_path") or "").casefold()):
            properties = node.properties
            rows.append(
                {
                    "id": node.id,
                    "relative_path": properties.get("relative_path"),
                    "path": properties.get("path"),
                    "uri": properties.get("uri") or node.text,
                    "artifact_type": properties.get("artifact_type"),
                    "language": properties.get("language"),
                    "context_scope": properties.get("context_scope"),
                    "status": node.status,
                }
            )
        return {"query": path, "normalized_path": normalized, "matches": rows}
