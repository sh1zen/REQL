from dataclasses import asdict
from importlib import import_module
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import reql
from api import MemoryGraph as APIMemoryGraph
from mcp.tools import call_tool, reql_tests_for, reql_work_context
from memory import config, query
from memory.services.coordination import CoordinationStore
from reql import MemoryGraph, QueryContextRequest


class ReleaseContractTests(unittest.TestCase):
    def test_standalone_module_preserves_public_imports(self) -> None:
        """Check that the module exposes the existing API and import aliases."""
        self.assertEqual(Path(reql.__file__).name, "reql.py")
        self.assertIs(MemoryGraph, APIMemoryGraph)
        self.assertIs(import_module("reql.config"), config)
        self.assertIs(import_module("reql.query"), query)

    def test_cli_and_mcp_match_the_python_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "handler.py").write_text("def dispatch():\n    return 42\n", encoding="utf-8")
            (root / "test_handler.py").write_text(
                "from handler import dispatch\n\ndef test_dispatch():\n    assert dispatch() == 42\n", encoding="utf-8",
            )
            storage = root / ".reql/memory.reql"
            work = CoordinationStore(storage.with_name("agent-dashboard.reql"))
            work.put("failure", "Dispatch caching introduced stale results", agent_id="agent:test", session_id=None,
                     rationale="A previous response remained visible", files=["handler.py"])
            graph = MemoryGraph.open(storage)
            try:
                self.assertFalse(graph.compile_project(root).run.errors)
                expected_tests = [asdict(item) for item in graph.find_tests_for(file="handler.py")]
                expected_failures = graph.failure_context(files=["handler.py"], limit=8)
                request = QueryContextRequest.from_raw(text="dispatch", scopes=["code"], include_source=True)
                expected_context = graph.query_context_result(request).to_dict()
            finally:
                graph.close()

            self.assertTrue(any(
                row["path"] == "handler.py" and row["source_status"] == "verified" and "return 42" in row["text"]
                for row in expected_context["payload"]["snippets"]
            ))
            cli = Path(__file__).resolve().parents[1] / "cli.py"

            def command(*args: str):
                output = subprocess.run([sys.executable, str(cli), *args, "--json"], cwd=root, check=True, capture_output=True)
                return json.loads(output.stdout)

            self.assertEqual(command("tests", "for", "--file", "handler.py")["associations"], expected_tests)
            self.assertEqual(command("project", "context", "--file", "handler.py", "--failures-only"), expected_failures)
            with patch.dict(os.environ, {"REQL_MCP_ALLOWED_ROOTS": str(root)}):
                self.assertEqual(reql_tests_for(storage_path=str(storage), file="handler.py")["associations"], expected_tests)
                self.assertEqual(reql_work_context(storage_path=str(storage), files=["handler.py"], kinds=["failure"]),
                                 expected_failures)
                mcp_context = call_tool("query_context", {
                    "storage_path": str(storage), "query": "dispatch", "code": True, "include_source": True,
                })
            cli_context = command("query_context", "--query", "dispatch", "--code", "--source")
            self.assertEqual(expected_context["schema_version"], 2)
            for result in (cli_context, mcp_context, expected_context):
                self.assertIn("trace_id", result["payload"])
                del result["payload"]["trace_id"]
            self.assertEqual(cli_context, expected_context)
            self.assertEqual(mcp_context, expected_context)
