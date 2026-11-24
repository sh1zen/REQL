"""Regression coverage for scan policy and statically inferred project facts."""
from contextlib import closing, redirect_stdout
from codecs import BOM_UTF16_BE, BOM_UTF16_LE
from io import StringIO
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memory.artifacts.models import ScanError
from memory.artifacts.options import CompilationOptions
from memory.artifacts.scanner import ProjectScanner
from memory.cli import main
from memory.config import default_config, load_effective_config, merge_overrides
from memory.domain.exceptions import StorageError
from memory.domain.models import MemoryEdge
from reql import MemoryGraph


class AuditRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.storage = self.root / ".reql" / "memory.reql"

    def write(self, relative: str, source: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def test_compile_summary_confines_all_test_evidence_to_its_project(self) -> None:
        local = self.root / "local"
        other = self.root / "other"
        widget = self.write("local/widget.py", "def widget(): return 1\n")
        self.write("other/tests/test_widget.py", "def test_widget(): pass\n")
        self.write("other/spec/widget_contract.py", "def widget_contract(): pass\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(other).run.errors)
            summary = graph.compile_project(local).summary
            self.assertEqual(summary.associated_tests, [])

            source, foreign = (
                next(node for node in graph.store.find_nodes_by_types(("Function",)) if node.label == label)
                for label in ("widget.widget", "spec.widget_contract.widget_contract")
            )
            graph.store.upsert_edge(MemoryEdge(id="cross-project-call", from_id=foreign.id, to_id=source.id, type="CALLS"))
            widget.write_text("def widget(): return 2\n", encoding="utf-8")
            self.write("local/tests/test_widget.py", "def test_widget(): pass\n")
            self.write("local/spec/widget_checks.py", "def widget_checks(): pass\n")
            self.assertFalse(graph.update_project(local).run.errors)

            widget.write_text("def widget(): return 3\n", encoding="utf-8")
            result = graph.update_project(local)
            self.assertFalse(result.run.errors)
            self.assertEqual(
                [(test.path, test.reason) for test in result.summary.associated_tests],
                [("spec/widget_checks.py", "changed symbol match"), ("tests/test_widget.py", "matching test filename")],
            )

    def test_nested_test_paths_are_test_evidence_in_cleanup_and_data_traces(self) -> None:
        test_path = "src/__tests__/widget.ts"
        self.write("src/widget.ts", "export function processWidget() { return 42; }\n")
        self.write(test_path, """import { processWidget } from '../widget';
export function verifyWidget() { return processWidget(); }
""")
        trace_test_path = "src/__tests__/field.ts"
        self.write(trace_test_path, "const EXPECTED = 'Field.type';\n")
        self.write("wire_model.py", "class WireField:\n    type: str\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            finding = next(
                node for node in graph.store.find_nodes_by_types(("StaticAnalysisFinding",))
                if node.properties.get("symbol_name") == "processWidget"
            )
            # Keep a prior finding visible alongside the newly indexed call so cleanup can check the reference.
            graph.store.update_node_fields(
                finding.id, status="active",
                properties={**finding.properties, "status": "active", "removal_safety": "safe", "blocking_signals": []},
            )
            cleanup = graph.query_context_payload(
                "processWidget", mode="cleanup", scopes=["code", "test"], top_k=30, max_depth=3, max_items=30,
            )["payload"]
            self.assertIn((test_path, "test_ref", "CALLS"), {
                (row.get("path"), row.get("read_kind"), row.get("edge_type"))
                for row in cleanup["targeted_reads"]
            })
            trace = graph.query_context_payload("Field.type", top_k=30, max_depth=3, max_items=30)["payload"]["data_trace"]
            self.assertIn((trace_test_path, "test/fixture"), {(row["path"], row["stage"]) for row in trace})

    def test_configured_entrypoint_agrees_across_explain_pipeline_and_html(self) -> None:
        self.write("jobs.py", """def calculate(): return 1
def _main(): return calculate()
def _helper(): return calculate()
def run(): return calculate()
""")
        self.write("reql.conf", 'analysis:\n  workflow:\n    entry_names: ["_main"]\n')
        original = Path.cwd()
        self.addCleanup(os.chdir, original)
        os.chdir(self.root)
        with closing(MemoryGraph.open(self.storage, config=load_effective_config(start_dir=self.root, env={}))) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            pipeline = graph.project_pipeline(self.root)
            self.assertEqual({workflow.trigger.label for workflow in pipeline.workflows if not workflow.inferred}, {"_main"})
            html = graph.export_html(self.root / ".reql/graph.html").read_text(encoding="utf-8")
            data = re.search(r'<script id="graph-data" type="application/json">(.*?)</script>', html, re.DOTALL)
            self.assertIsNotNone(data)
            assert data is not None
            entrypoints = {node["properties"]["name"] for node in json.loads(data[1])["nodes"] if node["entrypoint"]}
            self.assertEqual(entrypoints, {"_main"})
        with closing(MemoryGraph.open(self.storage, read_only=True, config=default_config())) as graph:
            pipeline = graph.project_pipeline(self.root)
            self.assertEqual({(workflow.trigger.label, workflow.inferred) for workflow in pipeline.workflows}, {("run", True)})
        with redirect_stdout(StringIO()) as out:
            self.assertEqual(main(["project", "explain", "--json"]), 0)
        explanation = json.loads(out.getvalue())
        self.assertEqual(
            {item["target"]["label"] for workflow in explanation["workflows"]
             for item in workflow["participants"] if item["role"] == "trigger"},
            {"_main"},
        )

    def test_extensionless_ecmascript_imports_resolve_mts_and_cts(self) -> None:
        self.write("main.mts", """import { tool } from './tool';
import { worker } from './worker';
export function run() { return tool() + worker(); }
""")
        self.write("tool.mts", "export function tool() { return 1; }\n")
        self.write("worker/index.cts", "export function worker() { return 2; }\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            imports = {
                node.id: node.properties["resolved_relative_path"]
                for node in graph.store.find_nodes_by_types(("Import",))
                if node.properties.get("relative_path") == "main.mts"
            }
            self.assertEqual(set(imports.values()), {"tool.mts", "worker/index.cts"})
            calls = graph.store.get_edges(type_="CALLS", limit=None)
            self.assertEqual(
                {graph.store.get_node(edge.to_id).properties["relative_path"] for edge in calls},
                {"tool.mts", "worker/index.cts"},
            )

    def test_surface_format_controls_linking_and_retrieval_scope(self) -> None:
        stylesheet = "ui/standalone.css"
        template = "ui/widget.html"
        notes = "views/notes.md"
        self.write(stylesheet, ".widget-marker { color: red; }\n")
        self.write(template, '<div class="widget-marker">Widget</div>\n')
        self.write(notes, '# Notes\n\n<div class="widget-marker">Documentation</div>\n')
        self.write("app.py", "def run(): return render_template('notes.md')\n")
        config = merge_overrides(default_config(), {"compile.documents.markdown": True})
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            scopes = {
                node.properties["relative_path"]: node.properties["context_scope"]
                for node in graph.store.find_nodes_by_types(("File",))
            }
            self.assertEqual({path: scopes[path] for path in (stylesheet, template, notes)}, {
                stylesheet: "code", template: "code", notes: "docs",
            })
            references = graph.store.get_edges(type_="REFERENCES", limit=None)
            shared_paths = {
                frozenset((edge.properties["relative_path"], edge.properties["target_relative_path"]))
                for edge in references if edge.properties.get("relation") == "shared_surface_identifier"
            }
            self.assertEqual(shared_paths, {frozenset((stylesheet, template))})
            self.assertFalse(any(edge.properties.get("relation") == "template_render" for edge in references))
            payload = graph.query_context_payload("widget-marker", scopes=["code"], top_k=30, max_items=30)["payload"]
            code_paths = {row["path"] for row in payload["working_set"]}
            self.assertTrue({stylesheet, template}.issubset(code_paths))
            self.assertNotIn(notes, code_paths)
            docs = graph.query_context_payload("Documentation", scopes=["docs"], top_k=30, max_items=30)
            self.assertIn(notes, json.dumps(docs))

    def test_utf16_compiles_code_and_documents_and_preserves_verified_source(self) -> None:
        files = {
            "widget.py": BOM_UTF16_LE + "def utf16_widget():\r\n    return 'caff\u00e8'\r\n".encode("utf-16-le"),
            "guide.txt": BOM_UTF16_BE + "# UTF-16 guide\n\nCaff\u00e8 in the guide.\n".encode("utf-16-be"),
            "plain.txt": "Plain text without a byte-order mark.\n".encode("utf-16-le"),
            "binary.py": b"real\x00binary\x00\x01\x02",
        }
        for relative, content in files.items():
            (self.root / relative).write_bytes(content)
        config = merge_overrides(default_config(), {"compile.documents.markdown": True, "compile.documents.plain_text": True})
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            result = graph.compile_project(self.root)
            self.assertFalse(result.run.errors)
            self.assertEqual({item.relative_path: item.artifact_type for item in result.scan.artifacts}, {
                "widget.py": "code", "guide.txt": "markdown", "plain.txt": "text", "binary.py": "binary",
            })
            fragments = graph.store.find_nodes_by_types(("SourceFragment",))
            self.assertTrue(any("Caff\u00e8 in the guide." in node.text for node in fragments))
            self.assertTrue(any("Plain text without a byte-order mark." in node.text for node in fragments))
            payload = graph.query_context_payload("utf16_widget", scopes=["code"], include_source=True)["payload"]
            self.assertTrue(any(
                row.get("source_status") == "verified" and "return 'caff\u00e8'" in row.get("text", "")
                for row in payload["snippets"]
            ), payload["snippets"])

    def test_project_report_computes_analysis_without_changing_the_graph(self) -> None:
        self.write("app.py", "def calculate(): return 42\ndef run(): return calculate()\n")
        original = Path.cwd()
        self.addCleanup(os.chdir, original)
        os.chdir(self.root)
        output = self.root / ".reql/reports"
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["project", "report", "--output", str(output)]), 0)
        self.assertTrue((output / "CACHE_REPORT.md").is_file())
        self.assertFalse(self.storage.exists())
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            before = graph.store.export_json()
        with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
            report = graph.project_report(self.root, output_dir=output).graph_report.read_text(encoding="utf-8")
            self.assertRegex(report, r"(?m)^- \`community:[^ ]+\` .* size=\d+")
            self.assertIn("- rank=1 ", report)
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["project", "report", "--output", str(output)]), 0)
        with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
            after = graph.store.export_json()
        self.assertEqual(after["nodes"], before["nodes"])
        self.assertEqual(after["edges"], before["edges"])

    def test_api_scan_and_cache_defaults_follow_graph_configuration(self) -> None:
        self.write("keep.py", "VALUE = 1\n")
        self.write("excluded.py", "VALUE = 2\n")
        self.write("big.py", "#" * 2048 + "\n")
        self.write("notes.md", "# Notes\n")
        config = merge_overrides(default_config(), {
            "scan.include": ["*.py"],
            "scan.exclude": ["excluded.py"],
            "scan.max_file_size_mb": 0.001,
            "cache.enabled": False,
        })
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            result = graph.compile_project(self.root)
            self.assertEqual([item.relative_path for item in result.scan.artifacts], ["keep.py"])
            self.assertEqual(graph.workspace_status(self.root)["freshness"], "current")
            updated = graph.update_project(self.root)
            self.assertEqual([item.relative_path for item in updated.scan.artifacts], ["keep.py"])
            self.assertIsNone(updated.revision)
            self.assertEqual(graph.workspace_status(self.root)["freshness"], "current")
            self.assertIs(graph.cache_status(self.root)["cache_enabled"], False)
            changed = graph.update_project(
                self.root, max_file_size_bytes=8192, include_patterns=[], exclude_patterns=[], cache_enabled=True,
            )
            self.assertEqual(
                {item.relative_path for item in changed.scan.artifacts},
                {"keep.py", "excluded.py", "big.py", "notes.md"},
            )
            status = graph.cache_status(
                self.root, max_file_size_bytes=8192, include_patterns=[], exclude_patterns=[], cache_enabled=True,
            )
            self.assertEqual(status["cached_artifacts"], 4)

    def test_explicit_scan_override_can_disable_builtin_exclusions(self) -> None:
        self.write("project/node_modules/widget.js", "export function widget() {}\n")
        self.write("project/skip.py", "VALUE = 1\n")
        config = merge_overrides(default_config(), {"scan.exclude": ["skip.py"]})
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            result = graph.compile_project(self.root / "project", parsing_options=CompilationOptions(ignore_defaults=True))
            self.assertEqual([item.relative_path for item in result.scan.artifacts], ["node_modules/widget.js"])

    def test_cli_report_and_stats_share_project_scan_policy(self) -> None:
        self.write("service.py", "VALUE = 1\n")
        self.write("other.py", "VALUE = 2\n")
        config_path = self.write("reql.conf", 'scan:\n  include: ["*.py"]\n')
        original = Path.cwd()
        self.addCleanup(os.chdir, original)
        os.chdir(self.root)
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["project", "compile"]), 0)
        config_path.write_text('scan:\n  include: ["*.py"]\n  exclude: ["./other.py"]\n', encoding="utf-8")
        with redirect_stdout(StringIO()) as out:
            self.assertEqual(main(["stats", "--json"]), 0)
        cache = json.loads(out.getvalue())["cache"]
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["project", "report", "--output", ".reql/reports"]), 0)
        report = (self.root / ".reql/reports/CACHE_REPORT.md").read_text(encoding="utf-8")
        self.assertEqual(cache["deleted_artifacts"], 1)
        for label, key in (
            ("Cached", "cached_artifacts"), ("Dirty", "dirty_artifacts"), ("Deleted", "deleted_artifacts"),
        ):
            self.assertIn(f"- {label} artifacts: {cache[key]}\n", report)

    def test_report_rejects_both_incomplete_and_failed_scans(self) -> None:
        self.write("service.py", "VALUE = 1\n")
        output = self.root / ".reql/reports"
        with closing(MemoryGraph.open(self.storage)) as graph:
            graph.compile_project(self.root)
            incomplete = ProjectScanner().scan(self.root)
            incomplete.errors.append(ScanError(path="service.py", relative_path="service.py", message="unreadable source"))
            for fake in ({"return_value": incomplete}, {"side_effect": OSError("unreadable source")}):
                with self.subTest(fake=list(fake)):
                    with patch.object(ProjectScanner, "scan", **fake), self.assertRaisesRegex(StorageError, "unreadable source"):
                        graph.project_report(self.root, output_dir=output)
                    self.assertFalse(output.exists())

    def test_report_surfaces_recoverable_analysis_failure(self) -> None:
        self.write("service.py", "VALUE = 1\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            graph.compile_project(self.root)
            with patch("memory.reporting.project_report.CommunityDetector.detect", side_effect=StorageError("store unavailable")):
                reports = graph.project_report(self.root, output_dir=self.root / ".reql/reports")
            self.assertIn("Community analysis unavailable: store unavailable", reports.graph_report.read_text(encoding="utf-8"))

    def test_test_path_classification_agrees_in_summary_explain_and_export(self) -> None:
        self.write("src/widget.ts", "export function widget() { return 1; }\n")
        self.write("src/widget.test.helper.ts", "export function helper() { return 1; }\n")
        test_paths = {"src/widget.test.ts", "src/__tests__/widget.ts", "spec/widget.ts"}
        for path in test_paths:
            self.write(path, 'test("widget", () => 1);\n')
        with closing(MemoryGraph.open(self.storage)) as graph:
            result = graph.compile_project(self.root)
            self.assertFalse(result.run.errors)
            self.assertEqual({row.path for row in result.summary.associated_tests}, test_paths)
            explanation = graph.explain_project(self.root)
            self.assertEqual({path for group in explanation.capabilities for path in group.tests}, test_paths)
            self.assertTrue(all(
                path not in test_paths for group in explanation.capabilities for path in group.primary_paths
            ))
            html = graph.export_html(self.root / ".reql/graph.html").read_text(encoding="utf-8")
            data = re.search(r'<script id="graph-data" type="application/json">(.*?)</script>', html, re.DOTALL)
            self.assertIsNotNone(data)
            assert data is not None
            nodes = json.loads(data[1])["nodes"]
            visible = {node.get("properties", {}).get("relative_path") for node in nodes}
            self.assertTrue(test_paths.isdisjoint(visible))
            self.assertIn("src/widget.ts", visible)
            self.assertIn("src/widget.test.helper.ts", visible)

    def test_ecmascript_test_bindings_in_configured_directory_keep_callback_evidence(self) -> None:
        """Index namespace imports, default node:test imports and curried cases."""
        self.write("reql.conf", 'testing:\n  discovery:\n    directories: ["quality-[0-9]+"]\n')
        self.write("widget.ts", "export function widget() { return 1; }\n")
        path = "packages/quality-42/widget_checks.ts"
        self.write(path, """import * as suite from 'vitest';
import nodeTest from 'node:test';
import { test as other, widget } from '../../widget';
suite.test.each([1])('parameterized', () => widget());
nodeTest('options', { timeout: 10 }, () => widget());
suite.test.todo('planned');
other('ordinary callback', () => widget());
""")
        with closing(MemoryGraph.open(self.storage, config=load_effective_config(start_dir=self.root))) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            tests = {node.properties["name"]: node for node in graph.store.find_nodes_by_types(("Function",))
                     if node.properties.get("relative_path") == path}
            self.assertEqual(set(tests), {"parameterized", "options", "planned"})
            self.assertEqual(tests["parameterized"].properties["metadata"]["test_modifiers"], ["each"])
            self.assertEqual({row.test_id for row in graph.find_tests_for(file="widget.ts")},
                             {tests[name].id for name in ("parameterized", "options")})

    def test_ecmascript_test_imports_and_modifiers_keep_callback_evidence(self) -> None:
        self.write("widget.ts", "export function widget() { return 1; }\n")
        path = "widget.test.ts"
        self.write(path, """
import { widget } from './widget';
test('regular', () => widget());
it.only('focused', () => widget());
check.skip('skipped', () => widget());
check.concurrent.only('concurrent', () => widget());
probe('ordinary callback', () => widget());
import { test as check } from 'vitest';
import { test as probe } from './widget';
""")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            functions = {
                node.properties["name"]: node for node in graph.store.find_nodes_by_types(("Function",))
                if node.properties.get("relative_path") == path
            }
            self.assertEqual(set(functions), {"regular", "focused", "skipped", "concurrent"})
            self.assertEqual(functions["concurrent"].properties["metadata"]["test_modifiers"], ["concurrent", "only"])
            widget = next(node for node in graph.store.find_nodes_by_types(("Function",)) if node.label == "widget.widget")
            evidence = graph.find_tests_for(symbol_id=widget.id)
            self.assertEqual({row.test_id for row in evidence}, {node.id for node in functions.values()})

    def test_python_endpoint_evidence_and_identity_are_source_local(self) -> None:
        first = self.write("one.py", """
import flask as web
from fastapi import APIRouter as Router

app = web.Flask(__name__)
router = Router(prefix="/one")
registry = {}

@registry.get("/cache")
def cache(): pass

@app.route("/write", methods=["POST", "PUT", "POST"])
def write(): pass

@router.get("/status")
def status(): pass

@app.get("/status")
def health(): pass

@route("/convention")
def convention(): pass

def nested(app):
    @app.get("/shadowed")
    def shadowed(): pass
""")
        self.write("two.py", """
from fastapi import FastAPI
app = FastAPI()

@app.get("/status")
def health(): pass
""")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            endpoints = {
                (node.label, node.properties["handler"]): node.id
                for node in graph.store.find_nodes_by_types(("Endpoint",)) if node.status == "active"
            }
            self.assertEqual(set(endpoints), {
                ("POST /write", "one.write"), ("PUT /write", "one.write"),
                ("GET /one/status", "one.status"), ("GET /status", "one.health"),
                ("GET /status", "two.health"), ("ANY /convention", "one.convention"),
            })
            self.assertEqual(len(set(endpoints.values())), 6)
            self.assertEqual(graph.compile_project(self.root).run.files_changed, 0)
            first.unlink()
            self.assertFalse(graph.update_project(self.root).run.errors)
            active = [node.id for node in graph.store.find_nodes_by_types(("Endpoint",)) if node.status == "active"]
            self.assertEqual(active, [endpoints[("GET /status", "two.health")]])

    def test_css_distinguishes_fallbacks_from_proven_redundancy(self) -> None:
        self.write("public/app.css", """
.fallback { display: -webkit-box; display: flex; }
.invalid { color: red; color: invalid; }
.repeat { color: blue; color: blue; }
.priority { color: red !important; color: blue; color: black; }
""")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            findings = {
                (node.properties["finding_type"], node.properties["symbol_name"]): node.properties["removal_safety"]
                for node in graph.store.find_nodes_by_types(("StaticAnalysisFinding",))
                if node.status == "active" and "overridden_css_declaration" in node.properties["finding_type"]
            }
            self.assertEqual(findings, {
                ("always_overridden_css_declaration", ".repeat color"): "safe",
                ("possibly_overridden_css_declaration", ".fallback display"): "validate",
                ("possibly_overridden_css_declaration", ".invalid color"): "validate",
            })

    def test_markdown_preserves_shorter_and_labelled_inner_fences(self) -> None:
        self.write("README.md", "~~~~markdown\n~~~\ninside\n~~~~bash\n~~~~~\nafter\n")
        config = merge_overrides(default_config(), {"compile.documents.markdown": True})
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            fragments = {
                (node.properties.get("fragment_type"), node.text)
                for node in graph.store.find_nodes_by_types(("SourceFragment",))
            }
            self.assertIn(("code_block", "~~~\ninside\n~~~~bash"), fragments)
            self.assertIn(("paragraph", "after"), fragments)
            self.assertNotIn(("paragraph", "inside"), fragments)

    def test_findings_return_after_the_last_resolved_import_is_removed(self) -> None:
        self.write("implementation.py", """def run(): pass

class Alpha:
    def save(self): pass

class Beta:
    def save(self): pass
""")
        config = merge_overrides(default_config(), {"retention.commits": 1})
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            def findings():
                return {
                    node.properties["qualified_name"]: node
                    for node in graph.store.find_nodes_by_types(("StaticAnalysisFinding",))
                    if node.properties.get("finding_type") in {"possibly_unused_function", "possibly_unused_method"}
                }

            graph.compile_project(self.root)
            declared = findings()["implementation.run"]
            importer = self.write("consumer.py", "from implementation import run\n")
            self.write("dynamic.py", "def invoke(remote):\n    remote.save()\n")
            graph.compile_project(self.root)
            suppressed = findings()
            self.assertEqual(suppressed["implementation.run"].status, "archived")
            self.assertEqual(suppressed["implementation.Alpha.save"].status, "active")
            self.assertEqual(suppressed["implementation.Beta.save"].status, "active")
            imports = graph.store.get_edges(to_id=declared.properties["symbol_id"], type_="IMPORTS")
            self.assertEqual([(edge.properties["source_file"], edge.properties["status"]) for edge in imports],
                             [("consumer.py", "active")])

            self.write("unrelated.py", "VALUE = 1\n")
            graph.compile_project(self.root)
            self.assertEqual(findings()["implementation.run"].id, declared.id)
            importer.unlink()
            graph.update_project(self.root)
            restored = findings()["implementation.run"]
            self.assertEqual((restored.id, restored.status), (declared.id, "active"))
            evidence = graph.store.get_edges(to_id=restored.id, type_="HAS_FINDING")
            self.assertEqual(len(evidence), 2)
            self.assertEqual({edge.properties["status"] for edge in evidence}, {"active"})
            self.assertEqual(graph.update_project(self.root).run.files_changed, 0)

    def test_removing_an_import_does_not_restore_a_finding_removed_by_new_source(self) -> None:
        implementation = self.write("implementation.py", "def run(): pass\n")
        importer = self.write("consumer.py", "from implementation import run\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            graph.compile_project(self.root)
            finding = next(
                node for node in graph.store.find_nodes_by_types(("StaticAnalysisFinding",))
                if node.properties.get("qualified_name") == "implementation.run"
            )
            self.assertEqual(finding.status, "archived")
            implementation.write_text("def run(): pass\nrun()\n", encoding="utf-8")
            graph.update_project(self.root)
            importer.unlink()
            graph.update_project(self.root)
            self.assertEqual(graph.get_node(finding.id).status, "archived")
            self.assertNotIn("archived_reason", graph.get_node(finding.id).properties)

    def test_named_import_uses_the_export_binding_not_an_unexported_same_name(self) -> None:
        self.write("implementation.ts", "function visible() {}\nexport { visible as api };\n")
        self.write("consumer.ts", "import { api, visible } from './implementation';\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            result = graph.compile_project(self.root)
            self.assertFalse(result.run.errors)
            target = next(node for node in graph.store.find_nodes_by_types(("Function",)) if node.properties["name"] == "visible")
            imports = graph.store.get_edges(to_id=target.id, type_="IMPORTS")
            binding, = (edge for edge in imports if edge.properties["status"] == "active")
            self.assertEqual(binding.properties["import_name"], "api")
            self.write("implementation.ts", "function visible() {}\nexport { visible as renamed };\n")
            self.assertFalse(graph.update_project(self.root).run.errors)
            archived = graph.store.get_edges(to_id=target.id, type_="IMPORTS")
            self.assertIn(binding.id, {edge.id for edge in archived})
            self.assertEqual({edge.properties["status"] for edge in archived}, {"archived"})

    def test_pipeline_covers_independent_cycles_and_drops_archived_calls(self) -> None:
        path = self.write("loops.py", """def alpha(): beta()
def beta(): alpha()
def gamma(): delta()
def delta(): gamma()
def omega(): terminal()
def terminal(): pass
""")
        with closing(MemoryGraph.open(self.storage)) as graph:
            graph.compile_project(self.root)
            def triggers():
                return {item.trigger.label for item in graph.project_pipeline(self.root).workflows if item.inferred}

            self.assertEqual(triggers(), {"alpha", "gamma", "omega"})
            path.write_text(path.read_text(encoding="utf-8").replace("def gamma(): delta()", "def gamma(): pass"), encoding="utf-8")
            graph.update_project(self.root)
            self.assertEqual(triggers(), {"alpha", "delta", "omega"})

    def test_report_counts_project_and_shared_call_edges(self) -> None:
        self.write("local/source.py", "def helper(): pass\ndef run(): helper()\n")
        self.write("other/source.py", "def helper(): pass\ndef run(): helper()\n")
        local = self.root / "local"
        other = self.root / "other"
        with closing(MemoryGraph.open(self.storage)) as graph:
            local_project = graph.compile_project(local).scan.project.id
            other_project = graph.compile_project(other).scan.project.id
            edges = graph.store.get_edges(type_="CALLS", limit=None)
            self.assertEqual({edge.properties["project_id"] for edge in edges}, {local_project, other_project})
            selected = next(edge for edge in edges if edge.properties["project_id"] == local_project)
            graph.store.upsert_edge(MemoryEdge(id="shared-call", from_id=selected.to_id, to_id=selected.from_id, type="CALLS"))
            report = graph.project_report(local, output_dir=self.root / ".reql/reports")
            self.assertIn("- call edges: 2\n", report.graph_report.read_text(encoding="utf-8"))

    def test_template_resolves_to_sibling_views_with_lowercase_controllers(self) -> None:
        self.write("packages/store/controllers/Stock.php", "<?php function stock() { return view('inventory/list'); }\n")
        template = "packages/store/views/inventory/list.php"
        self.write(template, "<p>Inventory</p>\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            links = [
                edge for edge in graph.store.get_edges(type_="REFERENCES", limit=None)
                if edge.properties.get("relation") == "template_render"
            ]
            self.assertEqual([edge.properties["resolved_relative_path"] for edge in links], [template])


    def test_unchanged_import_refreshes_its_target_on_file_addition_and_deletion(self) -> None:
        self.write("consumer.py", "from implementation import api\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            def import_node():
                return next(
                    node for node in graph.store.find_nodes_by_types(("Import",))
                    if node.properties.get("binding_name") == "api"
                )

            graph.compile_project(self.root)
            original = import_node()
            self.assertIsNone(original.properties["resolved_relative_path"])

            target = self.write("implementation.py", "def api(): pass\n")
            self.assertFalse(graph.update_project(self.root).run.errors)
            resolved = import_node()
            self.assertEqual(resolved.id, original.id)
            self.assertEqual(resolved.properties["resolved_relative_path"], "implementation.py")
            bindings = graph.store.get_edges(from_id=original.id, type_="IMPORTS")
            binding, = (edge for edge in bindings if edge.properties["status"] == "active")

            target.unlink()
            self.assertFalse(graph.update_project(self.root).run.errors)
            self.assertIsNone(import_node().properties["resolved_relative_path"])
            archived = graph.store.get_edges(from_id=original.id, type_="IMPORTS")
            self.assertIn(binding.id, {edge.id for edge in archived})
            self.assertEqual({edge.properties["status"] for edge in archived}, {"archived"})
