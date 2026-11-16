"""Regression coverage for complete scans and trustworthy compilation caches."""
from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from memory.artifacts.cache import artifact_cache_path
from memory.artifacts.compiler import ArtifactCompiler
from memory.artifacts.options import CompilationOptions
from memory.artifacts.fingerprint import DEFAULT_PARSER_VERSION, tree_sitter_versions
from memory.code_analysis.languages.javascript import JavaScriptTreeSitterExtractor
from memory.code_analysis.parser_base import default_code_parser_registry
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

    def test_matching_missing_cache_recovers_but_new_parser_version_recompiles(self) -> None:
        """Recovery requires the same compiler version as the completed source snapshot."""
        options = CompilationOptions.from_config(self.graph.config)
        self.assertEqual(self.graph.compile_project(self.root, parsing_options=options).run.files_changed, 1)
        matching = IncrementalCompilationService(
            self.graph.store, parser_version=DEFAULT_PARSER_VERSION, compile_options=options,
        )
        self.assertEqual(matching.compile_path(self.root).run.files_changed, 0)
        self.graph.clear_cache(self.root)
        self.assertEqual(self.graph.compile_project(self.root, parsing_options=options).run.files_changed, 0)
        self.graph.clear_cache(self.root)
        upgraded = IncrementalCompilationService(
            self.graph.store, parser_version=DEFAULT_PARSER_VERSION + "-next", compile_options=options,
        )
        self.assertEqual(upgraded.compile_path(self.root).run.files_changed, 1)
        self.assertEqual(upgraded.compile_path(self.root).run.files_changed, 0)
        upgraded.clear_cache(self.root)
        self.assertEqual(upgraded.compile_path(self.root).run.files_changed, 0)
        self.assertEqual(upgraded.cache_status(self.root)["dirty_artifacts"], 0)

    def test_changing_chunk_contract_and_options_uses_one_compilation_fingerprint(self) -> None:
        """The compiler, cache and option-specific compiler agree after each upgrade."""
        self.graph.compile_project(self.root)
        service = IncrementalCompilationService(self.graph.store, chunking_version="test-chunks-v2")
        options = CompilationOptions(ingest_documents=False)
        run = service.compile_path(self.root, parsing_options=options)
        self.assertFalse(run.run.errors)
        self.assertEqual(run.run.files_changed, 1)
        artifact = self.graph.store.get_node(run.scan.artifacts[0].id)
        self.assertEqual(artifact.properties["compilation_fingerprint"],
                         service._cache_for_options(options).identity.current(run.scan.artifacts[0]))
        self.assertEqual(service.compile_path(self.root, parsing_options=options).run.files_changed, 0)
        self.assertEqual(service.cache_status(self.root, parsing_options=options)["dirty_artifacts"], 0)

    def test_extractor_upgrade_reuses_unaffected_artifacts(self) -> None:
        """A shared walker revision invalidates JavaScript, TypeScript and TSX only."""
        expected = self._write_mixed_project()
        initial = self._incremental_service().compile_path(self.root)
        self.assertFalse(initial.run.errors)
        self.assertEqual(initial.run.files_changed, 5)
        revision = JavaScriptTreeSitterExtractor.facts_revision + 1
        with patch.object(JavaScriptTreeSitterExtractor, "facts_revision", revision):
            service = self._incremental_service()
            self._assert_recompiled(service, initial, expected)
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

    def test_explicit_project_version_still_invalidates_all_artifacts(self) -> None:
        """Callers can explicitly advance the overall compilation contract."""
        self._write_mixed_project()
        initial = self._incremental_service().compile_path(self.root)
        updated = self._incremental_service(parser_version=DEFAULT_PARSER_VERSION + "-project-upgrade")
        self._assert_recompiled(updated, initial, {artifact.relative_path for artifact in initial.scan.artifacts})

    def test_injected_registry_honors_the_registered_builtin_extractor(self) -> None:
        """Document options and explicit registry injection preserve code version checks."""
        expected = self._write_mixed_project()
        options = CompilationOptions.from_config(self.graph.config)

        def service() -> IncrementalCompilationService:
            compiler = ArtifactCompiler(
                options=options, enable_pdf=False, code_parser_registry=default_code_parser_registry(),
            )
            return IncrementalCompilationService(self.graph.store, compiler=compiler)

        initial = service().compile_path(self.root)
        revision = JavaScriptTreeSitterExtractor.facts_revision + 1
        with patch.object(JavaScriptTreeSitterExtractor, "facts_revision", revision):
            self._assert_recompiled(service(), initial, expected)

    def _write_mixed_project(self) -> set[str]:
        code = {"widget.js": "function widget() { return 1; }\n",
                "widget.ts": "function widget(): number { return 1; }\n",
                "widget.tsx": "function Widget() { return <div />; }\n"}
        for path, content in {**code, "guide.md": "# Widget\n\nUsage guide.\n"}.items():
            (self.root / path).write_text(content, encoding="utf-8")
        return set(code)

    def _incremental_service(self, *, parser_version: str | None = None) -> IncrementalCompilationService:
        return IncrementalCompilationService(
            self.graph.store, parser_version=parser_version, compile_options=CompilationOptions.from_config(self.graph.config),
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
