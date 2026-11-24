from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from dataclasses import asdict
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.tools import MCPToolError, call_tool, list_tools, reql_tests_for, reql_tests_import
from memory.cli import main
from memory.config import load_effective_config
from reql import MemoryGraph, QueryContextRequest


class TestIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent = Path(directory.name)
        self.root = self.parent / "project"
        self.root.mkdir()
        (self.root / "tests").mkdir()
        self.module = self.root / "invoice.py"
        self.module.write_text("def issued():\n    return 42\n", encoding="utf-8")
        self.test = self.root / "tests/test_invoice.py"
        self.test.write_text("from invoice import issued as make\n\ndef test_issued():\n    assert make() == 42\n", encoding="utf-8")
        (self.root / "tests/test_names.py").write_text("def test_issued():\n    assert True\n", encoding="utf-8")
        self.storage = self.root / ".reql/memory.reql"
        self.graph = MemoryGraph.open(self.storage)
        self.addCleanup(self.graph.close)
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        self.target = next(node for node in self.graph.store.find_nodes_by_property("name", "issued")
                           if node.type == "Function")

    def report(self, *, per_test: bool = True, hashes: bool = True) -> Path:
        data = {"files": {"invoice.py": {"executed_lines": [2]}}}
        if per_test:
            data["files"]["invoice.py"]["contexts"] = {"2": ["tests/test_invoice.py::test_issued|run", ""]}
        if hashes:
            data["reql"] = {"source_hashes": {
                path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (self.test, self.module)
            }}
        report = self.root / "coverage.json"
        report.write_text(json.dumps(data), encoding="utf-8")
        return report

    def test_static_evidence_candidates_and_idempotent_compile(self) -> None:
        actual = self.graph.find_tests_for(symbol_id=self.target.id, include_candidates=True)
        self.assertEqual([row.evidence_kind for row in actual], ["static", "candidate"])
        self.assertTrue(actual[0].verified_current)
        self.assertFalse(actual[1].verified_current)
        context = self.graph.query_context_result(QueryContextRequest.from_raw(text="issued", scopes=["code"]))
        self.assertIn("static", {row.get("evidence_kind") for row in context.payload["test_targets"]})
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 0)
        self.assertEqual(self.graph.find_tests_for(symbol_id=self.target.id, include_candidates=True), actual)
        self.test.write_text("def test_issued():\n    assert True\n", encoding="utf-8")
        self.assertFalse(self.graph.find_tests_for(symbol_id=self.target.id)[0].verified_current)
        self.graph.compile_project(self.root)
        self.assertEqual(self.graph.find_tests_for(symbol_id=self.target.id), [])

    def test_query_context_includes_all_ranked_test_files_within_request(self) -> None:
        """Carry more than four verified associations into the response and its text."""
        expected = {"tests/test_invoice.py"}
        for index in range(5):
            path = f"tests/test_invoice_{index}.py"
            (self.root / path).write_text(
                f"from invoice import issued\n\ndef test_issued_{index}():\n    assert issued() == 42\n",
                encoding="utf-8",
            )
            expected.add(path)
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        service = self.graph.query_context_service
        result = service.execute(QueryContextRequest.from_raw(text="issued", scopes=["code"], max_items=10))
        self.assertEqual({row["path"] for row in result.payload["test_targets"] if row["evidence_kind"] == "static"},
                         expected)
        rendered = service.render(result)
        self.assertTrue(all(path in rendered for path in expected))

    def test_observations_deduplicate_and_follow_both_source_hashes(self) -> None:
        service = self.graph.test_intelligence
        self.assertEqual(service.import_coverage(self.report(per_test=False), project_path=self.root)["identified"], 0)
        report = self.report()
        self.assertEqual(service.import_coverage(report, project_path=self.root)["imported"], 1)
        self.assertEqual(service.import_coverage(report, project_path=self.root)["imported"], 0)
        found = self.graph.find_tests_for(symbol_id=self.target.id)
        self.assertEqual([row.evidence_kind for row in found], ["observed", "static"])
        self.assertTrue(found[0].verified_current)
        (self.root / "independent.py").write_text("value = 3\n", encoding="utf-8")
        self.graph.compile_project(self.root)
        self.assertTrue(self.graph.find_tests_for(symbol_id=self.target.id)[0].verified_current)
        self.module.unlink()
        self.graph.compile_project(self.root)
        found = self.graph.find_tests_for(symbol_id=self.target.id)
        self.assertEqual([row.evidence_kind for row in found], ["observed"])
        self.assertFalse(found[0].verified_current)
        self.assertEqual(service.status()["stale_or_unverified"], 1)

    def test_confinement_and_mcp_contract(self) -> None:
        outside = self.parent / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "within"):
            self.graph.test_intelligence.import_coverage(outside, project_path=self.root)
        report = self.report()
        expected = [asdict(row) for row in self.graph.find_tests_for(symbol_id=self.target.id)]
        self.graph.close()
        with patch.dict(os.environ, {"REQL_MCP_ALLOWED_ROOTS": str(self.root)}):
            actual = reql_tests_for(storage_path=str(self.storage), symbol_id=self.target.id)
            self.assertEqual(actual["associations"], expected)
            self.assertNotIn("reql_tests_import", {row["name"] for row in list_tools(include_write=False)})
            with self.assertRaisesRegex(MCPToolError, "confirm"):
                reql_tests_import(storage_path=str(self.storage), path=str(self.root), input=str(report))
            self.assertEqual(call_tool("reql_tests_import", {
                "storage_path": str(self.storage), "path": str(self.root), "input": str(report), "confirm": True,
            })["imported"], 1)

    def test_configured_directory_regex_reaches_every_consumer(self) -> None:
        """Compile nested test files, recommend them and resolve their coverage IDs."""
        source = self.root / "gateway.py"
        source.write_text("def receive():\n    return 1\n", encoding="utf-8")
        test_source = "from gateway import receive\ndef testReceive():\n    assert receive()\n"
        static_path = "quality/Cases-42/nested/sample.py"
        static = self.root / static_path
        static.parent.mkdir(parents=True)
        static.write_text(test_source, encoding="utf-8")
        candidate_path = "quality/Cases-42/gateway.py"
        (self.root / candidate_path).write_text("def test_other():\n    assert True\n", encoding="utf-8")
        for path in ("other/quality/Cases-42/sample.py", "quality/Cases-42-extra/sample.py", "quality/Cases-42.py"):
            decoy = self.root / path
            decoy.parent.mkdir(parents=True, exist_ok=True)
            decoy.write_text(test_source, encoding="utf-8")
        config_path = self.root / "reql.conf"
        config_path.write_text(
            'testing:\n  discovery:\n    directories: ["quality/cases-[0-9]+(?:[.]py)?"]\n  suggestions:\n'
            '    evidence_order: ["static", "observed", "candidate"]\n', encoding="utf-8",
        )
        self.graph.close()
        self.graph = MemoryGraph.open(self.storage, config=load_effective_config(start_dir=self.root))
        self.addCleanup(self.graph.close)
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        source.write_text("def receive():\n    return 2\n", encoding="utf-8")
        result = self.graph.compile_project(self.root)
        self.assertEqual({row.path: row.reason for row in result.summary.associated_tests}, {
            candidate_path: "matching test filename",
            static_path: "direct graph relationship",
        })
        findings = self.graph.find_tests_for(file="gateway.py", include_candidates=True)
        self.assertEqual([(row.test_path, row.evidence_kind) for row in findings], [
            (static_path, "static"), (candidate_path, "candidate"),
        ])
        test_context = self.graph.query_context_result(QueryContextRequest.from_raw(
            text="testReceive", scopes=["test"], include_source=True,
        ))
        self.assertIn(static_path, {row["path"] for row in test_context.payload["snippets"]})
        report = self.root / "coverage.json"
        named_context = "runner case 42"
        names = [
            r"quality\Cases-42\nested\sample.py::testReceive[param]|run",
            f"{static}::testReceive[a|b]|run",
            named_context,
        ]
        report.write_text(json.dumps({
            "files": {"gateway.py": {"executed_lines": [2], "contexts": {"2": names}}},
            "reql": {
                "test_contexts": {named_context: {"path": static_path, "name": "testReceive"}},
                "source_hashes": {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in (source, static)},
            },
        }), encoding="utf-8")
        self.assertEqual(self.graph.test_intelligence.import_coverage(report, project_path=self.root)["imported"], 3)
        found = self.graph.find_tests_for(file="gateway.py")
        self.assertEqual([row.evidence_kind for row in found], ["static", "observed", "observed", "observed"])
        self.assertTrue(all(row.verified_current for row in found))

        config_path.write_text("testing:\n  discovery:\n    directories: []\n", encoding="utf-8")
        self.graph.close()
        with closing(MemoryGraph.open(self.storage, config=load_effective_config(start_dir=self.root))) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            rows = graph.find_tests_for(file="gateway.py", include_candidates=True)
            self.assertEqual([(row.evidence_kind, row.verified_current) for row in rows],
                             [("observed", False)] * 3)
            artifact = next(node for node in graph.store.find_nodes_by_property("relative_path", static_path)
                            if node.type == "SourceArtifact")
            self.assertEqual(artifact.properties.get("context_scope"), "code")

    def test_stats_reports_imported_evidence_freshness(self) -> None:
        """Show verified and stale coverage observations through the CLI."""
        self.assertEqual(self.graph.test_intelligence.import_coverage(self.report(), project_path=self.root)["imported"], 1)
        self.graph.close()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["stats", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["tests"], {
            "schema_version": 1, "observed": 1, "verified_current": 1, "stale_or_unverified": 0,
        })
        self.module.write_text("def issued():\n    return 43\n", encoding="utf-8")
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["stats", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["tests"], {
            "schema_version": 1, "observed": 1, "verified_current": 0, "stale_or_unverified": 1,
        })

    def test_parallel_imports_share_one_transactional_observation(self) -> None:
        report = self.report()
        self.graph.close()

        def run() -> int:
            graph = MemoryGraph.open(self.storage, read_only=True, defer_lexical_index=True)
            try:
                return graph.test_intelligence.import_coverage(report, project_path=self.root)["imported"]
            finally:
                graph.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: run(), range(2))), [0, 1])
        graph = MemoryGraph.open(self.storage, read_only=True)
        try:
            self.assertEqual(graph.test_intelligence.status()["verified_current"], 1)
        finally:
            graph.close()
