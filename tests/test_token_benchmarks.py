
"""Test benchmark isolation and the token report without live model calls."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from benchmarks import token
from memory.agent import AgentWorkspace


class TokenBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(".tmp")
        self.scratch.mkdir(exist_ok=True)

    def test_paired_run_reads_one_frozen_tree_and_grades_both_handoffs(self) -> None:
        """A local fake agent drives the real preparation, grading, and reporting."""
        with TemporaryDirectory(dir=self.scratch) as folder:
            root = Path(folder).resolve()
            seed = root / "seed"
            seed.mkdir()
            source = seed / "solution.py"
            source.write_text("value = 0\n", encoding="utf-8")
            rules = seed / "AGENTS.md"
            rules.write_text(
                "Shared instructions\n<!-- REQL-INSTALL:START -->\nHistorical rules\n<!-- REQL-INSTALL:END -->\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            token.git(seed, env, "init", "-q")
            token.git(seed, env, "add", "--all")
            token.git(seed, env, "-c", "user.name=Benchmark", "-c", "user.email=benchmark@example.invalid",
                      "-c", "core.hooksPath=.git/empty-hooks", "-c", "commit.gpgsign=false",
                      "commit", "-qm", "Frozen seed")
            revision = token.git(seed, env, "rev-parse", "HEAD").decode().strip()
            source.write_text("value = 99\n", encoding="utf-8")
            case = {"id": "pair", "fixture": "seed", "revision": revision, "task": "Set value to 7",
                    "query": "solution value", "files": ["solution.py"],
                    "check_commands": [["{python}", "-c", "from solution import value; assert value == 7"]]}
            manifest = root / "cases.json"
            token.save(manifest, [case])
            output = root / "results"

            def fake_agent(arm, project, case, worker, reql, env, args):
                self.assertEqual((project / "solution.py").read_text(encoding="utf-8"), "value = 0\n")
                self.assertNotIn("Historical rules", (project / "AGENTS.md").read_text(encoding="utf-8"))
                workspace = None
                task_id = None
                if reql:
                    self.assertTrue(Path(reql["skill"]).is_file())
                    task_id = reql["assignments"]["worker"]
                    workspace = AgentWorkspace(project / ".reql/memory.reql", agent_id="agent:worker",
                                               activity_id="worker")
                    workspace.init(task_id=task_id)
                else:
                    self.assertFalse((project / ".reql").exists())
                    (project / ".bench" / "worker.md").write_text("Verified the value change.", encoding="utf-8")
                (project / "solution.py").write_text("value = 7\n", encoding="utf-8")
                if workspace:
                    workspace.finish("Verified the value change", task_id=task_id)
                trace = arm / "worker.jsonl"
                turn = {"type": "turn.completed", "usage": {
                    "input_tokens": 100 if reql else 200, "cached_input_tokens": 20, "output_tokens": 10,
                }}
                trace.write_text(json.dumps(turn), encoding="utf-8")
                return token.usage_from_trace(trace)

            with (patch.object(token, "CASES_PATH", manifest),
                  patch.object(token, "run_worker", side_effect=fake_agent),
                  redirect_stdout(StringIO())):
                exit_status = token.main([
                    "--archive-root", str(root), "--output", str(output), "--codex", sys.executable,
                ])
            report = json.loads((output / "results.json").read_bytes())["cases"]["pair"]
            self.assertEqual(exit_status, 0, report)
            candidate, control = (report[mode] for mode in token.MODES)
            self.assertEqual(candidate["fixture_tree"], control["fixture_tree"])
            self.assertEqual(report["delta_percent"]["total_tokens"], -47.62)
            self.assertEqual(source.read_text(encoding="utf-8"), "value = 99\n")

    def test_usage_requires_completed_turns_and_consistent_counters(self) -> None:
        with TemporaryDirectory(dir=self.scratch) as directory:
            trace = Path(directory) / "worker.jsonl"
            trace.write_text('{"type": "turn.failed"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "No completed"):
                token.usage_from_trace(trace)
            trace.write_text(
                '{"type":"turn.completed","usage":{"input_tokens":2,"cached_input_tokens":3,"output_tokens":4}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Invalid Codex token counters"):
                token.usage_from_trace(trace)


if __name__ == "__main__":
    unittest.main()

