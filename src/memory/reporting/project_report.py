"""Project-level Markdown report orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..analysis.communities import CommunityDetector
from ..analysis.hubs import HubAnalyzer
from ..artifacts.fingerprint import normalize_path, project_id
from ..config import REQLConfig, default_config
from ..domain.constants import ACTIVE_STATUSES
from ..domain.exceptions import StorageError
from ..domain.models import MemoryNode
from ..domain.timeutils import utcnow_iso
from ..services.incremental_compilation import IncrementalCompilationService
from ..storage.graph_store import GraphStore
from .cache_report import CacheReportBuilder
from .delta_report import DeltaReportBuilder
from .graph_report import GraphReportBuilder


@dataclass(frozen=True, slots=True)
class ProjectReportFiles:
    graph_report: Path
    graph_deltas: Path
    cache_report: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "graph_report": str(self.graph_report),
            "graph_deltas": str(self.graph_deltas),
            "cache_report": str(self.cache_report),
        }


class ProjectReportGenerator:
    """Builds deterministic REQL project reports from graph data."""

    def __init__(
        self, store: GraphStore, *, compilation: IncrementalCompilationService | None = None,
        config: REQLConfig | None = None,
    ) -> None:
        self.store = store
        self.config = config if config is not None else default_config()
        self.compilation = compilation if compilation is not None else IncrementalCompilationService(store)

    def generate(self, path: str | Path, *, config_path: str | Path | None = None) -> dict[str, str]:
        context = self._context(path, config_path=config_path)
        policy = self.config.reporting.markdown
        return {
            "GRAPH_REPORT.md": GraphReportBuilder(policy=policy).render(context),
            "GRAPH_DELTAS.md": DeltaReportBuilder(policy=policy).render(context),
            "CACHE_REPORT.md": CacheReportBuilder(policy=policy).render(context),
        }

    def write_reports(
        self, path: str | Path, *, output_dir: str | Path, config_path: str | Path | None = None,
    ) -> ProjectReportFiles:
        reports = self.generate(path, config_path=config_path)
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        graph_report = out / "GRAPH_REPORT.md"
        graph_deltas = out / "GRAPH_DELTAS.md"
        cache_report = out / "CACHE_REPORT.md"
        graph_report.write_text(reports["GRAPH_REPORT.md"], encoding="utf-8")
        graph_deltas.write_text(reports["GRAPH_DELTAS.md"], encoding="utf-8")
        cache_report.write_text(reports["CACHE_REPORT.md"], encoding="utf-8")
        return ProjectReportFiles(graph_report=graph_report, graph_deltas=graph_deltas, cache_report=cache_report)

    def _context(self, path: str | Path, *, config_path: str | Path | None) -> dict[str, Any]:
        scan, scan_status = self.compilation.scan_cache_status(path, config_path=config_path)
        root = normalize_path(path)
        project = self.store.get_node_by_key("Project", root)
        project_id_ = project.id if project else project_id(root)
        artifacts = _nodes(self.store, "SourceArtifact", project_id_)
        cache_entries = _nodes(self.store, "ArtifactCacheEntry", project_id_)
        runs = _nodes(self.store, "CompilationRun", project_id_)
        deltas = _nodes(self.store, "GraphDelta", project_id_)
        code_types = {
            "Module",
            "Class",
            "Function",
            "Method",
            "Variable",
            "Import",
            "Dependency",
            "Endpoint",
            "Schema",
            "Config",
            "Test",
            "StaticAnalysisFinding",
            "CodeSymbol",
        }
        code_nodes = [node for type_ in sorted(code_types) for node in _nodes(self.store, type_, project_id_)]
        call_edge_count = self.store.count_edges(edge_types={"CALLS"}, project_id=project_id_)

        analysis = self.config.analysis
        analysis_warnings: list[str] = []
        community_by_node = None
        communities: list[MemoryNode] = []
        if analysis.enable_communities:
            try:
                community_result = CommunityDetector(self.store, policy=analysis.communities).detect(
                    project_id=project_id_, persist=False,
                )
                communities = community_result.community_nodes
                community_by_node = community_result.membership
            except StorageError as exc:
                analysis_warnings.append(f"Community analysis unavailable: {exc}")
                communities = _nodes(self.store, "Community", project_id_)

        hubs = []
        hub_warnings = []
        if analysis.enable_hubs:
            try:
                hub_report = HubAnalyzer(self.store, config=analysis).analyze(
                    project_id=project_id_, limit=None, community_by_node=community_by_node, persist=False,
                )
                hubs = hub_report.hubs
                hub_warnings = hub_report.warnings
            except StorageError as exc:
                analysis_warnings.append(f"Hub analysis unavailable: {exc}")

        archived_count = self.store.count_nodes(
            project_id=project_id_, statuses={"archived"}, include_global_project=True,
        )
        stale_count = self.store.count_nodes(
            project_id=project_id_, statuses=set(ACTIVE_STATUSES), include_global_project=False,
        )

        return {
            "generated_at": utcnow_iso(),
            "root_path": root,
            "project_id": project_id_,
            "project": project,
            "artifacts": artifacts,
            "cache_entries": cache_entries,
            "runs": sorted(runs, key=lambda n: str(n.properties.get("started_at") or n.created_at), reverse=True),
            "deltas": sorted(deltas, key=lambda n: str(n.properties.get("created_at") or n.created_at), reverse=True),
            "code_nodes": code_nodes,
            "call_edge_count": call_edge_count,
            "communities": communities,
            "hubs": hubs,
            "hub_warnings": hub_warnings,
            "archived_count": archived_count,
            "stale_count": stale_count,
            "scan_status": scan_status,
            "skipped_files": scan.skipped_files,
            "analysis_warnings": analysis_warnings,
        }


def _nodes(store: GraphStore, type_: str, project_id_: str) -> list[MemoryNode]:
    """Read all rows for this report so its summaries count the complete scope."""
    return store.find_nodes_by_property("project_id", project_id_, type_=type_, limit=None, clone=False)
