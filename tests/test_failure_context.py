from pathlib import Path
import tempfile
import unittest

from memory.services.coordination import CoordinationStore
from reql import MemoryGraph, QueryContextRequest


class FailureContextTests(unittest.TestCase):
    def test_file_causality_replacement_and_source_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "serializer.py"
            source.write_text("def encode(value):\n    return str(value)\n", encoding="utf-8")
            storage = root / ".reql/memory.reql"
            work = CoordinationStore(storage.with_name("agent-dashboard.reql"))
            metadata = {"agent_id": "agent:test", "session_id": None}
            graph = MemoryGraph.open(storage)
            try:
                revision = graph.compile_project(root).revision
                symbol = next(node for node in graph.store.find_nodes_by_property("name", "encode") if node.type == "Function")
                task = work.put("task", "Refactor serializer", **metadata)["node"]
                previous = work.put("failure", "Original approach", rationale="Lost field",
                                    files=["serializer.py"], **metadata)["node"]
                replacement = work.put(
                    "failure", "Numeric conversion discarded leading zeroes", rationale="Round-trip check failed",
                    files=["serializer.py"], parent=task["id"], supersedes=[previous["id"]],
                    failure_evidence={"source_revision": revision.id, "symbol_ids": [symbol.id, "invalid"],
                                      "observed_checks": ["local round-trip"]}, **metadata,
                )["node"]
                work.put("failure", "Broken mail handshake", rationale="Incorrect socket", files=["mail.py"], **metadata)
                work.put("failure", "Nonrelevant approach", rationale="Extra allocation", **metadata)
                before = work.records()
                response = graph.failure_context("Refactor serializer", files=["serializer.py"])
                self.assertEqual([row["id"] for row in response["records"]], [replacement["id"]])
                row = response["records"][0]
                self.assertEqual(row["freshness"], "current")
                self.assertIn("file match serializer.py", row["reasons"])
                self.assertEqual(row["failure_evidence"]["symbol_ids"], [symbol.id])
                self.assertEqual(graph.failure_context(record_id=task["id"])["records"][0]["id"], replacement["id"])
                self.assertEqual(graph.failure_context("totally unrelated")["records"], [])
                context = graph.query_context_result(QueryContextRequest.from_raw(text="encode serializer", scopes=["code"]))
                failure = context.payload["failure_context"]["records"][0]
                self.assertEqual(failure["engineering_context_ref"], replacement["id"])
                self.assertNotIn("content", failure)
                source.write_text("def encode(value):\n    return repr(value)\n", encoding="utf-8")
                changed = graph.failure_context(files=["serializer.py"])["records"][0]
                self.assertEqual(changed["freshness"], "potentially_obsolete")
                self.assertEqual(changed["status"], "active")
                self.assertEqual(work.records(), before)
            finally:
                graph.close()
