"""Exercise ordered checks and evidence reuse on an indexed project."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from api import MemoryGraph
from memory.cli import main
from memory.config import default_config
from memory.services.verification import VerificationService


class VerificationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = Path(".tmp-test")
        scratch.mkdir(exist_ok=True)
        folder = TemporaryDirectory(dir=scratch)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.storage = self.root / ".reql" / "memory.reql"
        self.source = self.root / "app.py"
        self.source.write_text("value = 1\n", encoding="utf-8")
        self.compile()
        self.focused_input = self.root / "focused.txt"
        self.focused_input.write_text("pass", encoding="utf-8")
        self.broad_input = self.root / "broad.txt"
        self.broad_input.write_text("pass", encoding="utf-8")
        self.log = self.storage.parent / "checks.log"
        command = (
            "from pathlib import Path; import sys\n"
            "name = sys.argv[1]\n"
            "with Path('.reql/checks.log').open('a') as log:\n"
            "    log.write(name + '\\n')\n"
            "raise SystemExit(Path(name + '.txt').read_text().startswith('fail'))\n"
        )
        self.plan = {stage: [{"command": [sys.executable, "-c", command, stage], "inputs": [f"{stage}.txt"]}]
                     for stage in ("focused", "broad")}

    def compile(self) -> None:
        """Publish the project's current revision through normal compilation."""
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)

    def verify_cli(self) -> tuple[int, dict]:
        """Read structured results while running the real CLI and check commands."""
        plan_path = self.storage.parent / "verify.json"
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()):
                code = main(["agent", "verify", "--plan", str(plan_path), "--json"])
        finally:
            os.chdir(previous)
        return code, json.loads(output.getvalue())

    def test_verification_orders_checks_and_reuses_only_matching_successes(self) -> None:
        code, first = self.verify_cli()
        self.assertEqual(code, 0, first)
        self.assertEqual(self.log.read_text().splitlines(), ["focused", "broad"])
        self.assertEqual([row["reused"] for row in first["checks"]], [False, False])
        code, second = self.verify_cli()
        self.assertEqual(code, 0, second)
        self.assertEqual([row["reused"] for row in second["checks"]], [True, True])
        self.assertEqual(self.log.read_text().splitlines(), ["focused", "broad"])

        self.focused_input.write_text("fail", encoding="utf-8")
        for _ in range(2):
            code, failed = self.verify_cli()
            self.assertEqual(code, 1, failed)
            self.assertEqual(failed["checks"], [{"stage": "focused", "check": 1, "exit_code": 1, "reused": False}])
        self.assertEqual(self.log.read_text().splitlines(), ["focused", "broad", "focused", "focused"])

        self.focused_input.write_text("pass", encoding="utf-8")
        self.source.write_text("value = 2\n", encoding="utf-8")
        self.compile()
        code, changed = self.verify_cli()
        self.assertEqual(code, 0, changed)
        self.assertNotEqual(changed["source_revision"], first["source_revision"])
        self.assertEqual([row["reused"] for row in changed["checks"]], [False, False])
        self.assertEqual(self.log.read_text().splitlines()[-2:], ["focused", "broad"])

    def test_success_is_published_once_for_concurrent_workers(self) -> None:
        start = Barrier(2, timeout=5)

        def verify() -> dict:
            service = VerificationService(self.root, self.storage, default_config())
            start.wait()
            return service.verify(self.plan, emit=lambda _: None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(verify) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertTrue(all(outcome["passed"] for outcome in outcomes))
        self.assertEqual(self.log.read_text().splitlines(), ["focused", "broad"])
        self.assertEqual(sum(row["reused"] for outcome in outcomes for row in outcome["checks"]), 2)

    def test_a_check_that_changes_its_input_cannot_publish_a_pass(self) -> None:
        command = "from pathlib import Path; Path('focused.txt').write_text('changed')"
        self.plan["focused"][0]["command"] = [sys.executable, "-c", command]
        code, result = self.verify_cli()
        self.assertEqual(code, 2, result)
        self.assertIn("inputs or revision changed", result["error"]["message"])
        self.assertFalse(self.log.exists())
        code, result = self.verify_cli()
        self.assertEqual(code, 0, result)
        self.assertFalse(result["checks"][0]["reused"])

    def test_checks_without_declared_inputs_always_run(self) -> None:
        self.plan["broad"][0].pop("inputs")
        for reused_focused in (False, True):
            code, result = self.verify_cli()
            self.assertEqual(code, 0, result)
            self.assertEqual([row["reused"] for row in result["checks"]], [reused_focused, False])
        self.assertEqual(self.log.read_text().splitlines(), ["focused", "broad", "broad"])


if __name__ == "__main__":
    unittest.main()
