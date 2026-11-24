"""Incremental project compilation orchestration."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..artifacts.cache import (
    ArtifactCache,
    ArtifactCacheEntry,
    DirtySet,
    artifact_cache_path,
)
from ..artifacts.compile_summary import (
    CompilationSummary,
    build_compilation_summary,
    capture_symbol_state,
)
from ..artifacts.compiler import (
    ArtifactCompiler,
    ArtifactContentChangedError,
    archive_artifact_fragments,
    link_document_fragments_to_code,
    refresh_project_css_surface_findings,
)
from ..artifacts.context_scope import application_surface_kind, artifact_context_scope
from ..artifacts.delta import CompilationRun, DeltaRepository, GraphDelta
from ..artifacts.fingerprint import artifact_id, normalize_path, project_id
from ..artifacts.models import ArtifactCompilationResult, ScanResult, SourceArtifact
from ..artifacts.options import (
    CompilationOptions,
    normalize_compilation_options,
)
from ..artifacts.project import ProjectRegistry
from ..artifacts.revision import ProjectRevision, RevisionRepository
from ..artifacts.scanner import DEFAULT_MAX_FILE_SIZE_BYTES, ProjectScanner
from ..artifacts.structural_links import refresh_project_structural_links
from ..config import default_config
from ..config.models import ScanConfig
from ..diagnostics import PerformanceLogger
from ..domain.constants import PROJECT_USAGE_ARCHIVE_REASON
from ..domain.exceptions import StorageError
from ..domain.ids import stable_id
from ..domain.models import MemoryEdge, MemoryNode
from ..domain.timeutils import utcnow_iso
from ..storage.graph_store import GraphStore
from ..storage.retention import RetentionCleanupResult, prune_project_data
from ..testing.static_links import refresh_static_links

ORPHAN_DIRECTORY_MIN_FILES = 2
ORPHAN_DIRECTORY_FINDING_TYPE = "possibly_orphan_directory"
ORPHAN_DIRECTORY_EDGE_TYPES = {
    "IMPORTS",
    "IMPORTS_FROM",
    "DEPENDS_ON",
    "CALLS",
    "INSTANTIATES",
    "REFERENCES",
    "READS",
    "TESTS",
}


ORPHAN_DIRECTORY_COMMON_ROOTS = {"src", "tests", "test", "docs"}
ORPHAN_DIRECTORY_NAME_HINTS = {"legacy", "old", "unused", "obsolete", "dead", "archive", "archived", "backup", "backups", "deprecated"}
UNUSED_SYMBOL_FINDING_TYPES = {"possibly_unused_function", "possibly_unused_method", "possibly_unused_class"}
UNUSED_SYMBOL_USAGE_EDGE_TYPES = {
    "CALLS",
    "USES",
    "REFERENCES",
    "READS",
    "RETURNS",
    "RAISES",
    "INHERITS",
    "IMPLEMENTS",
    "INSTANTIATES",
    "IMPORTS",
    "RE_EXPORTS",
    "TESTS",
}
UNUSED_SYMBOL_NODE_TYPES = {"Function", "Class", "Interface", "Method"}


@dataclass(slots=True)
class CompileProjectResult:
    scan: ScanResult
    dirty_set: DirtySet
    run: CompilationRun
    delta: GraphDelta
    revision: ProjectRevision | None = None
    summary: CompilationSummary = field(default_factory=CompilationSummary)
    retention: RetentionCleanupResult | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "scan": self.scan.to_dict(),
            "dirty_set": self.dirty_set.to_dict(),
            "run": self.run.to_dict(),
            "delta": self.delta.to_dict(),
            "revision": self.revision.to_dict(include_manifest=False) if self.revision else None,
            "summary": self.summary.to_dict(),
            "retention": self.retention.to_dict() if self.retention else None,
        }


class IncrementalCompilationService:
    """Scans projects, compiles dirty artifacts, updates cache, and records deltas."""

    def __init__(
        self,
        store: GraphStore,
        *,
        compiler: ArtifactCompiler | None = None,
        compile_options: CompilationOptions | None = None,
        scan_config: ScanConfig | None = None,
        cache_enabled: bool = True,
        retention_commits: int = 20,
        profile_logger: PerformanceLogger | None = None,
    ) -> None:
        self.store = store
        self.profile_logger = profile_logger
        self.retention_commits = retention_commits
        self.scan_config = scan_config
        self.cache_enabled = cache_enabled
        self.project_registry = ProjectRegistry(store)
        self.default_options = normalize_compilation_options(
            compiler.options if compiler is not None and compile_options is None else compile_options
        )
        if compiler is not None:
            if compiler.options != self.default_options:
                raise ValueError("Injected compiler and incremental service must share compilation options")
            self.compiler = compiler
        else:
            self.compiler = ArtifactCompiler(options=self.default_options)
        self.cache = self._cache_for_options(self.default_options)
        self.deltas = DeltaRepository(store)
        self.revisions = RevisionRepository(store)

    def compile_path(
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
        cache_enabled = self.cache_enabled if cache_enabled is None else cache_enabled
        profile = self.profile_logger
        compile_start = profile.span("compile.total", path=Path(path).expanduser().resolve(strict=False)) if profile else nullcontext()
        with compile_start:
            for attempt in range(3):
                try:
                    return self._compile_path(
                        path,
                        max_file_size_bytes=max_file_size_bytes,
                        include_patterns=include_patterns,
                        exclude_patterns=exclude_patterns,
                        config_path=config_path,
                        cache_enabled=cache_enabled,
                        parsing_options=parsing_options,
                    )
                except ArtifactContentChangedError as exc:
                    if attempt == 2:
                        raise StorageError(
                            "Project files changed during three consecutive compile attempts; "
                            "the previous committed graph and cache were preserved. Retry when writes settle."
                        ) from exc
            raise AssertionError("unreachable compile retry state")

    def _compile_path(
        self,
        path: str | Path,
        *,

        max_file_size_bytes: int | None,
        include_patterns: list[str] | None,
        exclude_patterns: list[str] | None,
        config_path: str | Path | None,
        cache_enabled: bool,
        parsing_options: CompilationOptions | None,
    ) -> CompileProjectResult:
        profile = self.profile_logger
        root = Path(path).expanduser().resolve(strict=False)
        options = self.default_options if parsing_options is None else normalize_compilation_options(parsing_options)
        compiler = self._compiler_for_options(options)
        cache = self._cache_for_options(options, project_root=root, compiler=compiler)
        cache_entries = cache.project_entry_map(project_id(normalize_path(root)), active_only=True) if cache_enabled else {}
        scanner = self._project_scanner(
            options, max_file_size_bytes=max_file_size_bytes, include_patterns=include_patterns,
            exclude_patterns=exclude_patterns, config_path=config_path,
        )
        with (profile.span("compile.scan", path=Path(path).expanduser().resolve(strict=False)) if profile else nullcontext()):
            scan = scanner.scan(root)
        if profile:
            profile.event("compile.scan.result", category="counter", artifacts=len(scan.artifacts), skipped=len(scan.skipped_files), errors=len(scan.errors))
        if scan.errors:
            # An incomplete scan cannot prove deletions or a complete revision.
            details = "; ".join(f"{error.relative_path or error.path}: {error.message}" for error in scan.errors)
            raise StorageError(f"Project scan failed; the previous graph and cache were preserved. {details}")
        with (profile.span("compile.plan", artifacts=len(scan.artifacts), cache_enabled=cache_enabled) if profile else nullcontext()):
            if cache_enabled:
                dirty = cache.dirty_set(scan, entries=cache_entries)
            else:
                dirty = DirtySet(
                    changed_artifact_ids={artifact.id for artifact in scan.artifacts},
                    deleted_artifact_ids=set(),
                )
            recovered_artifact_ids = cache.recoverable_artifact_ids(scan, dirty.changed_artifact_ids, entries=cache_entries) if cache_enabled else set()
            dirty.changed_artifact_ids.difference_update(recovered_artifact_ids)
            dirty.deleted_artifact_ids.update(cache.deleted_project_artifact_ids(scan))
            remapping_ids = {artifact_id(scan.project.id, name) for name in ("foundry.toml", "remappings.txt")}
            if remapping_ids & (dirty.changed_artifact_ids | dirty.deleted_artifact_ids):
                # Solidity parsers use Foundry inputs even when their own source bytes are unchanged.
                affected = {artifact.id for artifact in scan.artifacts if artifact.language == "Solidity"}
                dirty.changed_artifact_ids.update(affected)
                recovered_artifact_ids.difference_update(affected)
            if self._template_membership_changed(scan, dirty):
                # Previously parsed sources keep their literal template evidence even when no target existed.
                affected = {
                    node.id for node in self.store.find_nodes_by_property(
                        "project_id", scan.project.id, type_="SourceArtifact", status="active", limit=None, clone=False,
                    )
                    if node.properties.get("parser_metadata", {}).get("has_template_references")
                } - dirty.deleted_artifact_ids
                dirty.changed_artifact_ids.update(affected)
                recovered_artifact_ids.difference_update(affected)
        run = self.deltas.new_run(project_id=scan.project.id)
        run.files_seen = len(scan.artifacts)
        run.files_changed = len(dirty.changed_artifact_ids)
        run.files_skipped = len(scan.artifacts) - run.files_changed
        run.files_deleted = len(dirty.deleted_artifact_ids)

        artifacts_by_id = {artifact.id: artifact for artifact in scan.artifacts}
        previous_symbol_state = capture_symbol_state(
            self.store,
            dirty.changed_artifact_ids | dirty.deleted_artifact_ids,
        )
        aggregate = ArtifactCompilationResult(artifact_id="*")
        if profile:
            profile.event(
                "compile.plan.result",
                category="counter",
                files_seen=run.files_seen,
                files_changed=run.files_changed,
                files_skipped=run.files_skipped,
                files_deleted=run.files_deleted,
            )

        with (profile.span("compile.transaction", changed=run.files_changed, deleted=run.files_deleted) if profile else nullcontext()), self.store.transaction():
            delta, revision, pending_disk_entries = self._apply_compile_transaction(scan, dirty, compiler, cache, cache_enabled, recovered_artifact_ids, artifacts_by_id, aggregate, run, profile)
        if cache_enabled and pending_disk_entries:
            cache.persist_disk_entries(pending_disk_entries)
        retention = None
        if revision is not None:
            with (
                profile.span(
                    "compile.retention",
                    project_id=scan.project.id,
                    commits=self.retention_commits,
                )
                if profile
                else nullcontext()
            ):
                retention = prune_project_data(
                    self.store,
                    project_id=scan.project.id,
                    retention_commits=self.retention_commits,
                )
        checkpoint = self._checkpoint_store_if_needed(profile)

        for node_id in aggregate.affected_node_ids:
            dirty.affected_node_ids.add(node_id)
        if profile:
            profile.event(
                "compile.result",
                category="counter",
                status=run.status,
                nodes_created=run.nodes_created,
                nodes_updated=run.nodes_updated,
                edges_created=run.edges_created,
                edges_updated=run.edges_updated,
                errors=len(run.errors),
                checkpointed=bool(checkpoint.get("checkpointed")) if checkpoint else False,
            )
        summary = build_compilation_summary(
            self.store,
            revision=revision,
            delta=delta,
            previous_symbol_state=previous_symbol_state,
            discovery=options.test_discovery,
        )
        return CompileProjectResult(
            scan=scan,
            dirty_set=dirty,
            run=run,
            delta=delta,
            revision=revision,
            summary=summary,
            retention=retention,
        )

    def _checkpoint_store_if_needed(self, profile: PerformanceLogger | None) -> dict[str, Any]:
        checkpoint = getattr(self.store, "checkpoint_if_needed", None)
        if checkpoint is None:
            return {}
        if profile:
            with profile.span("compile.checkpoint"):
                result = checkpoint()
            lexical_finalize_ms = float(result.get("lexical_finalize_ms", 0.0) or 0.0)
            if lexical_finalize_ms > 0:
                profile.event(
                    "compile.lexical_finalize",
                    category="span",
                    duration_ms=lexical_finalize_ms,
                    ok=True,
                )
            profile.event("compile.checkpoint.result", category="counter", **result)
            return dict(result)
        return dict(checkpoint())

    def _apply_compile_transaction(
        self,
        scan: ScanResult,
        dirty: DirtySet,
        compiler: ArtifactCompiler,
        cache: ArtifactCache,
        cache_enabled: bool,
        recovered_artifact_ids: set[str],
        artifacts_by_id: dict[str, SourceArtifact],
        aggregate: ArtifactCompilationResult,
        run: CompilationRun,
        profile: PerformanceLogger | None,
    ) -> tuple[GraphDelta, ProjectRevision | None, list[ArtifactCacheEntry]]:
        pending_disk_entries: list[ArtifactCacheEntry] = []
        for artifact_id in sorted(recovered_artifact_ids):
            artifact = artifacts_by_id[artifact_id]
            with (profile.span("compile.cache_recover", artifact_id=artifact.id, relative_path=artifact.relative_path) if profile else nullcontext()):
                pending_disk_entries.append(cache.upsert_entry(artifact, compiled_at=utcnow_iso(), persist_disk=False))
        if dirty.changed_artifact_ids:
            with (profile.span("compile.register_delta", changed=len(dirty.changed_artifact_ids)) if profile else nullcontext()):
                scan.registration = self.project_registry.register_scan_delta(scan, dirty.changed_artifact_ids)
        removed_code = False
        for artifact_id in sorted(dirty.deleted_artifact_ids):
            node = self.store.get_node(artifact_id, clone=False)
            if node is None:
                continue
            removed_code |= node.properties.get("artifact_type") == "code"
            with (profile.span("compile.delete_archive", artifact_id=artifact_id) if profile else nullcontext()):
                archive_result = archive_artifact_fragments(self.store, node)
                self._archive_deleted_artifact_node(node.id)
                file_archive_result = self._archive_file_nodes(run.project_id, artifact_id)
            aggregate.merge(archive_result, file_archive_result)
            aggregate.archived_nodes.add(artifact_id)
            aggregate.affected_node_ids.add(artifact_id)

        for artifact_id in sorted(dirty.changed_artifact_ids):
            artifact = artifacts_by_id[artifact_id]
            try:
                with (profile.span("compile.artifact", artifact_id=artifact.id, relative_path=artifact.relative_path, size_bytes=artifact.size_bytes, artifact_type=artifact.artifact_type, language=artifact.language or "") if profile else nullcontext()), self.store.transaction():
                    result = compiler.compile_artifact(self.store, artifact)
            except ArtifactContentChangedError:
                raise
            except Exception as exc:  # keep prior graph/cache intact for this artifact
                run.errors.append(f"{artifact.relative_path}: {exc}")
                if profile:
                    profile.event("compile.artifact.error", category="error", artifact_id=artifact.id, relative_path=artifact.relative_path, error=str(exc))
                continue
            for error in result.errors:
                run.errors.append(f"{artifact.relative_path}: {error}")
            if cache_enabled:
                with (profile.span("compile.cache_upsert", artifact_id=artifact.id, relative_path=artifact.relative_path) if profile else nullcontext()):
                    pending_disk_entries.append(cache.upsert_entry(artifact, compiled_at=utcnow_iso(), persist_disk=False))
            if artifact.artifact_type != "code" and stable_id("module", artifact.id) in result.archived_nodes:
                removed_code = True
            aggregate.merge(result)
            if profile:
                profile.event(
                    "compile.artifact.result",
                    category="counter",
                    artifact_id=artifact.id,
                    relative_path=artifact.relative_path,
                    added_nodes=len(result.added_nodes),
                    updated_nodes=len(result.updated_nodes),
                    added_edges=len(result.added_edges),
                    updated_edges=len(result.updated_edges),
                    archived_nodes=len(result.archived_nodes),
                    archived_edges=len(result.archived_edges),
                    errors=len(result.errors),
                )

        for artifact_id in sorted(dirty.deleted_artifact_ids):
            if artifact_id not in aggregate.archived_nodes:
                continue
            cache_entry = cache.get_entry(scan.project.id, artifact_id)
            if cache_entry is not None:
                cache.archive_entry(cache_entry.id)

        if compiler.options.ingest_documents and (dirty.changed_artifact_ids or removed_code):
            link_artifact_ids = None if removed_code else self._document_code_link_scope(dirty.changed_artifact_ids, artifacts_by_id, compiler)
            with (profile.span("compile.link_documents_to_code", changed=len(dirty.changed_artifact_ids), scoped=link_artifact_ids is not None) if profile else nullcontext()):
                link_result = link_document_fragments_to_code(
                    self.store,

                    project_id=scan.project.id,
                    artifact_ids=link_artifact_ids,
                )
            aggregate.merge(link_result)
            if profile:
                profile.event(
                    "compile.link_documents_to_code.result",
                    category="counter",
                    added_edges=len(link_result.added_edges),
                    updated_edges=len(link_result.updated_edges),
                    archived_edges=len(link_result.archived_edges),
                )

        if dirty.changed_artifact_ids or dirty.deleted_artifact_ids:
            with (profile.span("compile.refresh_application_surface", changed=len(dirty.changed_artifact_ids), deleted=len(dirty.deleted_artifact_ids)) if profile else nullcontext()):
                aggregate.merge(refresh_project_css_surface_findings(self.store, scan.project.id))

        if not run.errors and (dirty.changed_artifact_ids or dirty.deleted_artifact_ids):
            with (profile.span("compile.refresh_structural_links", changed=len(dirty.changed_artifact_ids)) if profile else nullcontext()):
                aggregate.merge(refresh_project_structural_links(self.store, scan.project.id, profile_logger=profile))
                aggregate.merge(refresh_static_links(self.store, scan.project.id))
            with (profile.span("compile.refresh_unused_symbol_findings", changed=len(dirty.changed_artifact_ids)) if profile else nullcontext()):
                aggregate.merge(self._refresh_unused_symbol_findings(scan))
            with (profile.span("compile.orphan_directories", artifacts=len(scan.artifacts)) if profile else nullcontext()):
                aggregate.merge(self._refresh_orphan_directory_findings(scan))

        run.completed_at = utcnow_iso()
        run.status = "failed" if run.errors else "completed"
        run.nodes_created = len(aggregate.added_nodes)
        run.nodes_updated = len(aggregate.updated_nodes)
        run.edges_created = len(aggregate.added_edges)
        run.edges_updated = len(aggregate.updated_edges)

        delta = GraphDelta(
            id=self._new_delta_id(),
            run_id=run.id,

            project_id=scan.project.id,
            artifact_id="*",
            added_nodes=sorted(aggregate.added_nodes),
            updated_nodes=sorted(aggregate.updated_nodes),
            archived_nodes=sorted(aggregate.archived_nodes),
            added_edges=sorted(aggregate.added_edges),
            updated_edges=sorted(aggregate.updated_edges),
            archived_edges=sorted(aggregate.archived_edges),
            affected_node_ids=sorted(aggregate.affected_node_ids),
            affected_community_ids=sorted(aggregate.affected_community_ids),
        )
        with (profile.span("compile.persist_delta", run_id=run.id, delta_id=delta.id) if profile else nullcontext()):
            self.deltas.persist_run(run)
            self.deltas.persist_delta(delta)
        revision = None
        if not run.errors:
            revision = self.revisions.record(
                project_id=scan.project.id,
                run_id=run.id,
                artifacts=scan.artifacts,
            )
        return delta, revision, pending_disk_entries

    def _archive_deleted_artifact_node(self, node_id: str) -> None:
        node = self.store.get_node(node_id, clone=False)
        if node is None or node.status == "archived":
            return
        properties = dict(node.properties)
        properties["status"] = "archived"
        properties["updated_at"] = utcnow_iso()
        self.store.update_node_fields(node.id, status="archived", properties=properties)

    def _archive_file_nodes(self, project_id_: str, artifact_id: str) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id=artifact_id)
        for node in self.store.find_nodes_by_property("artifact_id", artifact_id, type_="File", limit=1000, clone=False):
            if node.properties.get("project_id") != project_id_ or node.status == "archived":
                continue
            properties = dict(node.properties)
            properties["status"] = "archived"
            properties["updated_at"] = utcnow_iso()
            self.store.update_node_fields(node.id, status="archived", properties=properties)
            result.archived_nodes.add(node.id)
            result.affected_node_ids.add(node.id)
            for edge in self.store.incident_edges([node.id], limit=None, clone=False):
                if edge.properties.get("artifact_id") != artifact_id or edge.properties.get("status") == "archived":
                    continue
                edge_properties = dict(edge.properties)
                edge_properties["status"] = "archived"
                self.store.update_edge_fields(edge.id, properties=edge_properties)
                result.archived_edges.add(edge.id)
                result.affected_edge_ids.add(edge.id)
        return result

    def _refresh_orphan_directory_findings(self, scan: ScanResult) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id="*")
        code_artifacts = [
            artifact
            for artifact in scan.artifacts
            if artifact.artifact_type == "code" and artifact_context_scope(artifact) == "code"
        ]
        grouped = _candidate_orphan_directory_groups(code_artifacts)
        if not grouped:
            return self._archive_stale_orphan_directory_findings(scan.project.id, set())

        project_dependency_edges: list[MemoryEdge] = []
        for edge_type in sorted(ORPHAN_DIRECTORY_EDGE_TYPES):
            project_dependency_edges.extend(
                edge
                for edge in self.store.find_edges_by_property(
                    "project_id",
                    scan.project.id,
                    type_=edge_type,
                    limit=None,
                    clone=False,
                )
                if edge.properties.get("status") != "archived"
            )
        endpoint_ids = {
            node_id
            for edge in project_dependency_edges
            for node_id in (edge.from_id, edge.to_id)
        }
        path_by_node_id = {
            node.id: path
            for node in self.store.get_nodes(sorted(endpoint_ids), clone=False)
            if node.status == "active"
            if (path := _node_relative_path(node))
        }
        current_finding_ids: set[str] = set()
        for directory, artifacts in grouped:
            if self._directory_has_external_inbound(directory, path_by_node_id, project_dependency_edges):
                continue
            directory_node = self.store.get_node(stable_id("directory", scan.project.id, directory), clone=False)
            if directory_node is None or directory_node.status == "archived":
                continue
            finding = _orphan_directory_finding_node(scan.project.id, directory_node, artifacts)
            stored, created = self.store.upsert_node(finding, return_clone=False)
            current_finding_ids.add(stored.id)
            if created:
                result.added_nodes.add(stored.id)
            else:
                result.updated_nodes.add(stored.id)
            result.affected_node_ids.add(stored.id)
            edge = _orphan_directory_finding_edge(scan.project.id, directory_node, stored)
            stored_edge, edge_created = self.store.upsert_edge(edge, return_clone=False)
            if edge_created:
                result.added_edges.add(stored_edge.id)
            else:
                result.updated_edges.add(stored_edge.id)
            result.affected_edge_ids.add(stored_edge.id)
        archive_result = self._archive_stale_orphan_directory_findings(scan.project.id, current_finding_ids)
        result.merge(archive_result)
        return result

    def _directory_has_external_inbound(
        self,
        directory: str,
        path_by_node_id: dict[str, str],
        project_dependency_edges: list[MemoryEdge],
    ) -> bool:
        for edge in project_dependency_edges:
            target_path = path_by_node_id.get(edge.to_id)
            target_inside_directory = target_path is not None and _path_is_inside_directory(target_path, directory)
            if not target_inside_directory and not _edge_targets_directory_by_module(edge, directory):
                continue
            source_path = _normalize_relative_path(str(edge.properties.get("source_file") or "")) or path_by_node_id.get(edge.from_id)
            if not source_path or not _path_is_inside_directory(source_path, directory):
                return True
        return False

    def _archive_stale_orphan_directory_findings(self, project_id_: str, current_finding_ids: set[str]) -> ArtifactCompilationResult:
        result = ArtifactCompilationResult(artifact_id="*")
        for node in self.store.find_nodes_by_property("project_id", project_id_, type_="StaticAnalysisFinding", status="active", limit=None, clone=False):
            if node.properties.get("finding_type") != ORPHAN_DIRECTORY_FINDING_TYPE or node.id in current_finding_ids:
                continue
            properties = dict(node.properties)
            properties["status"] = "archived"
            properties["updated_at"] = utcnow_iso()
            self.store.update_node_fields(node.id, status="archived", properties=properties)
            result.archived_nodes.add(node.id)
            result.affected_node_ids.add(node.id)
            for edge in self.store.get_edges(to_id=node.id, type_="HAS_FINDING", limit=None, clone=False):
                if (edge.properties.get("extractor") != "orphan_directory_analyzer"
                        or edge.properties.get("status") == "archived"):
                    continue
                self.store.update_edge_fields(edge.id, properties={**edge.properties, "status": "archived"})
                result.archived_edges.add(edge.id)
                result.affected_edge_ids.add(edge.id)
        return result

    def _refresh_unused_symbol_findings(self, scan: ScanResult) -> ArtifactCompilationResult:
        """Reconcile locally eligible candidates against current project usage."""
        result = ArtifactCompilationResult(artifact_id="*")
        project_id_ = scan.project.id
        findings = [
            finding
            for finding in self.store.find_nodes_by_property(
                "project_id", project_id_, type_="StaticAnalysisFinding",
                status=("active", "archived"), limit=None, clone=False,
            )
            if finding.properties.get("finding_type") in UNUSED_SYMBOL_FINDING_TYPES
            and (finding.status == "active" or finding.properties.get("archived_reason") == PROJECT_USAGE_ARCHIVE_REASON)
        ]
        candidates = {str(finding.properties["symbol_id"]) for finding in findings if finding.properties.get("symbol_id")}
        if not candidates:
            return result

        symbol_ids = {
            node.id for node in self.store.get_nodes(sorted(candidates), clone=False)
            if node.status == "active" and node.type in UNUSED_SYMBOL_NODE_TYPES
        }
        used = {
            edge.to_id
            for edge in self.store.incident_edges(
                sorted(symbol_ids), edge_types=UNUSED_SYMBOL_USAGE_EDGE_TYPES, limit=None, clone=False,
            )
            if edge.properties.get("project_id") == project_id_
            and edge.properties.get("status") != "archived"
            and edge.to_id in symbol_ids and edge.from_id != edge.to_id
        }
        for finding in findings:
            symbol_id = finding.properties.get("symbol_id")
            if symbol_id not in symbol_ids:
                continue
            has_usage = symbol_id in used
            if has_usage != (finding.status == "archived"):
                self._set_finding_usage(finding, result, has_usage=has_usage)
        return result

    def _set_finding_usage(
        self, finding: MemoryNode, result: ArtifactCompilationResult, *, has_usage: bool,
    ) -> None:
        """Keep the finding and its evidence edges in the same lifecycle state."""
        status = "archived" if has_usage else "active"
        properties = {**finding.properties, "status": status, "updated_at": utcnow_iso()}
        if has_usage:
            properties["archived_reason"] = PROJECT_USAGE_ARCHIVE_REASON
            result.archived_nodes.add(finding.id)
            result.affected_node_ids.add(finding.id)
        else:
            properties.pop("archived_reason", None)
            result.record_node(finding.id, created=False)
        self.store.update_node_fields(finding.id, status=status, properties=properties)
        for edge in self.store.get_edges(to_id=finding.id, type_="HAS_FINDING", limit=None, clone=False):
            if edge.properties.get("status") == status:
                continue
            edge_properties = {**edge.properties, "status": status, "updated_at": properties["updated_at"]}
            self.store.update_edge_fields(edge.id, properties=edge_properties)
            if has_usage:
                result.archived_edges.add(edge.id)
                result.affected_edge_ids.add(edge.id)
            else:
                result.record_edge(edge.id, created=False)

    def _template_membership_changed(self, scan: ScanResult, dirty: DirtySet) -> bool:
        """Check whether registered sources could resolve to a different template file."""
        for artifact in scan.artifacts:
            if artifact.id in dirty.changed_artifact_ids and application_surface_kind(artifact.relative_path) == "template":
                node = self.store.get_node(artifact.id, clone=False)
                if node is None or node.status == "archived":
                    return True
        for node in self.store.get_nodes(sorted(dirty.deleted_artifact_ids), clone=False):
            if application_surface_kind(_node_relative_path(node) or "") == "template":
                return True
        return False

    def _document_code_link_scope(
        self,
        changed_artifact_ids: set[str],
        artifacts_by_id: dict[str, SourceArtifact],
        compiler: ArtifactCompiler,
    ) -> set[str] | None:
        changed_artifacts = [artifacts_by_id[artifact_id] for artifact_id in changed_artifact_ids if artifact_id in artifacts_by_id]
        if any(artifact.artifact_type == "code" for artifact in changed_artifacts):
            return None
        return {
            artifact.id
            for artifact in changed_artifacts
            if artifact.artifact_type in {"markdown", "text", "config", "data", "unknown"} and compiler.document_ingest_enabled(artifact)
        }

    def _project_scanner(
        self, options: CompilationOptions, *, max_file_size_bytes: int | None,
        include_patterns: list[str] | None, exclude_patterns: list[str] | None,
        config_path: str | Path | None,
    ) -> ProjectScanner:
        """Resolve per-call scan overrides against the configured project policy."""
        config = self.scan_config
        if max_file_size_bytes is None:
            max_file_size_bytes = config.max_file_size_bytes if config is not None else DEFAULT_MAX_FILE_SIZE_BYTES
        if config is not None:
            if include_patterns is None:
                include_patterns = config.include
            if exclude_patterns is None:
                exclude_patterns = config.exclude
                if options.ignore_defaults and not config.ignore_defaults:
                    defaults = set(default_config().scan.exclude)
                    exclude_patterns = [pattern for pattern in exclude_patterns if pattern not in defaults]
        return ProjectScanner(
            max_file_size_bytes=max_file_size_bytes, include_patterns=include_patterns,
            exclude_patterns=exclude_patterns, config_path=config_path, use_default_ignores=not options.ignore_defaults,
            test_discovery=options.test_discovery,
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
        _, status = self.scan_cache_status(
            path, max_file_size_bytes=max_file_size_bytes, include_patterns=include_patterns,
            exclude_patterns=exclude_patterns, config_path=config_path, cache_enabled=cache_enabled,
            parsing_options=parsing_options,
        )
        return status

    def scan_cache_status(
        self,
        path: str | Path,
        *,
        max_file_size_bytes: int | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        config_path: str | Path | None = None,
        cache_enabled: bool | None = None,
        parsing_options: CompilationOptions | None = None,
    ) -> tuple[ScanResult, dict[str, object]]:
        """Scan once using compile policy and return diagnostics for that scan."""
        options = self.default_options if parsing_options is None else normalize_compilation_options(parsing_options)
        scanner = self._project_scanner(
            options, max_file_size_bytes=max_file_size_bytes, include_patterns=include_patterns,
            exclude_patterns=exclude_patterns, config_path=config_path,
        )
        try:
            scan = scanner.scan(path)
        except OSError as exc:
            raise StorageError(f"Project scan failed for {path}: {exc}") from exc
        if scan.errors:
            details = "\n".join(f"{error.relative_path or error.path}: {error.message}" for error in scan.errors)
            raise StorageError(f"Project scan failed for {path}:\n{details}")
        cache_enabled = self.cache_enabled if cache_enabled is None else cache_enabled
        return scan, self.cache_status_for_scan(scan, cache_enabled=cache_enabled, options=options)

    def cache_status_for_scan(
        self, scan: ScanResult, *, cache_enabled: bool, options: CompilationOptions | None = None,
    ) -> dict[str, object]:
        """Read cache diagnostics from a scan already performed by the caller."""
        if not cache_enabled:
            return {
                "cache_enabled": False,
                "project": scan.project.to_dict(),
                "total_artifacts": len(scan.artifacts),
                "cached_artifacts": 0,
                "dirty_artifacts": len(scan.artifacts),
                "deleted_artifacts": 0,
                "entries": [],
                "dirty": [artifact.id for artifact in scan.artifacts],
                "deleted": [],
            }
        cache = self._cache_for_options(
            self.default_options if options is None else options, project_root=scan.project.root_path,
        )
        return cache.status_for_scan(scan)

    def clear_cache(self, path: str | Path) -> dict[str, object]:
        root = normalize_path(path)
        project = self.store.get_node_by_key("Project", root)
        project_id_ = project.id if project else project_id(root)
        cache = self._cache_for_options(self.default_options, project_root=root)
        cleared = cache.clear_project(project_id_)
        return {"project_id": project_id_, "cleared_entries": cleared, "cache_path": str(artifact_cache_path(root))}

    def list_deltas(self, *, limit: int = 20) -> list[GraphDelta]:
        return self.deltas.list_deltas(limit=limit)

    def show_delta(self, delta_id: str) -> GraphDelta | None:
        return self.deltas.get_delta(delta_id)

    def _new_delta_id(self) -> str:
        from ..domain.ids import new_id

        return new_id("delta")

    def _cache_for_options(
        self, options: CompilationOptions, *, project_root: str | Path | None = None,
        compiler: ArtifactCompiler | None = None,
    ) -> ArtifactCache:
        compiler = self._compiler_for_options(options) if compiler is None else compiler
        cache_path = artifact_cache_path(project_root) if project_root is not None else None
        return ArtifactCache(
            self.store,
            cache_path=cache_path,
            identity=compiler.identity,
        )

    def _compiler_for_options(
        self,
        options: CompilationOptions,
    ) -> ArtifactCompiler:
        if options == self.default_options:
            return self.compiler
        return ArtifactCompiler(options=options)


def _candidate_orphan_directory_groups(artifacts: list[SourceArtifact]) -> list[tuple[str, list[SourceArtifact]]]:
    by_directory: dict[str, dict[str, SourceArtifact]] = {}
    for artifact in artifacts:
        relative_path = _normalize_relative_path(artifact.relative_path)
        parent = Path(relative_path).parent.as_posix()
        if parent in {"", "."}:
            continue
        parts = [part for part in parent.split("/") if part]
        for index in range(1, len(parts) + 1):
            directory = "/".join(parts[:index])
            if _skip_orphan_directory_candidate(directory):
                continue
            by_directory.setdefault(directory, {})[artifact.id] = artifact

    raw_candidates = [
        (directory, sorted(items.values(), key=lambda item: item.relative_path))
        for directory, items in by_directory.items()
        if len(items) >= ORPHAN_DIRECTORY_MIN_FILES
    ]
    raw_candidates.sort(key=lambda item: (len(item[0].split("/")), item[0]))
    selected: list[tuple[str, list[SourceArtifact]]] = []
    selected_file_sets: list[set[str]] = []
    for directory, items in raw_candidates:
        file_set = {artifact.id for artifact in items}
        if any(existing.issuperset(file_set) for existing in selected_file_sets):
            continue
        selected.append((directory, items))
        selected_file_sets.append(file_set)
    return selected


def _skip_orphan_directory_candidate(directory: str) -> bool:
    parts = [part for part in directory.split("/") if part]
    if not parts:
        return True
    return len(parts) == 1 and parts[0] in ORPHAN_DIRECTORY_COMMON_ROOTS


def _orphan_directory_confidence(directory: str) -> float:
    names = {part.casefold() for part in directory.split("/") if part}
    return 0.78 if names & ORPHAN_DIRECTORY_NAME_HINTS else 0.62


def _orphan_directory_cleanup_priority(directory: str) -> str:
    names = {part.casefold() for part in directory.split("/") if part}
    return "medium" if names & ORPHAN_DIRECTORY_NAME_HINTS else "low"


def _orphan_directory_finding_node(project_id_: str, directory_node: MemoryNode, artifacts: list[SourceArtifact]) -> MemoryNode:
    directory = str(directory_node.properties.get("relative_path") or directory_node.label or "")
    files = sorted(_normalize_relative_path(artifact.relative_path) for artifact in artifacts)
    confidence = _orphan_directory_confidence(directory)
    cleanup_priority = _orphan_directory_cleanup_priority(directory)
    reason = (
        f"Directory {directory} contains {len(files)} code artifacts with no detected inbound imports, calls, "
        "references, document links, or tests from outside the directory. Review the directory as one cleanup candidate instead of inspecting each file separately."
    )
    properties = {
        "project_id": project_id_,
        "relative_path": directory,
        "context_scope": "code",
        "finding_type": ORPHAN_DIRECTORY_FINDING_TYPE,
        "category": "dead_code",
        "severity": "info",
        "reason": reason,
        "evidence_scope": "directory_reachability",
        "confidence": confidence,
        "cleanup_priority": cleanup_priority,
        "cleanup_rank": {"high": 3, "medium": 2, "low": 1}[cleanup_priority],
        "removal_safety": "validate",
        "removal_reason": "Directory has no detected inbound project usage, but directory removal needs entrypoint, plugin, script, and dynamic-reference validation.",
        "validation_reason": "Validate manual entrypoints, plugin loading, external users, scripts, and dynamic imports before deleting this directory.",
        "blocking_signals": ["directory_level", "dynamic_reference_unknown", "entrypoint_unknown"],
        "symbol_id": directory_node.id,
        "symbol_type": "Directory",
        "symbol_kind": "directory",
        "symbol_name": directory,
        "qualified_name": directory,
        "name": directory,
        "directory": directory,
        "file_count": len(files),
        "files": files,
        "mode": "compile",
        "is_technical": True,
        "is_semantic": False,
    }
    return MemoryNode(
        id=stable_id("static-analysis-finding", project_id_, ORPHAN_DIRECTORY_FINDING_TYPE, directory),
        type="StaticAnalysisFinding",
        label=f"{ORPHAN_DIRECTORY_FINDING_TYPE}: {directory}",
        text=reason,
        canonical_key=f"{project_id_}:finding:{ORPHAN_DIRECTORY_FINDING_TYPE}:{directory}",
        properties=properties,
        salience=0.16,
        confidence=confidence,
        status="active",
    )


def _orphan_directory_finding_edge(project_id_: str, directory_node: MemoryNode, finding: MemoryNode) -> MemoryEdge:
    directory = str(finding.properties.get("relative_path") or "")
    return MemoryEdge(
        id=stable_id("edge", directory_node.id, "HAS_FINDING", finding.id),
        from_id=directory_node.id,
        to_id=finding.id,
        type="HAS_FINDING",
        origin="deterministic",
        properties={
            "project_id": project_id_,
            "relative_path": directory,
            "finding_type": ORPHAN_DIRECTORY_FINDING_TYPE,
            "source_file": directory,
            "extractor": "orphan_directory_analyzer",
            "status": "active",
            "evidence": str(finding.properties.get("reason") or finding.label or ""),
            "mode": "compile",
            "is_technical": True,
            "is_semantic": False,
        },
    )


def _node_relative_path(node: MemoryNode) -> str | None:
    value = node.properties.get("relative_path") or node.properties.get("source_file")
    if value is None:
        return None
    normalized = _normalize_relative_path(str(value))
    return normalized or None


def _normalize_relative_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def _path_is_inside_directory(path: str, directory: str) -> bool:
    normalized_path = _normalize_relative_path(path)
    normalized_directory = _normalize_relative_path(directory)
    return normalized_path == normalized_directory or normalized_path.startswith(f"{normalized_directory}/")


def _edge_targets_directory_by_module(edge: MemoryEdge, directory: str) -> bool:
    module = str(edge.properties.get("module") or "").strip()
    if not module:
        return False
    module_path = module.replace(".", "/").strip("/")
    normalized_directory = _normalize_relative_path(directory)
    return module_path == normalized_directory or module_path.startswith(f"{normalized_directory}/")
