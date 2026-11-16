from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
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
        self.assertEqual(context.schema_version, 2)
        self.assertIn("static", {row.get("evidence_kind") for row in context.payload["test_targets"]})
        self.assertEqual(self.graph.compile_project(self.root).run.files_changed, 0)
        self.assertEqual(self.graph.find_tests_for(symbol_id=self.target.id, include_candidates=True), actual)
        self.test.write_text("def test_issued():\n    assert True\n", encoding="utf-8")
        self.assertFalse(self.graph.find_tests_for(symbol_id=self.target.id)[0].verified_current)
        self.graph.compile_project(self.root)
        self.assertEqual(self.graph.find_tests_for(symbol_id=self.target.id), [])

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
