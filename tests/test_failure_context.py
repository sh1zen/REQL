from pathlib import Path
import tempfile
import unittest

from mcp.tools import reql_work_context
from memory.services.coordination import CoordinationStore
from memory.services.failure_context import FailureContextService, render_failure_context
from reql import MemoryGraph, QueryContextRequest


class FailureContextTests(unittest.TestCase):
    def test_recovery_returns_every_matching_failure_with_complete_evidence(self) -> None:
        scratch = Path.cwd() / ".tmp" / "regression-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            storage = Path(directory) / ".reql" / "memory.reql"
            work = CoordinationStore.for_project(storage)
            files = [f"src/serializer_{index}.py" for index in range(12)]
            checks = [f"Check {index} " + "expected behavior " * 12 for index in range(20)]
            rationale = "Evidence from the recorded failure. " * 150 + "End of failure evidence."
            failures = [work.put("failure", f"Serializer failed round trip {index}", rationale=rationale,
                                 files=files, failure_evidence={"observed_checks": checks},
                                 agent_id="agent:test", session_id=None)["node"] for index in range(8)]
            service = FailureContextService(work)
            payload = service.context(files=files)
            self.assertEqual({row["id"] for row in payload["records"]}, {row["id"] for row in failures})
            self.assertEqual(payload["omitted"], 0)
            self.assertTrue(all(row["rationale"] == rationale and row["files"] == files for row in payload["records"]))
            self.assertEqual(work.show(failures[0]["id"])["failure_evidence"]["observed_checks"], checks)
            self.assertEqual(reql_work_context(storage_path=str(storage), files=files, kinds=["failure"]), payload)
            self.assertEqual(render_failure_context(payload).count(rationale), len(failures))
            self.assertEqual(service.context(files=files, limit=1)["omitted"], len(failures) - 1)

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
