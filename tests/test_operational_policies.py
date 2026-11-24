"""Run storage, source, and coordination policy through their public entry points."""
from __future__ import annotations

from codecs import BOM_UTF16_BE
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import random
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from mcp.tools import call_tool
from memory.agent import AgentWorkspace
from memory.cli import main
from memory.config import default_config, load_effective_config, merge_overrides
from memory.domain.exceptions import StorageError
from memory.domain.models import MemoryEdge, MemoryNode
from memory.storage import exclusive_store_lock
from memory.storage.adapters import store_lock
from memory.testing.coverage_import import read_coverage_json
from reql import MemoryGraph


class OperationalPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.storage = self.root / ".reql" / "memory.reql"
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.root)

    def test_failed_checkpoint_publication_preserves_wal_and_reopens_multipart_records(self) -> None:
        config = merge_overrides(default_config(), {
            "storage.block_size_bytes": 4096,
            "storage.page_cache_blocks": 2,
            "storage.dense_node_threshold": 1,
            "storage.checkpoint_wal_bytes": 0,
        })
        label = "verified lexical storage"
        body = random.Random(17).randbytes(20_000).hex()
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            before = self.storage.read_bytes()
            graph.store.upsert_node(MemoryNode(id="large", type="Concept", text=body, label=label))
            graph.store.upsert_node(MemoryNode(id="small", type="Concept", label="neighbor"))
            graph.store.upsert_edge(MemoryEdge(id="edge", from_id="large", to_id="small", type="RELATED"))
            denied = PermissionError("checkpoint opened by an external process")
            denied.winerror = 32
            with patch.object(Path, "replace", autospec=True, side_effect=denied) as replace:
                with self.assertRaisesRegex(StorageError, "Cannot replace REQL checkpoint") as error:
                    graph.store.checkpoint_if_needed()
            replace.assert_called_once()
            self.assertIs(error.exception.__cause__, denied)
            self.assertEqual(self.storage.read_bytes(), before)
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertEqual(graph.store.get_node("large").text, body)
            checkpoint = graph.store.checkpoint_if_needed()
            self.assertTrue(checkpoint["checkpointed"])
            self.assertEqual(checkpoint["threshold"], 0)
            graph.store.compact_storage()
            self.assertEqual(graph.store.inspect_storage()["dense_nodes"]["threshold"], 1)
        with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
            self.assertEqual(graph.store.inspect_storage()["block_size"], 4096)
            self.assertEqual(graph.store.get_node("large").text, body)
            self.assertEqual(graph.store.lexical_search(label)[0][0].id, "large")

    def test_cli_edits_large_files_and_plans_and_preserves_utf16(self) -> None:
        source = self.root / "large.py"
        source.write_bytes(
            b"def large_source():\n    return 1\n" + b"\n" * 40 + b" " * (11 * 1024 * 1024) + b"\n"
        )
        utf16 = self.root / "wide.py"
        utf16.write_bytes(BOM_UTF16_BE + "def wide():\r\n    return 1\r\n".encode("utf-16-be"))
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["project", "compile"]), 0)
        plan = [{"path": "large.py", "start_line": 2, "end_line": 2, "text": "    return 2"}]
        plan.extend(
            {"path": "large.py", "start_line": line, "end_line": line, "text": " " * (200_000 if line == 4 else 1)}
            for line in range(4, 37)
        )
        plan.append({"path": "wide.py", "start_line": 2, "end_line": 2, "text": "    return 3"})
        document = json.dumps(plan)
        self.assertGreater(len(document), 192 * 1024)
        with patch("memory.cli.sys.stdin", StringIO(document)), redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["edit", "--plan", "-", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["changed"], ["large.py", "wide.py"])
        self.assertGreater(source.stat().st_size, 10 * 1024 * 1024)
        self.assertEqual(utf16.read_bytes(), BOM_UTF16_BE + "def wide():\r\n    return 3\r\n".encode("utf-16-be"))
        with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
            snippets = graph.query_context_payload("large_source", scopes=["code"], include_source=True)["payload"]["snippets"]
            self.assertTrue(any(row["source_status"] == "verified" and "return 2" in row["text"] for row in snippets))

    def test_coverage_reader_accepts_a_large_project_local_report(self) -> None:
        path = self.root / "coverage.json"
        path.write_bytes(b'{"files":{}}' + b" " * (32 * 1024 * 1024))
        data, digest = read_coverage_json(self.root, path)
        self.assertEqual(data, {"files": {}})
        self.assertEqual(len(digest), 64)

    def test_workspace_paths_and_complete_coordination_are_shared_across_providers(self) -> None:
        (self.root / "reql.conf").write_text(
            "workspace:\n  default_agent_id: lead\n  private_store_name: lead.reql\n"
            "  dashboard_store_name: work.reql\n  agents_dir: workers\n", encoding="utf-8",
        )
        config = load_effective_config(start_dir=self.root, env={})
        paths = [f"src/file{index}.py" for index in range(80)]
        env = {"REQL_AGENT_ACTIVITY_ID": "", "CODEX_THREAD_ID": "", "REQL_MCP_ALLOWED_ROOTS": str(self.root),
               "REQL_CONFIG": str(self.root / "reql.conf"), "REQL_CONFIG_OVERRIDES": ""}
        with patch.dict(os.environ, env):
            lead = AgentWorkspace(self.storage, config=config)
            self.assertEqual(lead.agent_id, "lead")
            self.assertEqual(lead.paths.agent_storage.name, "lead.reql")
            self.assertEqual(lead.paths.dashboard_storage.name, "work.reql")
            lead.init("lead session")
            worker = AgentWorkspace(self.storage, agent_id="worker", config=config)
            worker.init("worker session")
            self.assertEqual(worker.paths.agent_storage.parent.name, "workers")
            self.assertEqual(len(lead.public_dashboard(limit=1)["agents"]), 1)
            self.assertEqual({row["agent_id"] for row in AgentWorkspace.list_registered_agents(self.storage, config=config)},
                             {"lead", "worker"})
            work = lead.coordination
            task = work.put("task", "Retain evidence paths", agent_id="lead", session_id="s", files=paths)["node"]
            for value in ("second", "third"):
                task = work.put("task", value, agent_id="lead", session_id="s", record_id=task["id"],
                                expected_revision=task["revision"])["node"]
            self.assertEqual(len(work.show(task["id"])["history"]), 2)
            self.assertEqual(work.show(task["id"])["files"], paths)
            dependencies = [work.put("observation", f"Dependency {index}", agent_id="lead", session_id="s")["node"]["id"]
                            for index in range(44)]
            work.put(record_id=task["id"], expected_revision=task["revision"], depends_on=dependencies,
                     agent_id="lead", session_id="s")
            with redirect_stdout(StringIO()) as result:
                self.assertEqual(main(["project", "context", "--task", task["id"], "--json"]), 0)
            cli = json.loads(result.getvalue())
            self.assertEqual(cli["records"][0]["files"], paths)
            self.assertEqual({row["id"] for row in cli["records"][1:]}, set(dependencies))
            payload = call_tool("reql_work_context", {"storage_path": str(self.storage), "record_id": task["id"]})
            self.assertEqual(payload, cli)
            with self.assertRaisesRegex(ValueError, "positive integer"):
                work.context(limit=0)

    def test_dashboard_reads_and_work_writes_resume_after_lock_release(self) -> None:
        """Wait on the same native lease for dashboard and coordination access."""
        workspace = AgentWorkspace(self.storage, agent_id="agent:reader", activity_id="read")
        session = workspace.init("Read dashboard")["session"]
        acquire = store_lock._acquire
        work = workspace.coordination
        for shared, operation in (
            (True, lambda: workspace.public_dashboard()["agents"][0]["agent_id"]),
            (False, lambda: work.put(
                "observation", "Write after release", agent_id=workspace.agent_id, session_id=session["id"],
            )["node"]["content"]),
        ):
            with self.subTest(shared=shared):
                waiting = Event()

                def observe(fd: int, *, shared: bool, blocking: bool) -> bool:
                    if blocking:
                        waiting.set()
                    return acquire(fd, shared=shared, blocking=blocking)

                with ThreadPoolExecutor(max_workers=1) as worker:
                    with exclusive_store_lock(workspace.paths.dashboard_storage), patch.object(store_lock, "_acquire", observe):
                        pending = worker.submit(operation)
                        self.assertTrue(waiting.wait(10))
                        self.assertFalse(pending.done())
                    expected = "agent:reader" if shared else "Write after release"
                    self.assertEqual(pending.result(timeout=10), expected)

    def test_workspace_rejects_a_private_store_at_its_lifecycle_lease_path(self) -> None:
        """Keep independently acquired leases from sharing one lock identity."""
        dashboard = self.storage.with_name("work.reql")
        with self.assertRaisesRegex(ValueError, "stores and leases must use distinct paths"):
            AgentWorkspace(self.storage, agent_id="agent:reader", dashboard_storage=dashboard,
                           agent_storage=dashboard.with_name("work.reql.lifecycle"))

    def test_dashboard_accepts_an_explicit_row_count_above_twenty(self) -> None:
        """Keep the default compact and return the requested dashboard rows."""
        workspace = AgentWorkspace(self.storage, agent_id="agent:notes", activity_id="notes")
        workspace.init("Record progress")
        for index in range(21):
            workspace.publish_note(f"Progress update {index}")
        self.assertEqual(len(workspace.public_dashboard()["context"]), 5)
        self.assertEqual(len(workspace.dashboard(limit=21)["public"]["context"]), 21)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            workspace.dashboard(limit=0)


if __name__ == "__main__":
    unittest.main()
