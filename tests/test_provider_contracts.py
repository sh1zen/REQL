from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp.tools import query_context, reql_tests_for, reql_work_context
from memory.services.coordination import CoordinationStore
from reql import MemoryGraph, QueryContextRequest


class ReleaseContractTests(unittest.TestCase):
    def test_cli_rejects_retired_query_graph_command(self) -> None:
        """Reject the retired command before opening a graph."""
        cli = Path(__file__).resolve().parents[1] / "cli.py"
        result = subprocess.run(
            [sys.executable, str(cli), "query_graph", "--help"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice: 'query_graph'", result.stderr)

    def test_cli_and_mcp_match_the_python_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "handler.py").write_text("def dispatch():\n    return 'ok'\n", encoding="utf-8")
            (root / "test_handler.py").write_text(
                "from handler import dispatch\n\ndef test_dispatch():\n    assert dispatch() == 'ok'\n", encoding="utf-8",
            )
            storage = root / ".reql/memory.reql"
            work = CoordinationStore(storage.with_name("agent-dashboard.reql"))
            work.put("failure", "Dispatch caching introduced stale results", agent_id="agent:test", session_id=None,
                     rationale="A previous response remained visible", files=["handler.py"])
            graph = MemoryGraph.open(storage)
            try:
                graph.compile_project(root)
                expected_tests = [asdict(item) for item in graph.find_tests_for(file="handler.py")]
                expected_failures = graph.failure_context(files=["handler.py"], limit=8)
                request = QueryContextRequest.from_raw(text="dispatch", scopes=["code"], include_source=True)
                expected_context = graph.query_context_result(request).to_dict()
            finally:
                graph.close()

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
                mcp_context = query_context(storage_path=str(storage), query="dispatch", code=True, include_source=True)
            cli_context = command("query_context", "--query", "dispatch", "--code", "--source")
            for result in (cli_context, mcp_context, expected_context):
                self.assertEqual(result["schema_version"], 2)
                result["payload"].pop("trace_id", None)
            self.assertEqual(cli_context, expected_context)
            self.assertEqual(mcp_context, expected_context)
