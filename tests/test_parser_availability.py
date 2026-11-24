"""Fallback grammar loading must never initiate implicit network work."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import warnings

from memory.artifacts.models import SourceArtifact
from memory.code_analysis import TreeSitterCodeParser
from memory.code_analysis.catalog import _load_language_pack_language, load_tree_sitter_language
from memory.code_analysis.base import _parser_for
from reql import MemoryGraph


class ParserAvailabilityTests(unittest.TestCase):
    """Fake only the external grammar inventory and loader, keeping parser selection real."""

    def test_unavailable_grammars_return_skipped_results(self) -> None:
        """Report unavailable grammars and keep fallback lookup local."""
        provider = SimpleNamespace(available_languages=Mock(return_value=[]), get_language=Mock())
        unsupported = "Unsupported Tree-sitter language: sql"
        cases = (
            ("schema.sql", "sql", "select * from users;\n", unsupported,
             patch("memory.code_analysis.base._parser_for", side_effect=ValueError(unsupported))),
            ("Dockerfile", "Dockerfile", "FROM scratch\n", "not available locally",
             patch("memory.code_analysis.catalog.import_module", return_value=provider)),
        )
        for path, language, source, warning, loader in cases:
            with self.subTest(language=language), loader:
                artifact = SourceArtifact(
                    id=f"artifact:{path}", project_id="project:local", uri=f"file:///{path}",
                    path=path, relative_path=path, artifact_type="code", language=language,
                    size_bytes=len(source), sha256="", mtime=0.0,
                )
                result = TreeSitterCodeParser().parse_artifact(artifact, source)
                self.assertEqual(result.module.metadata["parser_status"], "skipped")
                self.assertIn(warning, result.module.metadata["parser_warning"])
                self.assertFalse(result.symbols)
                self.assertFalse(result.errors)
                if language == "Dockerfile":
                    self.assertIn("download", result.module.metadata["parser_warning"])
        provider.get_language.assert_not_called()

    def test_cached_alias_loads_locally_and_missing_aliases_are_not_probed(self) -> None:
        """A cached alternate grammar name remains usable without trying missing names."""
        grammar = object()
        provider = SimpleNamespace(available_languages=Mock(return_value=["docker"]),
                                   get_language=Mock(return_value=grammar))
        with patch("memory.code_analysis.catalog.import_module", return_value=provider):
            self.assertIs(_load_language_pack_language("dockerfile"), grammar)
        provider.get_language.assert_called_once_with("docker")

    def test_explicitly_prepared_grammar_recompiles_previously_skipped_source(self) -> None:
        """A successful empty parse must not block recovery after grammar setup."""
        from tree_sitter import Language
        import tree_sitter_python

        grammar = Language(tree_sitter_python.language())
        provider = SimpleNamespace(available_languages=Mock(side_effect=[[], ["python"]]),
                                   get_language=Mock(return_value=grammar))

        def import_provider(name: str):
            """Model an absent direct wheel and a separately prepared local fallback."""
            if name == "tree_sitter_python":
                raise ModuleNotFoundError(name, name=name)
            self.assertEqual(name, "tree_sitter_language_pack")
            return provider

        _parser_for.cache_clear()
        self.addCleanup(_parser_for.cache_clear)
        with TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            (project / "invoice.py").write_text("def invoice_total(amount):\n    return amount + 1\n", encoding="utf-8")
            graph = MemoryGraph.open(Path(temporary) / "memory.reql")
            try:
                with patch("memory.code_analysis.catalog.import_module", side_effect=import_provider):
                    first = graph.compile_project(project)
                    self.assertFalse(first.run.errors)
                    self.assertFalse(graph.store.find_nodes_by_types(["Function"]))
                    recovered = graph.compile_project(project)
                    self.assertFalse(recovered.run.errors)
                    self.assertEqual(recovered.run.files_changed, 1)
                    self.assertEqual([n.properties["name"] for n in graph.store.find_nodes_by_types(["Function"])], ["invoice_total"])
                    self.assertEqual(graph.compile_project(project).run.files_changed, 0)
                provider.get_language.assert_called_once_with("python")
            finally:
                graph.close()

    def test_missing_transitive_dependency_propagates_from_the_direct_grammar(self) -> None:
        failure = ModuleNotFoundError("grammar dependency is broken", name="missing_dependency")
        _parser_for.cache_clear()
        self.addCleanup(_parser_for.cache_clear)
        with patch("memory.code_analysis.catalog.import_module", side_effect=failure) as provider:
            with self.assertRaisesRegex(ModuleNotFoundError, "grammar dependency is broken"):
                _parser_for("python")
        provider.assert_called_once_with("tree_sitter_python")


    def test_installed_solidity_grammar_parses_without_deprecation_or_fallback(self) -> None:
        _parser_for.cache_clear()
        self.addCleanup(_parser_for.cache_clear)
        with warnings.catch_warnings(), patch("memory.code_analysis.catalog._load_language_pack_language") as fallback:
            warnings.simplefilter("error", DeprecationWarning)
            root = _parser_for("solidity").parse(b"contract Demo {}").root_node
        fallback.assert_not_called()
        self.assertFalse(root.has_error)
        self.assertEqual(root.named_children[0].type, "contract_declaration")

    def test_invalid_legacy_solidity_pointers_are_rejected_before_capsule_creation(self) -> None:
        import tree_sitter

        for address in (0, -1, 1, 1 << 128):
            with self.subTest(address=address), patch(
                "memory.code_analysis.catalog._load_direct_language", return_value=address,
            ):
                with self.assertRaisesRegex(ValueError, "Invalid Tree-sitter Solidity grammar pointer"):
                    load_tree_sitter_language(tree_sitter, "solidity")


if __name__ == "__main__":
    unittest.main()
