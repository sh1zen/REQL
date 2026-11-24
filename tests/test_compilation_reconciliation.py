"""Regression coverage for parser evidence and incremental graph reconciliation."""
from contextlib import closing
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memory.artifacts.mime import classify_path
from memory.artifacts.scanner import ProjectScanner
from memory.code_analysis.catalog import load_tree_sitter_language, tree_sitter_language_key
from memory.config import default_config, merge_overrides
from memory.document_ingestion.formats.pdf import PDFParser
from memory.document_ingestion.formats.text import PlainTextParser
from memory.domain.models import MemoryEdge, MemoryNode
from reql import MemoryGraph


class CompilationReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.storage = self.root / ".reql" / "shared.reql"

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path

    def test_css_findings_are_reconciled_only_in_the_selected_project(self) -> None:
        self.write("one/style.css", ".first { color: red; }")
        self.write("two/style.css", ".second { color: blue; }")
        with closing(MemoryGraph.open(self.storage)) as graph:
            first = graph.compile_project(self.root / "one")
            self.assertFalse(first.run.errors)
            self.assertFalse(graph.compile_project(self.root / "two").run.errors)
            evidence = graph.store.find_edges_by_property(
                "project_id", first.run.project_id, type_="HAS_FINDING", limit=None,
            )
            self.assertEqual(len(evidence), 2)
            self.assertTrue(all(edge.properties["status"] == "active" for edge in evidence))
            self.write("one/index.html", "<i class=first></i>")
            update = graph.update_project(self.root / "one")
            self.assertFalse(update.run.errors)
            self.assertTrue({edge.id for edge in evidence}.issubset(update.delta.archived_edges))

    def test_scoped_document_refresh_preserves_other_links_and_archives_deleted_targets(self) -> None:
        target = self.write("widget.py", "def build_widget(): pass\n")
        self.write("one.md", "# One\n\nCall widget.build_widget before running.\n")
        self.write("two.md", "# Two\n\nUse widget.build_widget to initialize the widget.\n")
        config = merge_overrides(default_config(), {"compile.documents.markdown": True})
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            def direct_links():
                return {
                    edge.properties["relative_path"]: edge.id
                    for edge in graph.store.get_edges(type_="REFERENCES", limit=None)
                    if edge.properties.get("extractor") == "document_code_linker"
                    and edge.properties["status"] == "active"
                    and edge.properties["target_name"] == "build_widget"
                }

            self.assertFalse(graph.compile_project(self.root).run.errors)
            original = direct_links()
            self.assertEqual(set(original), {"one.md", "two.md"})
            self.write("one.md", "# One\n\nThis page no longer references the builder.\n")
            self.assertFalse(graph.update_project(self.root).run.errors)
            self.assertEqual(direct_links(), {"two.md": original["two.md"]})
            target.unlink()
            self.assertFalse(graph.update_project(self.root).run.errors)
            self.assertEqual(direct_links(), {})
            self.assertFalse(any(
                edge.properties.get("extractor") == "document_processor"
                and edge.properties.get("target_name") == "build_widget"
                and edge.properties.get("status") == "active"
                for edge in graph.store.get_edges(type_="REFERENCES", limit=None)
            ))

    def test_document_linking_reaches_fragments_after_one_thousand_links(self) -> None:
        self.write("widget.py", "def build_widget(): pass\n")
        self.write("guide.md", "# Guide\n\n" + "\n\n".join(
            f"Step {number} calls widget.build_widget." for number in range(1002)
        ))
        config = merge_overrides(default_config(), {"compile.documents.markdown": True})
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            edges = graph.store.get_edges(type_="REFERENCES", limit=None)
            self.assertEqual(len({
                edge.from_id for edge in edges
                if edge.properties.get("extractor") == "document_code_linker"
                and edge.properties.get("target_name") == "build_widget"
            }), 1002)

    def test_document_links_use_explicit_names_and_reconcile_ambiguous_aliases(self) -> None:
        """Resolve marked short names and qualified references without linking incidental prose."""
        self.write("first.py", "def api(): pass\ndef frobnicate(): pass\n")
        self.write(
            "guide.md",
            "# About the api\n\nUse frobnicate when needed.\n\n"
            "## `api`\n\nCall first.api() directly.\n\nTry frobnicate() here.\n",
        )
        config = merge_overrides(default_config(), {"compile.documents.markdown": True})
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            def active_mentions() -> set[tuple[int, str]]:
                return {
                    (edge.properties["line_start"], edge.properties["evidence"])
                    for edge in graph.store.get_edges(type_="REFERENCES", limit=None)
                    if (edge.properties.get("extractor") == "document_code_linker"
                        and edge.properties["status"] == "active")
                }

            self.assertFalse(graph.compile_project(self.root).run.errors)
            self.assertEqual(active_mentions(), {(5, "api"), (7, "first.api"), (9, "frobnicate")})
            self.write("second.py", "def api(): pass\n")
            self.assertFalse(graph.update_project(self.root).run.errors)
            self.assertEqual(active_mentions(), {(7, "first.api"), (9, "frobnicate")})

    def test_deleted_fragment_archives_all_incident_document_edges(self) -> None:
        self.write("guide.md", "# Guide\n\nParagraph to be deleted.\n")
        config = merge_overrides(default_config(), {"compile.documents.markdown": True})
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            fragment = next(
                node for node in graph.store.find_nodes_by_types(("SourceFragment",))
                if node.properties.get("fragment_type") == "paragraph"
            )
            nodes = [MemoryNode(id=f"concept-{index}", type="Concept", label=f"term {index}") for index in range(103)]
            graph.store.batch_upsert_nodes(nodes)
            edges = [
                MemoryEdge(id=f"fragment-mention-{index}", from_id=fragment.id, to_id=node.id,
                           type="MENTIONS", properties={"project_id": "example"})
                for index, node in enumerate(nodes)
            ]
            graph.store.batch_upsert_edges(edges)
            self.assertEqual(len(graph.store.find_edges_by_property("project_id", "example", limit=None)), 103)
            self.assertEqual(len(graph.store.find_edges_by_property("project_id", "example", limit=1)), 1)
            self.write("guide.md", "# Guide\n")
            self.assertFalse(graph.update_project(self.root).run.errors)
            archived = graph.store.find_edges_by_property("project_id", "example", limit=None)
            self.assertEqual({edge.id: edge.properties["status"] for edge in archived},
                             {edge.id: "archived" for edge in edges})

    def test_literal_surfaces_handle_case_compound_selectors_and_real_html_attributes(self) -> None:
        self.write("app.css", """
.Unquoted, .First, .Second, .Added, .Exact { color: red; }
[data-icon=".NotASelector"] { color: blue; }
.Commented, .DataAttribute, .OnlyAString { color: green; }
.quote { content: "/* a literal */"; content: ""; }
""")
        self.write("index.html", """
<div class=Unquoted data-class="DataAttribute"></div>
<div class="exact"></div>
<!-- <i class="Commented"></i> -->
""")
        self.write("app.js", """
document.querySelector('.First .Second');
element.classList.add('First', 'Added');
const value = '.OnlyAString';
""")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            findings = {
                (node.properties["finding_type"], node.properties["symbol_name"])
                for node in graph.store.find_nodes_by_types(("StaticAnalysisFinding",))
                if node.status == "active"
            }
            self.assertEqual({name for kind, name in findings if kind == "unused_css_class"},
                             {".Exact", ".Commented", ".DataAttribute", ".OnlyAString", ".quote"})
            self.assertIn(("possibly_overridden_css_declaration", ".quote content"), findings)

    def test_php_template_evidence_is_bound_to_the_actual_call_and_owner(self) -> None:
        self.write("controllers/Shop.php5", """<?php
function show($view) { view('first'); view('second'); view($view, ['decoy' => 1]); }
require __DIR__ . '/../views/first.php';
require "$view.php";
""")
        for name in ("first", "second", "decoy"):
            self.write(f"views/{name}.php", "<p>Template</p>")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            references = graph.store.get_edges(type_="REFERENCES", limit=None)
            render = [edge for edge in references if edge.properties.get("relation") == "template_render"]
            self.assertEqual({edge.properties["target_name"] for edge in render}, {"first", "second"})
            self.assertTrue(all(graph.store.get_node(edge.from_id).type == "Function" for edge in render))
            self.assertEqual([edge.properties["resolved_relative_path"] for edge in references
                              if edge.properties.get("relation") == "template_include"], ["views/first.php"])

    def test_template_additions_and_deletions_relink_unchanged_renderers(self) -> None:
        self.write("controllers/Shop.php", "<?php function show() { view('card'); }")
        with closing(MemoryGraph.open(self.storage)) as graph:
            def rendered_paths():
                return {edge.properties["resolved_relative_path"] for edge in graph.store.get_edges(type_="REFERENCES", limit=None)
                        if edge.properties.get("relation") == "template_render" and edge.properties["status"] == "active"}

            self.assertFalse(graph.compile_project(self.root).run.errors)
            self.assertEqual(rendered_paths(), set())
            self.write("templates/card.php", "<p>Fallback</p>")
            self.assertFalse(graph.update_project(self.root).run.errors)
            self.assertEqual(rendered_paths(), {"templates/card.php"})
            preferred = self.write("views/card.php", "<p>Preferred</p>")
            self.assertFalse(graph.update_project(self.root).run.errors)
            self.assertEqual(rendered_paths(), {"views/card.php"})
            preferred.unlink()
            self.assertFalse(graph.update_project(self.root).run.errors)
            self.assertEqual(rendered_paths(), {"templates/card.php"})

    def test_python_and_ecmascript_imports_follow_their_own_formats(self) -> None:
        self.write("pkg/__init__.py", "")
        self.write("pkg/local.pyi", "def build() -> None: ...\n")
        self.write("pkg/consumer.py", "from . import local\nfrom ... import outsider\n")
        self.write("outsider.py", "def run(): pass\n")
        self.write("entry.ts", "import './setup.mjs';\nexport { action } from './setup.mjs';\n")
        self.write("setup.mts", "export function action() { return 1; }\n")
        self.write("sample.go", 'package main\nimport "fmt"\nfunc Type() { fmt.Println("value") }\n')
        self.write("fmt.py", "def run(): pass\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            paths = {
                (node.properties["relative_path"], node.properties["name"]): node.properties.get("resolved_relative_path")
                for node in graph.store.find_nodes_by_types(("Import",))
            }
            self.assertEqual(paths["pkg/consumer.py", "local"], "pkg/local.pyi")
            self.assertIsNone(paths["pkg/consumer.py", "outsider"])
            self.assertEqual(paths["entry.ts", "./setup.mjs"], "setup.mts")
            self.assertEqual(paths["entry.ts", "action"], "setup.mts")
            self.assertFalse(any(source == "sample.go" and target == "fmt.py" for (source, _), target in paths.items()))
            self.assertIn("sample.Type", {node.label for node in graph.store.find_nodes_by_types(("Function",))})
            self.assertFalse(any(
                node.properties.get("finding_type") == "unused_import" and node.properties["relative_path"] == "entry.ts"
                for node in graph.store.find_nodes_by_types(("StaticAnalysisFinding",))
            ))

    def test_solidity_remapping_edits_refresh_unchanged_sources(self) -> None:
        self.write("contracts/Main.sol", 'pragma solidity ^0.8.0; import {Lib} from "@lib/Lib.sol"; contract Main {}')
        for version in ("one", "two"):
            self.write(f"{version}/Lib.sol", "pragma solidity ^0.8.0; library Lib {}")
        self.write("remappings.txt", "@lib/=one/\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            def resolved():
                return {node.properties.get("resolved_relative_path") for node in graph.store.find_nodes_by_types(("Import",))
                        if node.status == "active" and node.properties["relative_path"] == "contracts/Main.sol"}

            self.assertFalse(graph.compile_project(self.root).run.errors)
            self.assertEqual(resolved(), {"one/Lib.sol"})
            self.write("remappings.txt", "@lib/=two/\n")
            update = graph.update_project(self.root)
            self.assertFalse(update.run.errors)
            self.assertEqual(update.run.files_changed, 4)
            self.assertEqual(resolved(), {"two/Lib.sol"})

    def test_known_python_router_alias_preserves_the_literal_http_method(self) -> None:
        self.write("routes.py", """
from fastapi.applications import FastAPI
app = FastAPI()
router = app
@router.api_route("/tunnel", methods=["CONNECT"])
def connect(): pass
""")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            self.assertEqual([node.label for node in graph.store.find_nodes_by_types(("Endpoint",))], ["CONNECT /tunnel"])

    def test_scan_classifies_interpreter_and_grammar_before_content_heuristics(self) -> None:
        cases = {
            "tool": ("#!/usr/bin/env -S python3 -u\n# A script heading\n", "Python"),
            "wrapper": ("#!/usr/bin/env node-wrapper\n", None),
            ".bashrc": ("echo hello\n", "Bash"),
            "Example.fs": ("module Example\n", "F#"),
            "LICENSE": ("# License\n\nTerms\n", None),
            "guide.txt": ("\ufeffGuide\n=====\n", "Markdown"),
            "later.txt": ("line\n" * 121 + "# Heading\n", "Markdown"),
        }
        for path, (content, expected) in cases.items():
            with self.subTest(path=path):
                self.assertEqual(classify_path(path, content.encode("utf-8")).language, expected)
        self.assertEqual(classify_path("LICENSE", b"# License\n").artifact_type, "text")
        self.assertEqual(tree_sitter_language_key("component", "TSX"), "tsx")
        fake_pack = SimpleNamespace(available_languages=lambda: ["typescript"], get_language=lambda key: key)
        with patch("memory.code_analysis.catalog._load_direct_language", return_value=None), \
                patch("memory.code_analysis.catalog.import_module", return_value=fake_pack):
            with self.assertRaisesRegex(ValueError, "tsx.*not available locally"):
                load_tree_sitter_language(SimpleNamespace(Language=str), "tsx")

    def test_markdown_code_blocks_and_crlf_paragraphs_keep_distinct_spans(self) -> None:
        self.write("guide.md", "\ufeffTitle\n=====\n\n```py\none()\n```\n\n```py\ntwo()\n```\n\n   ## C# ##\n")
        text = "  alpha\r\nbeta\r\n\r\n  gamma"
        self.write("data.txt", text)
        config = merge_overrides(default_config(), {"compile.documents.markdown": True})
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            fragments = [node for node in graph.store.find_nodes_by_types(("SourceFragment",))
                         if node.properties["relative_path"] == "guide.md"]
            self.assertEqual({node.text for node in fragments if node.properties["fragment_type"] == "heading"}, {"Title", "C#"})
            self.assertEqual({node.text for node in fragments if node.properties["fragment_type"] == "code_block"}, {"one()", "two()"})
        artifact = next(item for item in ProjectScanner().scan(self.root).artifacts if item.relative_path == "data.txt")
        parsed = PlainTextParser(max_chars=5).parse(artifact, text.encode())
        self.assertEqual([(item.text, item.start_line, item.end_line, item.start_offset, item.end_offset) for item in parsed.fragments],
                         [("alpha", 1, 1, 2, 7), ("beta", 2, 2, 9, 13), ("gamma", 4, 4, 19, 24)])

    def test_pdf_reads_the_compiler_snapshot_and_only_marks_missing_pypdf(self) -> None:
        self.write("report.pdf", "%PDF-replaced-file")
        artifact = ProjectScanner().scan(self.root).artifacts[0]
        content = b"%PDF-verified-snapshot"

        def reader(stream):
            self.assertIsInstance(stream, BytesIO)
            self.assertEqual(stream.read(), content)
            return SimpleNamespace(metadata={}, pages=[SimpleNamespace(extract_text=lambda: "Extracted text")])

        with patch("memory.document_ingestion.formats.pdf.import_module", return_value=SimpleNamespace(PdfReader=reader)):
            self.assertEqual(PDFParser().parse(artifact, content).fragments[0].text, "Extracted text")
        missing = ModuleNotFoundError("missing pypdf", name="pypdf")
        with patch("memory.document_ingestion.formats.pdf.import_module", side_effect=missing):
            result = PDFParser().parse(artifact, content)
            self.assertEqual(result.metadata["status"], "needs_parser")
            self.assertFalse(result.metadata.get("needs_ocr"))
        broken = ModuleNotFoundError("missing transitive dependency", name="dependency")
        with patch("memory.document_ingestion.formats.pdf.import_module", side_effect=broken):
            with self.assertRaisesRegex(ModuleNotFoundError, "missing transitive dependency"):
                PDFParser().parse(artifact, content)

    def test_compilation_retries_when_a_scanned_file_disappears(self) -> None:
        source = self.write("gone.py", "def earlier(): pass\n")
        self.write("steady.py", "def untouched(): pass\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            self.write("gone.py", "def changed(): pass\n")
            read_bytes = Path.read_bytes

            def remove_then_read(path):
                if path == source and source.exists():
                    source.unlink()
                return read_bytes(path)

            with patch.object(Path, "read_bytes", remove_then_read):
                result = graph.update_project(self.root)
            self.assertFalse(result.run.errors)
            self.assertEqual(result.run.files_deleted, 1)
            self.assertEqual({node.label for node in graph.store.find_nodes_by_types(("Function",))
                              if node.status == "active"}, {"steady.untouched"})

    def test_reclassifying_an_extensionless_script_archives_its_code(self) -> None:
        self.write("tool", "#!/usr/bin/env python3\ndef build_widget(): pass\n")
        self.write("guide.md", "# Guide\n\nUse tool.build_widget to set up the widget.\n")
        config = merge_overrides(default_config(), {"compile.documents.markdown": True})
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            widget = next(node for node in graph.store.find_nodes_by_types(("Function",)) if node.label == "tool.build_widget")
            references = graph.store.get_edges(to_id=widget.id, type_="REFERENCES", limit=None)
            reference, = (edge for edge in references if edge.properties.get("extractor") == "document_code_linker")
            self.assertEqual(reference.properties["status"], "active")
            self.write("tool", "# Tool documentation\n\nDescription.\n")
            result = graph.update_project(self.root)
            self.assertFalse(result.run.errors)
            self.assertIn(widget.id, result.delta.archived_nodes)
            self.assertEqual(graph.store.get_node(widget.id).status, "archived")
            archived = graph.store.get_edges(to_id=widget.id, type_="REFERENCES", limit=None)
            self.assertEqual({edge.id: edge.properties["status"] for edge in archived},
                             {edge.id: "archived" for edge in references})

    def test_orphan_directory_evidence_is_archived_and_restored_with_the_finding(self) -> None:
        self.write("legacy/first.py", "def first(): pass\n")
        self.write("legacy/second.py", "def second(): pass\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            edge = next(edge for edge in graph.store.get_edges(type_="HAS_FINDING", limit=None)
                        if edge.properties.get("extractor") == "orphan_directory_analyzer")
            consumer = self.write("consumer.py", "from legacy.first import first\nfirst()\n")
            self.assertFalse(graph.update_project(self.root).run.errors)
            self.assertEqual(graph.store.get_node(edge.to_id).status, "archived")
            self.assertEqual(graph.store.get_edge(edge.id).properties["status"], "archived")
            consumer.unlink()
            self.assertFalse(graph.update_project(self.root).run.errors)
            self.assertEqual(graph.store.get_node(edge.to_id).status, "active")
            self.assertEqual(graph.store.get_edge(edge.id).properties["status"], "active")

