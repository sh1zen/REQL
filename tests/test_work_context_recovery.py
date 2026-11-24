"""Validate complete, scoped handoffs from persisted shared work."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from mcp.tools import reql_work_context
from memory.services.coordination import CoordinationStore, render_work_context, render_work_overview


class WorkContextRecoveryTests(unittest.TestCase):
    """Recover current assumptions and complete task evidence without history dumps."""

    def setUp(self) -> None:
        scratch = Path.cwd() / ".tmp" / "regression-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = CoordinationStore(self.root / ".reql" / "agent-dashboard.reql")

    def put(self, kind: str, content: str, **fields) -> dict:
        """Write through the normal revision-checked store boundary."""
        return self.work.put(kind, content, agent_id="agent:planner", session_id="session:plan", **fields)["node"]

    def replacements(self) -> tuple[dict, dict, dict, dict]:
        """Build a changed contract whose replacements have no lexical/file anchor."""
        old = self.put("decision", "Store complete requests", rationale="Initial contract")
        task = self.put("task", "Implement transport", depends_on=[old["id"]])
        middle = self.put("decision", "Store request references", rationale="Changed retention policy", supersedes=[old["id"]])
        current = self.put("decision", "Store signed reference hashes", rationale="Revised privacy contract", supersedes=[middle["id"]])
        return old, middle, current, task

    def test_task_recovers_current_replacement_chain_without_rewriting_dependencies(self) -> None:
        old, middle, current, task = self.replacements()
        before = self.work.show(task["id"])
        payload = self.work.context(record_id=task["id"])
        records = {row["id"]: row for row in payload["records"]}
        self.assertIn(current["id"], records)
        self.assertNotIn(old["id"], records)
        self.assertNotIn(middle["id"], records)
        self.assertIn("replacement:" + old["id"], records[current["id"]]["reasons"])
        self.assertEqual(records[task["id"]]["execution_state"], "needs_review")
        self.assertEqual(records[task["id"]]["obsolete_dependencies"], [old["id"]])
        self.assertEqual(before, self.work.show(task["id"]))
        with self.assertRaisesRegex(ValueError, "obsolete"):
            self.work.put(record_id=task["id"], expected_revision=task["revision"], status="done",
                          agent_id="agent:worker", session_id="session:worker", rationale="Do not accept obsolete assumptions")

    def test_explicit_task_keeps_causal_evidence_in_a_large_noisy_backlog(self) -> None:
        _, _, current, task = self.replacements()
        for index in range(85):
            self.put("change", f"Unrelated shared module outcome {index}", status="done", files=["src/shared.py"])
        payload = self.work.context("shared module", record_id=task["id"], files=["src/shared.py"], limit=4)
        ids = {row["id"] for row in payload["records"]}
        self.assertIn(task["id"], ids)
        self.assertIn(current["id"], ids)
        self.assertFalse(any("Unrelated" in row["content"] for row in payload["records"]))

    def test_recovers_wide_and_deep_prerequisites_across_providers(self) -> None:
        """Follow every matched branch and keep long evidence in the rendered handoff."""
        rationale = "Acceptance detail. " * 400 + "End of the contract."
        paths = [f"src/module_{index}.py" for index in range(20)]
        leaf = self.put("decision", "Definition of accepted behavior", rationale=rationale, files=paths)
        chain = [leaf]
        for index in range(8):
            chain.append(self.put("decision", f"Layer {index} of the contract", rationale="Delegates to the prior layer",
                                  depends_on=[chain[-1]["id"]]))
        anchors = [self.put("task", f"Integration prerequisite {index}", depends_on=[chain[-1]["id"]])
                   for index in range(50)]
        verification = "Contract explanation. " * 24 + "VERIFIED: python -m unittest tests.test_transport; 23 passed."
        task_paths = [f"src/part_{index}.py" for index in range(7)]
        task = self.put("task", "Complete every prerequisite", rationale=verification, files=task_paths,
                        depends_on=[row["id"] for row in anchors])
        payload = self.work.context(record_id=task["id"])
        expected = {row["id"] for row in [task, *anchors, *chain]}
        self.assertEqual({row["id"] for row in payload["records"]}, expected)
        self.assertEqual((payload["total_matches"], payload["omitted"]), (len(expected), 0))
        self.assertEqual(reql_work_context(storage_path=str(self.root / ".reql" / "memory.reql"), record_id=task["id"]), payload)
        before = deepcopy(payload)
        text = render_work_context(payload, record_id=task["id"])
        self.assertEqual(payload, before)
        self.assertIn(verification, text)
        self.assertIn(task_paths[-1], text)
        self.assertIn(rationale, text)
        self.assertIn(paths[-1], text)
        overview = self.work.overview()
        self.assertEqual(len(overview["sections"]["active"]), 51)
        self.assertIn(rationale, render_work_overview(overview))
        limited = self.work.context(record_id=task["id"], limit=45)
        self.assertEqual((len(limited["records"]), limited["omitted"]), (45, len(expected) - 45))

    def test_each_direct_match_expands_its_own_branch(self) -> None:
        leaves = []
        for index in range(12):
            leaf = self.put("constraint", f"Required invariant {index}")
            self.put("task", f"Integration outcome {index}", depends_on=[leaf["id"]])
            leaves.append(leaf["id"])
        payload = self.work.context("integration outcome")
        self.assertEqual(len(payload["records"]), 24)
        self.assertTrue(set(leaves) <= {row["id"] for row in payload["records"]})

    def test_direct_constraint_survives_a_small_task_budget(self) -> None:
        """Keep an explicit acceptance contract ahead of history on a bounded handoff."""
        observations = [self.put("observation", f"Earlier result {n}") for n in range(8)]
        constraint = self.put("constraint", "The public call returns the phase result.")
        task = self.put("task", "Add the operation", depends_on=[*(row["id"] for row in observations), constraint["id"]])
        rows = self.work.context(record_id=task["id"], limit=2)["records"]
        self.assertEqual([row["id"] for row in rows], [task["id"], constraint["id"]])

    def test_small_budgets_never_present_partial_coordination_links_as_complete(self) -> None:
        dependencies = [self.put("task", f"Prerequisite {index}")["id"] for index in range(32)]
        task = self.put("task", "Coordinate all prerequisites", depends_on=dependencies)
        payload = self.work.context(record_id=task["id"], limit=1)
        text = render_work_context(payload, record_id=task["id"], max_chars=700)
        self.assertIn("task/blocked", text)
        self.assertIn("coordination links omitted", text)
        self.assertIn("agent show " + task["id"] + " --json", text)
        for budget in (1, 32, 100, 700, 1200, 6000):
            with self.subTest(budget=budget):
                self.assertLessEqual(len(render_work_context(payload, record_id=task["id"], max_chars=budget)), budget)
        for budget in (0, -1, True):
            with self.subTest(invalid=budget), self.assertRaises(ValueError):
                render_work_context(payload, max_chars=budget)

if __name__ == "__main__":
    unittest.main()
