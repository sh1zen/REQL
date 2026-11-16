"""Real persisted, multi-session engineering workflows through supported APIs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from api import MemoryGraph
from memory.agent import AgentWorkspace
from memory.cli import main
from memory.config import default_config, merge_config
from memory.domain.models import MemoryNode
from memory.services.coordination import CoordinationStore, MAX_REVISIONS, render_work_context
from mcp.tools import call_tool, list_tools, query_context as mcp_context


class CoordinationWorkflowTests(unittest.TestCase):
    """Validate continuation, parallelism, changed plans, focused context and overview."""

    def setUp(self) -> None:
        scratch = Path(".tmp-test")
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = self.root / ".reql" / "memory.reql"
        self.work = CoordinationStore(self.storage.with_name("agent-dashboard.reql"))

    def agent(self, name: str) -> AgentWorkspace:
        """Start a separate identity/activity without shared private selection."""
        agent = AgentWorkspace(self.storage, agent_id=f"agent:{name}", activity_id=name)
        agent.init(name)
        return agent

    def update(self, agent: AgentWorkspace, item: dict, **fields: object) -> dict:
        """Reconcile an observed revision, as a resumed coding agent would."""
        return agent.record(item["kind"], item["content"], record_id=item["id"],
                            expected_revision=item["revision"], **fields)["node"]

    def _claim_cli(self, task_id: str, query: str | None = None, *, as_json: bool = True) -> tuple[int, str]:
        """Run the public assignment command from the fixture project."""
        previous = Path.cwd()
        output = StringIO()
        try:
            os.chdir(self.root)
            with redirect_stdout(output), redirect_stderr(StringIO()):
                args = ["agent", "--agent", "agent:worker", "--activity", "worker", "init", "--task", task_id]
                if query is not None:
                    args.append(f"--query={query}")
                code = main([*args, "--json"] if as_json else args)
        finally:
            os.chdir(previous)
        return code, output.getvalue()

    def test_next_prefers_disjoint_work_then_reports_an_overlap(self) -> None:
        """Keep assignments flowing and report shared files without turning them into locks."""
        author = {"agent_id": "agent:planner", "session_id": "session:plan"}
        active = self.work.put("task", "Existing checkout edit", status="in_progress",
                               files=["shared.py"], **author)["node"]
        shared = self.work.put("task", "Follow-on shared edit", files=["shared.py"],
                               importance=3, workstream="changes", **author)["node"]
        free = self.work.put("task", "Independent edit", files=["free.py"],
                             importance=1, workstream="changes", **author)["node"]
        blocked = self.work.put("task", "Run after the independent edit", depends_on=[free["id"]],
                                importance=3, workstream="changes", **author)["node"]
        worker = {"agent_id": "agent:a", "session_id": "session:a", "workstream": "changes"}
        first = self.work.claim_next(**worker)
        self.assertEqual(first["claimed_task"]["id"], free["id"])
        again = self.work.claim_next(**worker)
        self.assertEqual(again["status"], "resumed")
        self.assertEqual(again["claimed_task"], first["claimed_task"])

        args = {"storage_path": str(self.storage), "agent_id": "agent:b", "session_id": "session:b",
                "workstream": "changes"}
        self.assertNotIn("reql_work_claim_next", {tool["name"] for tool in list_tools(include_write=False)})
        second = call_tool("reql_work_claim_next", args)
        self.assertEqual(second["claimed_task"]["id"], shared["id"])
        self.assertEqual(second["file_overlaps"]["files"],
                         [{"path": "shared.py", "tasks": [active["id"]], "omitted": 0}])
        waiting = self.work.claim_next(agent_id="agent:c", session_id="session:c", workstream="changes")
        self.assertEqual(waiting["status"], "blocked")
        self.assertEqual(waiting["blockers"][0]["id"], blocked["id"])
        self.assertEqual(waiting["blockers"][0]["waiting_on"], [free["id"]])

    def test_concurrent_next_claims_distinct_tasks_and_retries_are_idempotent(self) -> None:
        """Share the on-disk queue between independently initialized agent activities."""
        planner = self.agent("planner")
        tasks = {planner.add_task(f"Implement independent change {index}")["node"]["id"] for index in range(2)}
        barrier = Barrier(2, timeout=10)

        def claim(index: int) -> tuple[dict, dict]:
            workspace = AgentWorkspace(self.storage, agent_id=f"agent:worker{index}", activity_id=f"worker{index}")
            barrier.wait()
            first = workspace.init(next_task=True)
            return first, workspace.init(next_task=True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(claim, range(2)))
        self.assertEqual({first["claimed_task"]["id"] for first, _ in outcomes}, tasks)
        for first, resumed in outcomes:
            self.assertEqual(first["assignment_status"], "claimed")
            self.assertEqual(resumed["assignment_status"], "resumed")
            self.assertEqual(first["claimed_task"]["revision"], resumed["claimed_task"]["revision"])

    def test_cli_processes_share_the_queue_without_duplicate_claims(self) -> None:
        """Claim and resume work from two CLI processes sharing on-disk state."""
        planner = self.agent("planner")
        tasks = {}
        for symbol in ("calculate_total", "parse_invoice"):
            path = f"{symbol}.py"
            (self.root / path).write_text(f"def {symbol}():\n    return 1\n", encoding="utf-8")
            tasks[planner.add_task(symbol, files=[path])["node"]["id"]] = path
        graph = MemoryGraph.open(self.storage)
        try:
            self.assertFalse(graph.compile_project(self.root).run.errors)
        finally:
            graph.close()

        launcher = Path(__file__).resolve().parents[1] / "cli.py"
        barrier = Barrier(2, timeout=10)

        def worker(index: int) -> tuple[dict, dict]:
            command = [sys.executable, str(launcher), "agent", "--agent", f"agent:worker{index}",
                       "--activity", f"worker{index}", "init", "--next", "--json"]
            barrier.wait()
            outcomes = []
            for _ in range(2):
                result = subprocess.run(command, cwd=self.root, capture_output=True, encoding="utf-8", timeout=40)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                outcomes.append(json.loads(result.stdout))
            return outcomes[0], outcomes[1]

        with ThreadPoolExecutor(max_workers=2) as executor:
            workers = list(executor.map(worker, range(2)))
        self.assertEqual({claimed["claimed_task"]["id"] for claimed, _ in workers}, tasks.keys())
        for claimed, resumed in workers:
            task = claimed["claimed_task"]
            source = claimed["source_context"]["payload"]
            self.assertIn(tasks[task["id"]], {row["path"] for row in source["snippets"]})
            self.assertEqual(claimed["assignment_status"], "claimed")
            self.assertEqual(resumed["assignment_status"], "resumed")
            self.assertEqual(resumed["claimed_task"], task)
            self.assertEqual(claimed["file_overlaps"]["files"], [])

    def test_explicit_claim_reports_overlaps_without_a_source_lookup(self) -> None:
        """Return shared evidence and file scopes even when the graph has not been built."""
        planner = self.agent("planner")
        active = planner.add_task("Existing edit", status="in_progress", files=["shared.py"])["node"]
        pending = planner.add_task("Follow-up edit", files=["shared.py"], next_action="Check affected callers")["node"]

        code, output = self._claim_cli(pending["id"])
        self.assertEqual(code, 0, output)
        result = json.loads(output)
        self.assertEqual(result["claimed_task"]["revision"], 2)
        self.assertEqual(result["work_context"]["records"][0]["next_action"], "Check affected callers")
        self.assertEqual(result["file_overlaps"]["files"],
                         [{"path": "shared.py", "tasks": [active["id"]], "omitted": 0}])

        code, output = self._claim_cli(pending["id"], as_json=False)
        self.assertEqual(code, 0, output)
        self.assertIn(f"Resumed task: {pending['id']}\tr2", output)
        self.assertIn("Coordinate edits with their owners", output)
        self.assertIn(active["id"], output)

    def test_next_cli_returns_scoped_code_in_the_claim_response(self) -> None:
        """Use the saved title to recover source without another discovery command."""
        (self.root / "invoice.py").write_text("def calculate_invoice_total():\n    return 7\n", encoding="utf-8")
        graph = MemoryGraph.open(self.storage)
        try:
            self.assertFalse(graph.compile_project(self.root).run.errors)
        finally:
            graph.close()
        task = self.agent("planner").add_task("calculate_invoice_total", files=["invoice.py"])["node"]
        args = ["agent", "--agent", "agent:worker", "--activity", "next", "init", "--next"]
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(args), 0)
            text = output.getvalue()
        finally:
            os.chdir(previous)
        self.assertIn(f"Claimed task: {task['id']}\tr2", text)
        self.assertEqual(text.count("## Engineering context"), 1)
        self.assertIn("return 7", text)

    def test_next_cli_recovers_a_claim_when_the_graph_is_unavailable(self) -> None:
        """A source failure leaves the new claim usable by ID and revision."""
        task = self.agent("planner").add_task("Update invoice totals", files=["invoice.py"])["node"]
        args = ["agent", "--agent", "agent:worker", "--activity", "next", "init", "--next", "--json"]
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(args), 2)
            result = json.loads(output.getvalue())
        finally:
            os.chdir(previous)
        self.assertEqual(result["claimed_task"]["revision"], 2)
        self.assertEqual(self.work.show(task["id"])["status"], "in_progress")
        self.assertTrue(result["source_context_retry"].startswith(f"reql query_context --task {task['id']} "))
        self.assertEqual(result["work_context"]["records"][0]["id"], task["id"])

    def test_claim_with_source_recovers_work_once_and_preserves_revision_guards(self) -> None:
        """Claim through the public CLI and recover linked failure and current source together."""
        planner = self.agent("planner")
        failure = planner.record("failure", "Avoid truncating fractional totals",
                                 rationale="Fractional amounts must survive calculation", files=["invoice.py"])["node"]
        task = planner.add_task("Keep the invoice total precise", files=["invoice.py"],
                                depends_on=[failure["id"]])["node"]
        code, missing = self._claim_cli(task["id"], "calculate_invoice_total")
        self.assertEqual(code, 2)
        self.assertIn("indexed project", json.loads(missing)["error"]["message"])
        self.assertEqual(self.work.show(task["id"])["revision"], task["revision"])

        (self.root / "invoice.py").write_text("def calculate_invoice_total(amounts):\n    return sum(amounts)\n",
                                              encoding="utf-8")
        graph = MemoryGraph.open(self.storage)
        try:
            self.assertFalse(graph.compile_project(self.root).run.errors)
        finally:
            graph.close()
        show = CoordinationStore.show

        def changed_after_read(store: CoordinationStore, record_id: str) -> dict:
            snapshot = show(store, record_id)
            self.update(planner, snapshot, next_action="Check fractional totals")
            return snapshot

        with patch.object(CoordinationStore, "show", changed_after_read):
            code, rejected = self._claim_cli(task["id"], "calculate_invoice_total")
        self.assertEqual(code, 2)
        self.assertIn("revision changed", json.loads(rejected)["error"]["message"])
        current = self.work.show(task["id"])
        self.assertEqual(current["next_action"], "Check fractional totals")

        code, output = self._claim_cli(task["id"], "calculate_invoice_total")
        self.assertEqual(code, 0, output)
        result = json.loads(output)
        claimed = result["claimed_task"]
        self.assertEqual(claimed["revision"], current["revision"] + 1)
        self.assertEqual(claimed["agent_id"], "agent:worker")
        self.assertNotIn("work_context", result)
        source = result["source_context"]["payload"]
        self.assertIn(failure["id"], {row["id"] for row in source["engineering_context"]["records"]})
        self.assertIn("return sum(amounts)", source["snippets"][0]["text"])
        code, repeated = self._claim_cli(task["id"], "calculate_invoice_total")
        self.assertEqual(code, 0, repeated)
        self.assertEqual(json.loads(repeated)["claimed_task"], claimed)
        self.assertEqual(self.work.show(task["id"])["revision"], claimed["revision"])
        code, text = self._claim_cli(task["id"], "calculate_invoice_total", as_json=False)
        self.assertEqual(code, 0, text)
        self.assertEqual(text.count("## Engineering context"), 1)
        self.assertIn("Saved task and direct prerequisites shown in full.", text)
        self.assertEqual(text.count(failure["id"]), 2)  # Dependency link and its work record.
        self.assertIn("evidence unknown", text)
        self.assertNotIn("## Relevant failures", text)
        self.assertIn("def calculate_invoice_total(amounts)", text)
        self.assertEqual(self.work.show(task["id"])["revision"], claimed["revision"])

    def test_claim_with_source_reports_committed_revision_if_graph_becomes_unavailable(self) -> None:
        """Return the durable claim receipt on a partial failure so agents can recover safely."""
        planner = self.agent("planner")
        active = planner.add_task("Existing edit", status="in_progress", files=["invoice.py"])["node"]
        task = planner.add_task("Add total regression", files=["invoice.py"], next_action="Run invoice checks")["node"]
        self.storage.write_bytes(b"invalid store contents")
        code, output = self._claim_cli(task["id"], "-total")
        self.assertEqual(code, 2)
        result = json.loads(output)
        self.assertIn("source_context_error", result)
        self.assertEqual(result["claimed_task"]["revision"], self.work.show(task["id"])["revision"])
        self.assertEqual(self.work.show(task["id"])["agent_id"], "agent:worker")
        self.assertEqual(result["work_context"]["records"][0]["next_action"], "Run invoice checks")
        self.assertEqual(result["file_overlaps"]["files"][0]["tasks"], [active["id"]])
        self.assertEqual(result["source_context_retry"],
                         f"reql query_context --task {task['id']} --query=-total --code --brief")

        with patch.object(CoordinationStore, "assignment_context", side_effect=ValueError("Shared read failed")):
            code, output = self._claim_cli(task["id"], "-total")
        self.assertEqual(code, 2)
        retry = json.loads(output)
        self.assertEqual(retry["claimed_task"], result["claimed_task"])
        self.assertEqual(retry["source_context_retry"], result["source_context_retry"])
        self.assertIn("Shared read failed", retry["source_context_error"])

    def test_A_session_continuation_survives_private_cleanup_and_zero_message_retention(self) -> None:
        first = self.agent("A")
        first.config = merge_config(default_config(), {"retention": {"agent_sessions": 0}})
        goal = first.record("goal", "Deliver bounded serializer", key="serializer")["node"]
        decision = first.add_decision("Keep schema version stable", rationale="Existing consumers depend on it",
                                     parent=goal["id"], files=["src/codec.py"])["node"]
        task = first.add_task("Implement serializer", parent=goal["id"], depends_on=[decision["id"]],
                              files=["src/codec.py", "src/client.py"])["node"]
        first.complete_task(task["id"], "Both consumers updated; codec tests pass")
        pending = first.add_task("Validate malformed payload", parent=goal["id"], files=["src/codec.py"],
                                 next_action="Add the missing malformed-input regression")["node"]
        question = first.record("question", "Should empty payload be legal?", parent=pending["id"],
                                files=["src/codec.py"])["node"]
        first.add_note("Scratch must not be published")
        first.finish("Serializer implemented; malformed-input handling remains")
        self.assertFalse(first.exists())
        self.assertEqual(first.public_dashboard()["context"], [])
        second = self.agent("B")
        recovered = self.work.context(files=["src/codec.py"], limit=8)
        ids = {item["id"] for item in recovered["records"]}
        self.assertTrue({goal["id"], decision["id"], task["id"], pending["id"], question["id"]}.issubset(ids))
        rendered = render_work_context(recovered)
        self.assertIn("Existing consumers", rendered)
        self.assertIn("malformed-input regression", rendered)
        self.assertNotIn("Scratch must not", rendered)
        self.assertIn(pending["id"], {item["id"] for item in self.work.context("malformed-input regression")["records"]})
        self.assertEqual(self.work.show(task["id"])["status"], "done")
        self.assertEqual(self.work.show(pending["id"])["origin_agent_id"], "agent:A")
        resumed = self.update(second, self.work.show(pending["id"]), status="in_progress")
        self.assertEqual(resumed["agent_id"], "agent:B")
        self.assertEqual(resumed["history"][-1]["agent_id"], "agent:A")

    def test_B_parallel_workstreams_expose_conflicts_and_integration_dependencies(self) -> None:
        parser, client = self.agent("parser"), self.agent("client")
        one = parser.add_decision("Return nullable result", rationale="Parser needs absent fields", workstream="parse")["node"]
        two = client.add_decision("Require complete result", rationale="UI needs complete fields", workstream="client",
                                  contradicts=[one["id"]])["node"]
        parse_task = parser.add_task("Parser result contract", workstream="parse")["node"]
        ui_task = client.add_task("Update UI consumer", workstream="client", depends_on=[parse_task["id"]])["node"]
        integration = client.add_task("Integrate result contract", workstream="integration",
                                      depends_on=[parse_task["id"], ui_task["id"], two["id"]])["node"]
        parser.complete_task(parse_task["id"], "Parser contract tests passed")
        parser.finish("Parser complete")
        client.finish("Consumer and integration remain")
        context = self.work.context(record_id=integration["id"], limit=8)
        items = {item["id"]: item for item in context["records"]}
        self.assertEqual(items[ui_task["id"]]["execution_state"], "ready")
        self.assertEqual(items[integration["id"]]["waiting_on"], [ui_task["id"]])
        self.assertIn(one["id"], items[two["id"]]["conflicts"])
        self.assertIn(two["id"], items[one["id"]]["conflicts"])
        overview = self.work.overview()
        self.assertEqual(set(overview["workstreams"]), {"parse", "client", "integration"})
        self.assertTrue(overview["sections"]["blockers"])

    def test_C_plan_replacement_preserves_cause_and_flags_obsolete_work(self) -> None:
        agent = self.agent("plan")
        old = agent.add_decision("Use cached parser", rationale="Reduce latency", files=["src/parser.py"])["node"]
        task = agent.add_task("Integrate cached parser", depends_on=[old["id"]], files=["src/parser.py"])["node"]
        new = agent.add_decision("Use direct parser", rationale="Cache returns stale results after edits",
                                 supersedes=[old["id"]], files=["src/parser.py"])["node"]
        current = self.work.context("parser", files=["src/parser.py"])
        ids = [item["id"] for item in current["records"]]
        self.assertIn(new["id"], ids)
        self.assertNotIn(old["id"], ids)
        current_task = next(item for item in current["records"] if item["id"] == task["id"])
        self.assertEqual(current_task["execution_state"], "needs_review")
        with self.assertRaisesRegex(ValueError, "obsolete"):
            agent.complete_task(task["id"], "Cannot pretend this plan is current")
        historical = self.work.context("parser", include_history=True)
        self.assertIn(old["id"], {item["id"] for item in historical["records"]})
        prior = self.work.show(old["id"])
        self.assertEqual(prior["status"], "superseded")
        self.assertEqual(prior["replaced_by"], new["id"])
        self.assertIn("stale", prior["replacement_reason"])
        self.assertEqual(prior["history"][-1]["status"], "active")
        with self.assertRaisesRegex(ValueError, "Already superseded"):
            agent.add_decision("Use yet another parser", rationale="Competing stale plan", supersedes=[old["id"]])
        updated = self.update(agent, self.work.show(task["id"]), depends_on=[new["id"]], rationale="Reconcile with direct parser")
        agent.complete_task(updated["id"], "Direct parser integration verified")

    def test_E_overview_derives_next_steps_from_current_execution(self) -> None:
        agent = self.agent("overview")
        goal = agent.record("goal", "Improve context retrieval")["node"]
        one = agent.add_task("Index scope", parent=goal["id"], workstream="retrieval")["node"]
        two = agent.add_task("Wire context", parent=goal["id"], depends_on=[one["id"]], workstream="retrieval")["node"]
        agent.add_decision("Keep local deterministic core", rationale="No provider dependencies", parent=goal["id"])
        agent.record("question", "Which callers depend on envelope?", parent=two["id"])
        before = self.work.overview()
        self.assertEqual([item["id"] for item in before["sections"]["next"]], [one["id"]])
        agent.complete_task(one["id"], "Index tests pass")
        agent.finish("Index done; integration remains")
        after = CoordinationStore(self.work.path).overview()
        self.assertEqual([item["id"] for item in after["sections"]["next"]], [two["id"]])
        self.assertEqual(after["sections"]["goals"][0]["id"], goal["id"])
        self.assertEqual(after["sections"]["completed"][0]["id"], one["id"])
        self.assertTrue(after["sections"]["direction"])
        self.assertTrue(after["sections"]["blockers"])
        self.assertTrue(after["sections"]["checkpoints"])
        checkpoint = after["sections"]["checkpoints"][0]
        self.assertIn(goal["id"], checkpoint["summarizes"])
        self.assertEqual(checkpoint["waiting_on"], [])

    def test_duplicate_revision_conflict_and_atomic_supersession(self) -> None:
        agent = self.agent("atomic")
        decision = agent.add_decision("Use streaming", rationale="Bound allocations", key="transfer")["node"]
        before = self.work.path.read_bytes()
        duplicate = agent.add_decision("Use streaming", rationale="Bound allocations", key="transfer")
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["node"]["revision"], 1)
        self.assertEqual(self.work.path.read_bytes(), before)
        updated = self.update(agent, decision, rationale="Bound allocations and latency")
        with self.assertRaisesRegex(ValueError, "revision changed"):
            self.update(agent, decision, rationale="Stale competing assumption")
        with self.assertRaisesRegex(ValueError, "Invalid"):
            agent.add_decision("Use buffering", rationale="New evidence", supersedes=[decision["id"], "absent"])
        self.assertEqual(self.work.show(decision["id"])["status"], "active")
        self.assertEqual(self.work.show(decision["id"])["revision"], updated["revision"])
        first_goal = agent.record("goal", "First feature")["node"]
        second_goal = agent.record("goal", "Second feature")["node"]
        first_tests = agent.add_task("Add regression tests", parent=first_goal["id"])["node"]
        second_tests = agent.add_task("Add regression tests", parent=second_goal["id"])["node"]
        self.assertNotEqual(first_tests["id"], second_tests["id"])
        self.assertEqual(agent.add_task("Add regression tests", parent=first_goal["id"])["node"]["id"], first_tests["id"])

    def test_parallel_writers_reconcile_one_revision_without_lost_update(self) -> None:
        agent = self.agent("parallel")
        item = agent.add_task("Shared task")["node"]

        def change(number: int) -> str:
            """Race observed revisions through independent storage opens."""
            try:
                self.work.put("task", item["content"], agent_id=f"agent:{number}", session_id="parallel",
                              record_id=item["id"], expected_revision=1, status="in_progress",
                              rationale=f"Claim {number}")
                return "updated"
            except ValueError as exc:
                self.assertIn("revision changed", str(exc))
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(change, range(2)))
        self.assertCountEqual(results, ["updated", "conflict"])
        self.assertEqual(self.work.show(item["id"])["revision"], 2)

    def test_cycles_paths_and_lifecycle_reasons_are_validated(self) -> None:
        agent = self.agent("validation")
        first = agent.add_task("First")["node"]
        second = agent.add_task("Second", depends_on=[first["id"]])["node"]
        with self.assertRaisesRegex(ValueError, "Cycle"):
            self.update(agent, first, depends_on=[second["id"]])
        for path in ("../secret", "C:/private", "/private"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "repository-relative"):
                agent.record("change", "unsafe evidence", files=[path])
        with self.assertRaisesRegex(ValueError, "rationale"):
            self.update(agent, first, status="blocked")
        with self.assertRaisesRegex(ValueError, "waiting"):
            agent.complete_task(second["id"], "Premature completion")
        with self.assertRaisesRegex(ValueError, "reason must not be empty"):
            agent.reject_approach("Cache every query", " ")
        with self.assertRaisesRegex(ValueError, "content must not be empty"):
            agent.reject_approach(" ", "Stale results")
        self.assertEqual(self.work.show(first["id"])["revision"], 1)

    def test_history_and_terminal_retention_preserve_active_and_referenced_evidence(self) -> None:
        agent = self.agent("retention")
        item = agent.record("observation", "Current finding")["node"]
        for number in range(12):
            item = self.update(agent, item, rationale=f"Evidence {number}")
        self.assertEqual(len(item["history"]), MAX_REVISIONS)
        old = agent.add_task("Old outcome")["node"]
        agent.complete_task(old["id"], "Tested")
        active = agent.add_task("Follow-up", depends_on=[old["id"]])["node"]
        # A smaller limit exercises the actual maintenance policy without 200 writes.
        from unittest.mock import patch
        with patch("memory.services.coordination.MAX_TERMINAL_RECORDS", 2):
            for number in range(5):
                agent.record("change", f"Completed {number}", status="done")
            self.assertEqual(self.work.prune(), 3)
        ids = {record["id"] for record in self.work.records()}
        self.assertTrue({item["id"], old["id"], active["id"]}.issubset(ids))

    def test_compiled_code_context_and_mcp_share_scoped_engineering_evidence(self) -> None:
        agent = self.agent("integration")
        decision = agent.add_decision("Preserve payload field", rationale="Client contract", files=["src/codec.py"])["node"]
        graph = MemoryGraph.open(self.storage)
        try:
            graph.add_node(MemoryNode(id="function:encode", type="Function", label="encode_payload", text="encode payload serializer",
                                     properties={"relative_path": "src/codec.py", "line_start": 1, "line_end": 5}))
            payload = graph.query_context_payload("encode payload", scopes=("code",))
            work = payload["payload"]["engineering_context"]
            self.assertIn(decision["id"], {item["id"] for item in work["records"]})
            self.assertIn("Client contract", graph.query_context("encode payload", scopes=("code",)))
        finally:
            graph.close()
        mcp = mcp_context(storage_path=str(self.storage), query="encode payload", code=True)
        self.assertEqual(mcp["payload"]["engineering_context"], work)

    def test_mcp_work_tools_support_sessions_reconciliation_and_read_only_discovery(self) -> None:
        from mcp.tools import call_tool, list_tools, MCPToolError
        common = {"storage_path": str(self.storage)}
        created = call_tool("reql_work_record", {**common, "kind": "task", "content": "MCP continuation",
                                                "agent_id": "agent:first", "session_id": "session:first"})["node"]
        updated = call_tool("reql_work_record", {**common, "kind": "task", "content": created["content"],
                                                "agent_id": "agent:next", "session_id": "session:next",
                                                "fields": {"record_id": created["id"], "expected_revision": 1,
                                                           "status": "in_progress", "rationale": "Continue the handoff"}})["node"]
        context = call_tool("reql_work_context", {**common, "record_id": created["id"], "include_history": True})
        self.assertEqual(context["records"][0]["history"][0]["agent_id"], "agent:first")
        self.assertEqual(updated["session_id"], "session:next")
        overview = call_tool("reql_work_overview", common)
        self.assertEqual(overview["sections"]["active"][0]["id"], created["id"])
        readable = {schema["name"] for schema in list_tools(include_write=False)}
        self.assertIn("reql_work_context", readable)
        self.assertIn("reql_work_overview", readable)
        self.assertNotIn("reql_work_record", readable)
        with self.assertRaisesRegex(MCPToolError, "revision changed"):
            call_tool("reql_work_record", {**common, "kind": "task", "content": created["content"],
                                         "agent_id": "agent:stale", "session_id": "stale",
                                         "fields": {"record_id": created["id"], "expected_revision": 1, "status": "done"}})
        self.assertFalse(self.storage.exists())
