"""Exercise task handoffs and source guidance through installed command boundaries."""
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shlex
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from api import MemoryGraph
from memory.agent import AgentWorkspace
from memory.cli import main
from memory.config import load_effective_config, merge_config
from memory.domain.query_context import QueryContextRequest
from memory.services.coordination import CoordinationStore
from memory.services.query_context import QueryContextService


class TaskHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        parent = Path(temporary.name).resolve()
        self.root = parent / "main"
        self.root.mkdir()
        self.storage = self.root / ".reql" / "memory.reql"
        self.source = self.root / "invoice.py"
        self.source.write_text("def total():\n    return 7\n", encoding="utf-8")
        test_dir = self.root / "tests"
        test_dir.mkdir()
        (test_dir / "test_total.py").write_text(
            "import unittest\nfrom invoice import total\n"
            "class TotalTest(unittest.TestCase):\n"
            "    def test_total(self):\n"
            "        self.assertEqual(total(), 7)\n", encoding="utf-8",
        )
        self.work = CoordinationStore(self.storage.with_name("agent-dashboard.reql"))
        self.task = self.work.put("task", "Update invoice totals", files=["invoice.py"],
                                  agent_id="agent:owner", session_id="session:handoff")["node"]["id"]

    def _claimed_task(self) -> list[str]:
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        agent = ["agent", "--agent", "agent:owner", "--activity", "handoff"]
        with redirect_stdout(StringIO()):
            self.assertEqual(main([*agent, "init", "--task", self.task]), 0)
            self.assertEqual(main(["project", "compile"]), 0)
        return agent

    def test_assigned_brief_locates_setup_without_printing_the_document(self) -> None:
        """An indexed setup heading gives the agent a focused read location."""
        readme = self.root / "README.md"
        readme.write_text("# Invoice tool\n\n## Quick Start\n\nPrivate setup example.\n\n## Usage\n\nFull documentation.\n",
                          encoding="utf-8")
        guide = self.root / "guide" / "onboarding.markdown"
        guide.parent.mkdir()
        guide.write_text("# Guida\n\n## Avvio locale\n\nIstruzioni di avvio.\n\n## Utilizzo\n", encoding="utf-8")
        with (self.root / "reql.conf").open("a", encoding="utf-8") as config:
            config.write(
                'compile:\n  ingest_documents: true\n  documents:\n    markdown: true\n'
                'retrieval:\n  source:\n    setup_headings: ["Quick Start"]\n'
            )
        self._claimed_task()
        request = QueryContextRequest.from_raw(text="total", scopes=["code"], task_id=self.task)
        with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
            result = graph.query_context_service.execute(request, brief=True)
            self.assertNotIn("task_setup_location", result.payload)
        config = load_effective_config(start_dir=self.root, env={})
        for path, heading, body in (
            ("guide/onboarding.markdown", "Avvio locale", "Istruzioni di avvio"),
            ("README.md", "Quick Start", "Private setup example"),
        ):
            selected = merge_config(config, {"retrieval.source.setup_headings": [heading]})
            with self.subTest(path=path), closing(MemoryGraph.open(self.storage, read_only=True, config=selected)) as graph:
                service = graph.query_context_service
                result = service.execute(request, brief=True)
                location = result.payload["task_setup_location"]
                self.assertEqual((location["path"], location["title"], location["line_start"]), (path, heading, 3))
                text = service.render(result, brief=True)
                self.assertNotIn(body, text)
                if path != "README.md":
                    continue

                self.assertEqual(result.payload["task_setup_location"]["line_end"], 6)
                self.assertIn("`README.md` [3-6] (Quick Start)", text)
                bound = service.render(result, brief=True, completion_command=f"reql agent finish SUMMARY --task {self.task}")
                self.assertLess(bound.index("focused project checks"), bound.index("broader checks"))
                self.assertIn("reql project compile --lock-timeout", bound)
                self.assertIn("reql edit --plan -", bound)
                docs_command = next(line.split("`")[1] for line in bound.splitlines()
                                    if line.startswith("For missing documentation"))
                docs_args = shlex.split(docs_command)[1:]
                docs_args[docs_args.index("--query") + 1] = "README.md Private setup example"
                with redirect_stdout(StringIO()) as documentation:
                    self.assertEqual(main(docs_args), 0)
                self.assertIn("Private setup example", documentation.getvalue())
                self.assertNotIn("task_setup_location", service.execute(request, brief=True, follow_up=True).payload)
                readme.write_text("# Changed\n", encoding="utf-8")
                self.assertNotIn("task_setup_location", service.execute(request, brief=True).payload)

    def test_follow_up_retrieves_new_source_and_reuses_current_ranges(self) -> None:
        """Recover another owner and reuse displayed ranges until the source changes."""
        (self.root / "routing.py").write_text("def shipment_manifest():\n    return 'outbound'\n", encoding="utf-8")
        self.work.put(record_id=self.task, expected_revision=1, files=["invoice.py", "tests/test_total.py"],
                      agent_id="agent:owner", session_id=None)
        self.source.write_text("def total():\n    return 7\n\n"
                               "def currency():\n    return 'euro'\n", encoding="utf-8")
        self._claimed_task()
        query = ["query_context", "--task", self.task, "--query", "shipment_manifest", "--code", "--brief"]
        with redirect_stdout(StringIO()) as ordinary:
            self.assertEqual(main(query), 0)
        self.assertIn("test_total.py", ordinary.getvalue())
        with redirect_stdout(StringIO()) as follow_up:
            self.assertEqual(main([*query, "--follow-up"]), 0)
        text = follow_up.getvalue()
        self.assertIn("routing.py", text)
        self.assertNotIn("test_total.py", text)
        self.assertNotIn("Engineering context", text)

        with redirect_stdout(StringIO()) as initial:
            self.assertEqual(main(["query_context", "--task", self.task, "--query", "total", "--code", "--brief"]), 0)
        text = initial.getvalue()
        self.assertIn("return 7", text)
        command = next(line.split("`")[1] for line in text.splitlines() if "--seen-source" in line)
        args = shlex.split(command)[1:]
        args[args.index("--query") + 1] = "total missing_reference currency"
        with redirect_stdout(StringIO()) as extra:
            self.assertEqual(main(args), 0)
        self.assertNotIn("return 7", extra.getvalue())
        self.assertIn("return 'euro'", extra.getvalue())
        self.assertNotIn("Engineering context", extra.getvalue())

        self.source.write_text("def total():\n    return 8\n\n"
                               "def currency():\n    return 'euro'\n", encoding="utf-8")
        with redirect_stdout(StringIO()) as stale:
            self.assertEqual(main(args), 0)
        self.assertIn("Graph freshness: stale", stale.getvalue())
        self.assertNotIn("return 8", stale.getvalue())
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["project", "compile"]), 0)
        with redirect_stdout(StringIO()) as refreshed:
            self.assertEqual(main(args), 0)
        self.assertIn("return 8", refreshed.getvalue())

    def test_standalone_edit_preserves_source_and_finish_closes_the_claim(self) -> None:
        """Record the agent's verification results when closing the claim."""
        self.source.write_bytes(b"def total():\r\n    return 7\r\n")
        agent = self._claimed_task()
        plan = [{"path": "invoice.py", "find": "    return 7", "text": "    return 3 + 4"}]
        with patch("sys.stdin", StringIO(json.dumps(plan))), redirect_stdout(StringIO()) as edited:
            self.assertEqual(main(["edit", "--plan", "-", "--json"]), 0)
        self.assertEqual(json.loads(edited.getvalue())["changed"], ["invoice.py"])
        self.assertEqual(self.source.read_bytes(), b"def total():\r\n    return 3 + 4\r\n")
        summary = "Project tests passed when run by the agent."
        with (patch("subprocess.Popen", side_effect=AssertionError("finish must not launch checks")),
              patch("memory.cli._refresh_project", side_effect=AssertionError("finish must only record the handoff")),
              redirect_stdout(StringIO()) as finished):
            self.assertEqual(main([*agent, "finish", summary, "--task", self.task, "--json"]), 0)
        result = json.loads(finished.getvalue())
        self.assertEqual(result["completed_task"]["revision"], 3)
        self.assertEqual(self.work.show(result["checkpoint"]["id"])["content"], summary)

    def test_claim_suggests_finish_using_the_claimed_revision(self) -> None:
        """The supplied command closes the intended explicit session."""
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["project", "compile"]), 0)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["agent", "--agent", "agent:owner", "--activity=-task suite",
                                   "init", "--task", self.task, "--query", "total"]), 0)
        expected = (f"reql agent --agent agent:owner --activity='-task suite' "
                    f"finish 'SUMMARY' --task {self.task}")
        self.assertIn(expected, output.getvalue())
        self.assertIn("reql edit --plan -", output.getvalue())
        self.assertIn(f"--task {self.task} --follow-up", output.getvalue())
        self.assertIn("Pipe a JSON array to stdin, even for one edit.", output.getvalue())
        self.assertIn("{path,find,text}", output.getvalue())
        command = shlex.split(expected)
        with redirect_stdout(StringIO()) as result:
            self.assertEqual(main([*command[1:], "--json"]), 0)
        finished = json.loads(result.getvalue())
        self.assertEqual(finished["completed_task"]["revision"], 3)

    def test_combined_claim_waits_for_prerequisite_and_shows_scoped_source(self) -> None:
        """A pending prerequisite leaves the claim active and source available."""
        prereq = self.work.put("task", "Publish upstream contract",
                               agent_id="agent:planner", session_id=None)["node"]
        self.work.put(record_id=self.task, expected_revision=1, depends_on=[prereq["id"]],
                      agent_id="agent:planner", session_id=None)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        args = ["agent", "--agent", "agent:owner", "init", "--task", self.task, "--query", "total"]
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["project", "compile"]), 0)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(args), 0)
        text = output.getvalue()
        self.assertIn("Claim active. Finish waits", text)
        self.assertIn(f"`reql agent wait {prereq['id']}`", text)
        self.assertIn("def total():", text)
        self.assertIn("--follow-up", text)
        self.assertNotIn("changed after the claim", text)
        self.assertNotIn("finish 'SUMMARY'", text)
        claim = self.work.show(self.task)
        self.assertEqual(claim["status"], "in_progress")

        self.work.put(record_id=prereq["id"], expected_revision=1, status="done",
                      agent_id="agent:planner", session_id=None)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(args), 0)
        self.assertIn("finish 'SUMMARY'", output.getvalue())
        self.assertEqual(self.work.show(self.task)["claim"], claim["claim"])

    def test_combined_claim_omits_finish_if_task_changes_during_recovery(self) -> None:
        """The durable claim receipt must not offer a stale completion revision."""
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["project", "compile"]), 0)
        execute = QueryContextService.execute

        def changed(service, request, *, brief=False):
            self.work.put(record_id=self.task, expected_revision=2, next_action="Review new contract",
                          agent_id="agent:reviewer", session_id=None)
            return execute(service, request, brief=brief)

        with (patch.object(QueryContextService, "execute", changed), redirect_stdout(StringIO()) as output):
            self.assertEqual(main(["agent", "--agent", "agent:owner",
                                   "init", "--task", self.task, "--query", "total"]), 0)
        text = output.getvalue()
        self.assertIn(f"Claimed task: {self.task}\tr2", text)
        self.assertIn("changed after the claim", text)
        self.assertIn("Review new contract", text)
        self.assertNotIn("finish 'SUMMARY'", text)
        self.assertNotIn("reql edit --plan -", text)
        self.assertEqual(self.work.show(self.task)["revision"], 3)

    def test_finish_rejects_a_changed_task_revision(self) -> None:
        """A concurrent task update cannot be overwritten by completion."""
        agent = self._claimed_task()
        assigned_task = AgentWorkspace._assigned_task

        def concurrent_edit(workspace, task_id, expected_revision):
            task = assigned_task(workspace, task_id, expected_revision)
            self.work.put(record_id=self.task, expected_revision=2, next_action="A concurrent revision",
                          agent_id="agent:reviewer", session_id=None)
            return task

        with (patch.object(AgentWorkspace, "_assigned_task", new=concurrent_edit),
              redirect_stdout(StringIO()), redirect_stderr(StringIO())):
            self.assertEqual(main([*agent, "finish", "Verified", "--task", self.task]), 2)
        current = self.work.show(self.task)
        self.assertEqual((current["status"], current["revision"]), ("in_progress", 3))
        self.assertEqual(current["next_action"], "A concurrent revision")
        self.assertFalse(any(row["kind"] == "checkpoint" for row in self.work.records()))
