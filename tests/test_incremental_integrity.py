"""Regression coverage for complete scans and trustworthy compilation caches."""
from __future__ import annotations

from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from config import VERSION
from memory.artifacts.cache import artifact_cache_path
from memory.artifacts.compiler import ArtifactCompiler
from memory.artifacts.options import CompilationOptions
from memory.artifacts.fingerprint import tree_sitter_versions
from memory.code_analysis.parser_base import default_code_parser_registry
from memory.document_ingestion.base import default_parser_registry
from memory.domain.exceptions import StorageError
from memory.services.incremental_compilation import CompileProjectResult, IncrementalCompilationService


class IncrementalIntegrityTests(unittest.TestCase):
    """Exercise cache and scanner failure transitions through the public API."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Match scanner paths even when Windows temp uses a short-path alias.
        self.root = (Path(self.temp.name) / "project").resolve()
        self.root.mkdir()
        self.path = self.root / "rules.py"
        self.path.write_text("def invoice_total():\n    return 1\n", encoding="utf-8")
        self.graph = MemoryGraph.open(Path(self.temp.name) / "graph.reql")
        self.addCleanup(self.graph.close)

    def test_uncached_compile_archives_deleted_artifacts_and_symbols(self) -> None:
        """Disabling memoization must preserve deletion semantics and idempotence."""
        first = self.graph.compile_project(self.root, cache_enabled=False)
        self.assertFalse(first.run.errors)
        artifact = first.scan.artifacts[0]
        symbols = self.graph.store.find_nodes_by_property("artifact_id", artifact.id, type_="Function")
        self.assertTrue(symbols)
        self.path.unlink()
        deleted = self.graph.update_project(self.root, cache_enabled=False)
        self.assertFalse(deleted.run.errors)
        self.assertEqual(deleted.run.files_deleted, 1)
        for node_id in [artifact.id, *(node.id for node in symbols)]:
            self.assertEqual(self.graph.store.get_node(node_id).status, "archived")
        self.assertEqual(self.graph.compile_project(self.root, cache_enabled=False).run.files_deleted, 0)
        self.assertFalse(artifact_cache_path(self.root).exists())

    def test_scan_errors_preserve_graph_revision_and_disk_cache(self) -> None:
        """Unreadable files and directories are scan failures, not confirmed deletions."""
        self.graph.compile_project(self.root)
        before_nodes = {node.id: node.to_dict() for node in self.graph.store.all_nodes()}
        before_edges = {edge.id: edge.to_dict() for edge in self.graph.store.all_edges()}
        cache_path = artifact_cache_path(self.root)
        before_cache = cache_path.read_bytes()
        original_open, original_scandir = Path.open, os.scandir

        def denied_open(path, *args, **kwargs):
            if path == self.path:
                raise PermissionError("fixture read denied")
            return original_open(path, *args, **kwargs)

        def denied_scandir(path):
            if Path(path) == self.root:
                raise PermissionError("fixture listing denied")
            return original_scandir(path)

        for target, denied in (("pathlib.Path.open", denied_open),
                               ("memory.artifacts.scanner.os.scandir", denied_scandir)):
            for cache_enabled in (True, False):
                with self.subTest(target=target, cache_enabled=cache_enabled):
                    with patch(target, denied), self.assertRaisesRegex(StorageError, "scan.*failed|Scan.*failed"):
                        self.graph.compile_project(self.root, cache_enabled=cache_enabled)
                    self.assertEqual({node.id: node.to_dict() for node in self.graph.store.all_nodes()}, before_nodes)
                    self.assertEqual({edge.id: edge.to_dict() for edge in self.graph.store.all_edges()}, before_edges)
                    self.assertEqual(cache_path.read_bytes(), before_cache)
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 0)

    def test_missing_cache_cannot_recover_under_different_compile_options(self) -> None:
        """Matching bytes do not establish that the requested document policy ran."""
        document = self.root / "guide.md"
        document.write_text("# Invoice processing\n\nInvoice validation rules.\n", encoding="utf-8")
        disabled = CompilationOptions(ingest_documents=False)
        self.assertFalse(self.graph.compile_project(self.root, parsing_options=disabled).run.errors)
        self.graph.clear_cache(self.root)
        enabled = self.graph.compile_project(self.root, parsing_options=CompilationOptions(ingest_documents=True))
        self.assertFalse(enabled.run.errors)
        self.assertEqual(enabled.run.files_changed, 2)
        doc_artifact = next(item for item in enabled.scan.artifacts if item.relative_path == "guide.md")
        self.assertTrue(self.graph.store.find_nodes_by_property("artifact_id", doc_artifact.id, type_="SourceFragment"))

    def test_matching_missing_cache_recovers_but_new_release_recompiles(self) -> None:
        """Recovery requires the release that produced the completed source snapshot."""
        options = CompilationOptions.from_config(self.graph.config)
        self.assertEqual(self.graph.compile_project(self.root, parsing_options=options).run.files_changed, 1)
        self.assertEqual(self._incremental_service().compile_path(self.root).run.files_changed, 0)
        self.graph.clear_cache(self.root)
        self.assertEqual(self.graph.compile_project(self.root, parsing_options=options).run.files_changed, 0)
        self.graph.clear_cache(self.root)
        with patch("memory.artifacts.compile_identity.VERSION", VERSION + "-next"):
            upgraded = self._incremental_service()
            self.assertEqual(upgraded.compile_path(self.root).run.files_changed, 1)
            self.assertEqual(upgraded.compile_path(self.root).run.files_changed, 0)
            upgraded.clear_cache(self.root)
            self.assertEqual(upgraded.compile_path(self.root).run.files_changed, 0)
            self.assertEqual(upgraded.cache_status(self.root)["dirty_artifacts"], 0)

    def test_changing_options_uses_one_compilation_fingerprint(self) -> None:
        """The compiler and cache agree when one service compiles with different options."""
        self.graph.compile_project(self.root)
        service = self._incremental_service()
        options = CompilationOptions(ingest_documents=False)
        run = service.compile_path(self.root, parsing_options=options)
        self.assertFalse(run.run.errors)
        self.assertEqual(run.run.files_changed, 1)
        artifact = self.graph.store.get_node(run.scan.artifacts[0].id)
        self.assertEqual(artifact.properties["compilation_fingerprint"],
                         service._cache_for_options(options).identity.current(run.scan.artifacts[0]))
        self.assertEqual(service.compile_path(self.root, parsing_options=options).run.files_changed, 0)
        self.assertEqual(service.cache_status(self.root, parsing_options=options)["dirty_artifacts"], 0)

    def test_builtin_parsers_use_the_reql_version(self) -> None:
        """Persist REQL's version on the artifacts parsed by built-in parsers."""
        (self.root / "guide.md").write_text("# Guide\n\nUsing the invoice rules.\n", encoding="utf-8")
        (self.root / "guide.txt").write_text("Using the invoice rules.\n", encoding="utf-8")
        result = self.graph.compile_project(self.root, parsing_options=CompilationOptions())
        self.assertFalse(result.run.errors)
        self.assertEqual(result.run.files_changed, 3)
        self.assertEqual({parser.parser_version for parser in default_parser_registry().parsers}, {VERSION})
        self.assertEqual(
            {self.graph.store.get_node(artifact.id).properties["parser_version"] for artifact in result.scan.artifacts},
            {VERSION},
        )

    def test_reql_upgrade_recompiles_code_and_documents(self) -> None:
        """A release change invalidates cached facts for every built-in parser."""
        self._write_mixed_project()
        initial = self._incremental_service().compile_path(self.root)
        self.assertFalse(initial.run.errors)
        self.assertEqual(initial.run.files_changed, 5)
        with patch("memory.artifacts.compile_identity.VERSION", VERSION + "-next"):
            service = self._incremental_service()
            self._assert_recompiled(service, initial, {artifact.relative_path for artifact in initial.scan.artifacts})
            service.clear_cache(self.root)
            self.assertEqual(service.compile_path(self.root).run.files_changed, 0)

    def test_grammar_package_upgrade_invalidates_only_its_consumers(self) -> None:
        """The TypeScript grammar wheel provides both TypeScript and TSX."""
        self._write_mixed_project()
        initial = self._incremental_service().compile_path(self.root)
        self.assertFalse(initial.run.errors)
        versions = {**tree_sitter_versions(), "tree-sitter-typescript": "new-version"}
        with patch("memory.artifacts.compile_identity.tree_sitter_versions", return_value=versions):
            self._assert_recompiled(self._incremental_service(), initial, {"widget.ts", "widget.tsx"})

    def test_recompile_replaces_obsolete_cache_fields(self) -> None:
        """Rewrite old disk and graph cache snapshots without separate versions."""
        initial = self.graph.compile_project(self.root)
        self.assertFalse(initial.run.errors)
        artifact = initial.scan.artifacts[0]
        path = artifact_cache_path(self.root)
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = payload["projects"][artifact.project_id]["entries"][artifact.id]
        old_fields = {"parser_version": "project-scan-v10", "chunking_version": "uncompiled-v1"}
        entry.update(old_fields)
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.graph.store.update_node_fields(entry["id"], properties=entry)
        node = self.graph.store.get_node(artifact.id)
        self.graph.store.update_node_fields(
            artifact.id, properties={**node.properties, "compilation_fingerprint": "old-compiler-contract"},
        )

        reports = self.graph.project_report(self.root, output_dir=Path(self.temp.name) / "reports")
        for report in (reports.graph_report, reports.cache_report):
            text = report.read_text(encoding="utf-8")
            self.assertIn("Dirty artifacts: 1", text)

        refreshed = self.graph.compile_project(self.root)
        self.assertFalse(refreshed.run.errors)
        self.assertEqual(refreshed.run.files_changed, 1)
        disk = json.loads(path.read_text(encoding="utf-8"))
        disk_entry = disk["projects"][artifact.project_id]["entries"][artifact.id]
        graph_entry = self.graph.store.get_node(entry["id"])
        self.assertEqual(graph_entry.properties, disk_entry)
        self.assertFalse(old_fields.keys() & disk_entry.keys())
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 0)

    def test_injected_registry_preserves_grammar_scoped_code_fingerprints(self) -> None:
        """A TypeScript update refreshes its consumers and the custom document contract."""
        self._write_mixed_project()
        options = CompilationOptions.from_config(self.graph.config)

        def service() -> IncrementalCompilationService:
            compiler = ArtifactCompiler(
                options=options, enable_pdf=False, code_parser_registry=default_code_parser_registry(),
            )
            return IncrementalCompilationService(self.graph.store, compiler=compiler)

        initial = service().compile_path(self.root)
        self.assertFalse(initial.run.errors)
        versions = {**tree_sitter_versions(), "tree-sitter-typescript": "new-version"}
        with patch("memory.artifacts.compile_identity.tree_sitter_versions", return_value=versions):
            self._assert_recompiled(service(), initial, {"widget.ts", "widget.tsx", "guide.md"})

    def _write_mixed_project(self) -> None:
        code = {"widget.js": "function widget() { return 1; }\n",
                "widget.ts": "function widget(): number { return 1; }\n",
                "widget.tsx": "function Widget() { return <div />; }\n"}
        for path, content in {**code, "guide.md": "# Widget\n\nUsage guide.\n"}.items():
            (self.root / path).write_text(content, encoding="utf-8")

    def _incremental_service(self) -> IncrementalCompilationService:
        return IncrementalCompilationService(
            self.graph.store, compile_options=CompilationOptions.from_config(self.graph.config),
        )

    def _assert_recompiled(
        self, service: IncrementalCompilationService, initial: CompileProjectResult, expected: set[str],
    ) -> None:
        fingerprints = {
            artifact.id: self.graph.store.get_node(artifact.id).properties["compilation_fingerprint"]
            for artifact in initial.scan.artifacts
        }
        result = service.compile_path(self.root)
        self.assertFalse(result.run.errors)
        self.assertEqual(result.run.files_changed, len(expected))
        changed = {
            artifact.relative_path for artifact in result.scan.artifacts
            if self.graph.store.get_node(artifact.id).properties["compilation_fingerprint"] != fingerprints[artifact.id]
        }
        self.assertEqual(changed, expected)
        self.assertEqual(service.cache_status(self.root)["dirty_artifacts"], 0)
        self.assertEqual(service.compile_path(self.root).run.files_changed, 0)

    def test_uncached_option_changes_cannot_reuse_an_older_cache_entry(self) -> None:
        """An existing cache entry must also describe the graph's last actual compile."""
        self.graph.compile_project(self.root)
        alternate = CompilationOptions(ingest_documents=False)
        self.graph.compile_project(self.root, cache_enabled=False, parsing_options=alternate)
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 1)
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 0)

    def test_failed_compile_cannot_recover_from_scan_time_content_metadata(self) -> None:
        """Only the digest of successfully compiled bytes establishes cache provenance."""
        self.graph.compile_project(self.root)
        self.graph.clear_cache(self.root)
        self.path.write_text("def invoice_total():\n    return 2\n", encoding="utf-8")
        with patch.object(ArtifactCompiler, "_read_verified_content", side_effect=PermissionError("read denied")):
            self.assertTrue(self.graph.compile_project(self.root).run.errors)
        retried = self.graph.compile_project(self.root)
        self.assertFalse(retried.run.errors)
        self.assertEqual(retried.run.files_changed, 1)
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 0)

if __name__ == "__main__":
    unittest.main()
