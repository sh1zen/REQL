"""Exercise verification reuse, invalidation and handoff through installed command boundaries."""
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from api import MemoryGraph
from memory.cli import main
from memory.artifacts.revision import RevisionRepository
from memory.domain.query_context import QueryContextRequest
from memory.services.coordination import CoordinationStore
from memory.services.query_context import QueryContextService
from memory.storage import BlockGraphStore
from memory.testing import verification as verification_module
from memory.testing.evidence_store import TestEvidenceStore, VerificationEvidenceChanged, verification_plan
from memory.testing.verification import run_saved_checks, verification_handoff


class VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        parent = Path(temporary.name).resolve()
        self.root = parent / "main"
        self.root.mkdir()
        self.storage = self.root / ".reql" / "memory.reql"
        self.source = self.root / "invoice.py"
        self.source.write_text("def total():\n    return 7\n", encoding="utf-8")
        (self.root / "reql.conf").write_text(
            "scan:\n  exclude: [\"local.ini\", \"Cargo.toml\", \"Cargo.lock\", \".cargo/\", \"tests/acceptance.rs\"]\n",
            encoding="utf-8",
        )
        self.local_input = self.root / "local.ini"
        self.local_input.write_text("mode = 1\n", encoding="utf-8")
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
                                  agent_id="agent:owner", session_id="session:verify")["node"]["id"]
        self.evidence = TestEvidenceStore(self.storage)

    def test_two_processes_reuse_the_first_matching_pass(self) -> None:
        """Two saved verification requests for one task run the tests only once."""
        self._save_plan(patterns=("test_total.py",))
        test_file = self.root / "tests" / "test_total.py"
        with test_file.open("a", encoding="utf-8") as stream:
            stream.write("        from pathlib import Path\n"
                         "        import time\n"
                         "        with Path('.reql/runs.txt').open('a') as counter:\n"
                         "            counter.write('ran\\n')\n"
                         "        time.sleep(0.4)\n")
        script = """
import json
from pathlib import Path
import sys
from memory.testing.evidence_store import TestEvidenceStore
from memory.testing.verification import run_saved_checks

root = Path.cwd()
store = TestEvidenceStore(root / '.reql/memory.reql')
result = run_saved_checks(root, store, task_id=sys.argv[1], timeout=15)
print(json.dumps([result['status'], result['reused']]))
"""
        workers = []
        try:
            for _ in range(2):
                workers.append(subprocess.Popen([sys.executable, "-c", script, self.task], cwd=self.root,
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            outcomes = []
            for worker in workers:
                output, errors = worker.communicate(timeout=25)
                self.assertEqual(worker.returncode, 0, errors)
                outcomes.append(json.loads(output))
            self.assertCountEqual(outcomes, [["passed", False], ["passed", True]])
            self.assertEqual((self.root / ".reql" / "runs.txt").read_text().splitlines(), ["ran"])
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate()

    def test_receipt_superseded_during_reuse_is_rejected(self) -> None:
        """Another writer can replace a pass while the runner validates it for reuse."""
        result = self._run_checks(patterns=("test_total.py",))
        self.assertEqual(result["status"], "passed")
        receipt = result["receipt"]
        runner = verification_module._runner_fingerprints

        def replace_pass(environment, *, isolated_bytecode):
            self.evidence.record_verification({**receipt, "checks": [{"name": "test_total.py", "passed": False}]})
            return runner(environment, isolated_bytecode=isolated_bytecode)

        with patch.object(verification_module, "_runner_fingerprints", side_effect=replace_pass):
            with self.assertRaises(VerificationEvidenceChanged):
                self._run_checks()

    def _claimed_task(self) -> list[str]:
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        agent = ["agent", "--agent", "agent:owner", "--activity", "verification"]
        with redirect_stdout(StringIO()):
            self.assertEqual(main([*agent, "init", "--task", self.task]), 0)
            self.assertEqual(self._run_checks(patterns=("test_total.py",), diff=True)["status"], "passed")
            self.assertEqual(main(["project", "compile"]), 0)
        return agent

    def _save_plan(self, *, patterns: tuple[str, ...] = (), diff: bool = False,
                   extra_inputs: tuple[str, ...] = (), cargo: tuple[str, ...] = (),
                   cargo_test: tuple[str, ...] = ()) -> None:
        """Seed a previously saved selection without matching passing evidence."""
        receipt = {"root": str(self.root), "task_id": self.task, "patterns": list(patterns), "diff": diff,
                   "extra_inputs": list(extra_inputs), "fingerprint": "", "checks": []}
        if cargo:
            receipt["cargo"] = list(cargo)
        if cargo_test:
            receipt["cargo_test"] = list(cargo_test)
        self.evidence.record_verification(receipt)

    def _run_checks(self, *, patterns: tuple[str, ...] = (), diff: bool = False,
                    extra_inputs: tuple[str, ...] = (), cargo: tuple[str, ...] = (),
                    cargo_test: tuple[str, ...] = (), expected_plan: str | None = None) -> dict:
        """Exercise the same saved-check runner as task completion."""
        if patterns or diff or cargo or cargo_test:
            self._save_plan(patterns=patterns, diff=diff, extra_inputs=extra_inputs, cargo=cargo, cargo_test=cargo_test)
        return run_saved_checks(self.root, self.evidence, task_id=self.task, timeout=20, expected_plan=expected_plan)

    def _matching_pass(self) -> dict | None:
        return verification_handoff(self.root, self.evidence, self.task).get("verified_checks")

    def test_assigned_brief_points_to_verified_setup_without_printing_readme(self) -> None:
        """Saved checks and an indexed setup heading give the agent a focused read location."""
        readme = self.root / "README.md"
        readme.write_text("# Invoice tool\n\n## Quick Start\n\nPrivate setup example.\n\n## Usage\n\nFull documentation.\n",
                          encoding="utf-8")
        with (self.root / "reql.conf").open("a", encoding="utf-8") as config:
            config.write("compile:\n  ingest_documents: true\n  documents:\n    markdown: true\n")
        self._claimed_task()
        with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
            service = graph.query_context_service
            request = QueryContextRequest.from_raw(text="total", scopes=["code"], task_id=self.task)
            result = service.execute(request, brief=True)
            self.assertEqual(result.payload["task_setup_location"]["line_start"], 3)
            self.assertEqual(result.payload["task_setup_location"]["line_end"], 6)
            text = service.render(result, brief=True)
            self.assertIn("`README.md` [3-6] (Quick Start)", text)
            self.assertNotIn("Private setup example", text)
            bound = service.render(result, brief=True, completion_command=f"reql agent finish SUMMARY --task {self.task}")
            self.assertIn("Edit the files directly and finish before refreshing", bound)
            self.assertNotIn("reql edit --plan -", bound)
            self.assertNotIn("task_setup_location", service.execute(request, brief=True, follow_up=True).payload)
            readme.write_text("# Changed\n", encoding="utf-8")
            self.assertNotIn("task_setup_location", service.execute(request, brief=True).payload)

    def _source_revision(self) -> str:
        """Compile the checkout and use its real manifest revision."""
        with closing(MemoryGraph.open(self.storage)) as graph:
            compiled = graph.compile_project(self.root)
            self.assertFalse(compiled.run.errors)
            revision = RevisionRepository(graph.store).latest(compiled.scan.project.id)
            self.assertIsNotNone(revision)
            return revision.id

    def test_follow_up_retrieves_code_without_repeating_task_source_or_checks(self) -> None:
        """A later narrow query uses the existing brief and still retrieves another owner."""
        (self.root / "routing.py").write_text("def shipment_manifest():\n    return 'outbound'\n", encoding="utf-8")
        self._claimed_task()
        self._source_revision()
        query = ["query_context", "--task", self.task, "--query", "shipment_manifest", "--code", "--brief"]
        with redirect_stdout(StringIO()) as ordinary:
            self.assertEqual(main(query), 0)
        self.assertIn("test_total.py", ordinary.getvalue())
        with redirect_stdout(StringIO()) as follow_up:
            self.assertEqual(main([*query, "--follow-up"]), 0)
        text = follow_up.getvalue()
        self.assertIn("routing.py", text)
        self.assertNotIn("test_total.py", text)
        self.assertNotIn("Saved checks", text)
        self.assertNotIn("Engineering context", text)

    def test_finish_rejects_removed_options(self) -> None:
        """Accept only the task and summary for a text finish."""
        for option in ("--verification-plan", "--edit-plan", "--source-revision"):
            with self.subTest(option=option), redirect_stderr(StringIO()) as error:
                with self.assertRaises(SystemExit) as rejected:
                    main(["agent", "finish", "Verified", "--task", self.task, option, "value"])
                self.assertEqual(rejected.exception.code, 2)
                self.assertIn(f"unrecognized arguments: {option}", error.getvalue())

    def test_concurrent_new_plan_retains_selection_during_checks(self) -> None:
        """An older execution cannot overwrite new checks or complete the claim."""
        agent = self._claimed_task()
        self.source.write_text("def total():\n    return 3 + 4\n", encoding="utf-8")
        run = verification_module._run_check
        changed = False

        def replacing(root, arguments, environment, timeout):
            nonlocal changed
            if not changed:
                changed = True
                self._save_plan(diff=True)
            return run(root, arguments, environment, timeout)

        with (patch.object(verification_module, "_run_check", side_effect=replacing),
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO())):
            self.assertEqual(main([*agent, "finish", "Verified", "--task", self.task, "--json"]), 2)
        outcome = json.loads(output.getvalue())
        self.assertIn("Saved verification plan changed", outcome["error"]["message"])
        self.assertIn("return 3 + 4", self.source.read_text(encoding="utf-8"))
        self.assertEqual(self.evidence.verification(str(self.root), self.task)["patterns"], [])
        self.assertEqual(self.work.show(self.task)["status"], "in_progress")

    def test_changed_check_plan_after_refresh_leaves_task_open(self) -> None:
        """Recheck the observed selection at the task write as well as receipt publication."""
        agent = self._claimed_task()

        def after_checks(*args):
            self.assertEqual(self._run_checks(diff=True)["status"], "passed")
            return {"status": "completed", "files_changed": 0, "files_seen": 2, "errors": []}

        with (patch("memory.cli._refresh_project", side_effect=after_checks), redirect_stdout(StringIO()) as output):
            self.assertEqual(main([*agent, "finish", "Verified", "--task", self.task, "--json"]), 2)
        self.assertIn("Saved verification plan changed", json.loads(output.getvalue())["error"]["message"])
        self.assertEqual(self.work.show(self.task)["status"], "in_progress")

    def test_failing_result_after_reuse_blocks_completion(self) -> None:
        """A newer failure for the same check selection supersedes a cached pass."""
        agent = self._claimed_task()

        def after_checks(*args):
            receipt = self.evidence.verification(str(self.root), self.task)
            self.evidence.record_verification({**receipt, "checks": [{"name": "test_total.py", "passed": False}]})
            return {"status": "completed", "files_changed": 0, "files_seen": 2, "errors": []}

        with (patch("memory.cli._refresh_project", side_effect=after_checks), redirect_stdout(StringIO()) as output):
            self.assertEqual(main([*agent, "finish", "Verified", "--task", self.task, "--json"]), 2)
        self.assertIn("Saved verification evidence changed", json.loads(output.getvalue())["error"]["message"])
        self.assertEqual(self.work.show(self.task)["status"], "in_progress")

    def test_standalone_edit_preserves_source_and_finish_closes_the_claim(self) -> None:
        """Apply an indexed edit before finishing against the task's saved checks."""
        self.source.write_bytes(b"def total():\r\n    return 7\r\n")
        agent = self._claimed_task()
        self._save_plan(patterns=("test_total.py",))
        plan = [{"path": "invoice.py", "find": "    return 7", "text": "    return 3 + 4"}]
        with patch("sys.stdin", StringIO(json.dumps(plan))), redirect_stdout(StringIO()) as edited:
            self.assertEqual(main(["edit", "--plan", "-", "--json"]), 0)
        self.assertEqual(json.loads(edited.getvalue())["changed"], ["invoice.py"])
        self.assertEqual(self.source.read_bytes(), b"def total():\r\n    return 3 + 4\r\n")
        with redirect_stdout(StringIO()) as finished:
            self.assertEqual(main([*agent, "finish", "Verified", "--task", self.task, "--json"]), 0)
        result = json.loads(finished.getvalue())
        self.assertTrue(all(check["passed"] for check in result["verification"]["checks"]))
        self.assertEqual(result["verification"]["refresh"]["status"], "completed")
        self.assertEqual(result["completed_task"]["revision"], 3)

    def test_batch_reuses_only_matching_inputs_including_ignored_files(self) -> None:
        """Changing an ignored input or a diff baseline invalidates the saved result."""
        first = self._run_checks(patterns=("test_total.py",), diff=True, extra_inputs=("local.ini",))
        self.assertEqual(first["checks"], [
            {"name": "test_total.py", "tests": 1, "passed": True},
            {"name": "REQL source check", "passed": True},
        ])
        self.assertFalse(first["reused"])
        self.assertIsNotNone(first["receipt"])
        self._source_revision()
        updated = self._run_checks()
        self.assertFalse(updated["reused"])
        self.assertNotEqual(updated["receipt"]["source_check_revision"], first["receipt"]["source_check_revision"])
        with patch("memory.testing.verification._run_check", side_effect=AssertionError("must reuse")):
            self.assertTrue(self._run_checks()["reused"])
        self.local_input.write_text("mode = 2\n", encoding="utf-8")
        self.assertIsNone(self._matching_pass())
        self.assertEqual(self._run_checks()["status"], "passed")
        self.assertNotEqual(self._matching_pass()["fingerprint"],
                            first["receipt"]["fingerprint"])

    def test_verification_tracks_added_binary_inputs_and_project_exclusions(self) -> None:
        """Receipt identity follows the native file selection, including non-text test inputs."""
        result = self._run_checks(diff=True)
        self.assertEqual(result["status"], "passed")
        self.local_input.write_text("mode = 2\n", encoding="utf-8")
        self.assertIsNotNone(self._matching_pass())
        binary = self.root / "input.bin"
        binary.write_bytes(b"\x00a")
        self.assertIsNone(self._matching_pass())
        self.assertIsNotNone(self._run_checks()["receipt"])
        binary.unlink()
        self.assertIsNone(self._matching_pass())

    def test_failure_and_empty_collection_cannot_certify_the_task(self) -> None:
        empty = self._run_checks(patterns=("test_missing.py",))
        self.assertEqual(empty["status"], "failed")
        self.assertIsNone(empty["receipt"])
        self.source.write_text("def total():\n    return 9\n", encoding="utf-8")
        failure = self._run_checks(patterns=("test_total.py",))
        self.assertEqual(failure["status"], "failed")
        self.assertIn("AssertionError", failure["checks"][0]["error"])
        self.assertIsNone(self._matching_pass())
        with self.assertRaisesRegex(ValueError, "project-relative"):
            self._run_checks(patterns=("test_total.py",), extra_inputs=("../outside",))

    def test_cargo_receipt_reuse_checks_toolchain_and_ignored_cargo_inputs(self) -> None:
        """Keep Cargo runtime checks explicit and include ignored lock/config files in source identity."""
        manifest = self.root / "Cargo.toml"
        manifest.write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n', encoding="utf-8")
        lock = self.root / "Cargo.lock"
        lock.write_text("version = 4\n", encoding="utf-8")
        summary = "test result: ok. 21 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out"
        external = self.root.parent / "shared"
        directories = {"target_directory": str(external / "target"), "build_directory": str(external / "build")}

        def execute(toolchain: str, *, saved: bool = False) -> dict:
            with patch("memory.testing.verification._cargo_runner", return_value=toolchain), \
                    patch("memory.testing.verification._tool_path", return_value="cargo"), \
                    patch("memory.testing.verification._cargo_metadata", return_value=directories), \
                    patch("memory.testing.verification._run_check", return_value=(0, summary)) as runner:
                payload = self._run_checks() if saved else self._run_checks(cargo=("barrier",), diff=True)
            self.assertEqual(payload["status"], "passed")
            if payload["reused"]:
                runner.assert_not_called()
            else:
                self.assertEqual(runner.call_args_list[0].args[1],
                                 ["cargo", "test", "--locked", "--offline", "--lib", "--color", "never", "barrier"])
                env = runner.call_args_list[0].args[2]
                target = Path(env["CARGO_TARGET_DIR"])
                build = Path(env["CARGO_BUILD_BUILD_DIR"])
                self.assertEqual(target.parent, external / "target" / "reql-workspaces")
                self.assertEqual(build.parent, external / "build" / "reql-workspaces")
                self.assertEqual(target.name, build.name)
            return payload

        first = execute("cargo-toolchain-1")
        self.assertEqual(first["checks"][0], {"name": "Cargo library tests (barrier)", "passed": True, "tests": 21})
        self.assertEqual(first["receipt"]["cargo"], ["barrier"])
        self._source_revision()
        self.assertFalse(execute("cargo-toolchain-1", saved=True)["reused"])
        self.assertTrue(execute("cargo-toolchain-1", saved=True)["reused"])
        self.assertFalse(execute("cargo-toolchain-1")["reused"])
        self.assertFalse(execute("cargo-toolchain-2", saved=True)["reused"])
        with (closing(MemoryGraph.open(self.storage)) as graph,
              patch("memory.testing.verification._run_check", side_effect=AssertionError("must not spawn a toolchain"))):
            self.assertFalse(graph.compile_project(self.root).run.errors)
            request = QueryContextRequest.from_raw(text="total", scopes=["code"], task_id=self.task)
            result = graph.query_context_service.execute(request, brief=True)
            self.assertFalse(result.payload["verified_checks"]["runtime_checked"])
            self.assertEqual(result.payload["verification_plan"]["cargo"], ["barrier"])
            self.assertIn("`reql project compile`", graph.query_context_service.render(result, brief=True))
        lock.write_text("version = 3\n", encoding="utf-8")
        self.assertIsNone(self._matching_pass())
        execute("cargo-toolchain-2")
        manifest.write_text('[package]\nname = "fixture"\nversion = "0.2.0"\n', encoding="utf-8")
        self.assertIsNone(self._matching_pass())

    def test_cargo_empty_results_and_option_like_filters_cannot_pass(self) -> None:
        """A zero-test Cargo run and untrusted option-like input must not certify work."""
        with self.assertRaisesRegex(ValueError, "Cargo.toml at the project root"):
            self._run_checks(cargo=("",))
        (self.root / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n', encoding="utf-8")
        with patch("memory.testing.verification._cargo_runner", return_value="cargo-toolchain"), \
                patch("memory.testing.verification._tool_path", return_value="cargo"), \
                patch("memory.testing.verification._cargo_metadata", return_value={"target_directory": str(self.root / "target")}), \
                patch("memory.testing.verification._run_check",
                      return_value=(0, "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured")):
            result = self._run_checks()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["checks"][0]["tests"], 0)
        self.assertIsNone(self._matching_pass())
        with self.assertRaisesRegex(ValueError, "literal test filter"):
            self._run_checks(cargo=("--help",))

    def test_cargo_integration_plan_survives_a_red_baseline_and_guides_finish(self) -> None:
        """Use a named target as executable acceptance evidence, independent of library checks."""
        manifest = self.root / "Cargo.toml"
        manifest.write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n'
                            '[[test]]\nname = "contract"\npath = "tests/acceptance.rs"\n', encoding="utf-8")
        test = self.root / "tests" / "acceptance.rs"
        test.write_text("#[test]\nfn api_contract() { assert_eq!(7, 7); }\n", encoding="utf-8")
        metadata = {"target_directory": str(self.root / "target"), "build_directory": str(self.root / "target"),
                    "packages": [{"manifest_path": str(manifest), "targets": [
            {"name": "contract", "kind": ["test"], "src_path": str(test)},
        ]}]}
        passed = False
        calls = []

        def cargo_command(root, args, environment, timeout, **options):
            calls.append(args)
            if args[1] == "metadata":
                self.assertIn("--offline", args)
                self.assertGreater(options["complete_output_limit"], 0)
                return 0, json.dumps(metadata)
            self.assertEqual(args, ["cargo", "test", "--locked", "--offline", "--test", "contract", "--color", "never"])
            return (0, "test result: ok. 1 passed; 0 failed;") if passed else (101, "unresolved API")

        with (patch.object(verification_module, "_tool_path", return_value="cargo"),
              patch.object(verification_module, "_cargo_runner", return_value="toolchain"),
              patch.object(verification_module, "_run_check", side_effect=cargo_command)):
            rejected = self._run_checks(cargo_test=("contract",))
            self.assertEqual(rejected["status"], "failed")
            self.assertIsNone(rejected["receipt"])
            plan = verification_plan(self.evidence.verification(str(self.root), self.task))
            self.assertEqual(plan["cargo_test"], ["contract"])
            previous_calls = len(calls)
            with closing(MemoryGraph.open(self.storage)) as graph:
                self.assertFalse(graph.compile_project(self.root).run.errors)
                request = QueryContextRequest.from_raw(text="total", scopes=["code"], task_id=self.task)
                result = graph.query_context_service.execute(request, brief=True)
                self.assertNotIn("verified_checks", result.payload)
                failure = result.payload["baseline_failure"]
                self.assertEqual(failure["checks"], [{"name": "Cargo integration test (contract)", "tests": 0,
                                                     "passed": False}])
                self.assertFalse(failure["runtime_checked"])
                self.assertEqual(result.payload["verification_test_source"]["path"], "tests/acceptance.rs")
                self.assertNotIn("tests/acceptance.rs", result.payload["task_source_paths"])
                text = graph.query_context_service.render(result, brief=True)
                self.assertIn("Saved verification test", text)
                self.assertIn("Recorded failures on matching inputs: Cargo integration test (contract).", text)
                self.assertIn("--test contract", text)
                self.assertEqual(text.count("fn api_contract()"), 1)
                test.write_text("#[test]\nfn changed_contract() {}\n", encoding="utf-8")
                stale = graph.query_context_service.execute(request, brief=True)
                self.assertNotIn("baseline_failure", stale.payload)
                self.assertNotIn("verification_test_source", stale.payload)
                self.assertEqual(stale.payload["verification_plan"]["id"], plan["id"])
                test.write_text("#[test]\nfn api_contract() { assert_eq!(7, 7); }\n", encoding="utf-8")
            self.assertEqual(len(calls), previous_calls)  # A brief never launches Cargo.
            passed = True
            accepted = self._run_checks(expected_plan=plan["id"])
            self.assertEqual(accepted["status"], "passed")
            self.assertEqual(accepted["checks"], [{"name": "Cargo integration test (contract)", "tests": 1, "passed": True}])
            self.assertIsNotNone(self._matching_pass())
            test.write_text("#[test]\nfn changed_contract() {}\n", encoding="utf-8")
            self.assertIsNone(self._matching_pass())

    def test_observed_failure_prevents_reuse_of_an_earlier_success_at_the_same_inputs(self) -> None:
        passing = self._run_checks(patterns=("test_total.py",))["receipt"]
        self.assertIsNotNone(passing)
        failed = {**passing, "checks": [{"name": "test_total.py", "passed": False, "tests": 1}]}
        self.evidence.record_verification(failed)
        self.assertIsNone(self._matching_pass())
        rerun = self._run_checks()
        self.assertFalse(rerun["reused"])
        self.assertEqual(rerun["status"], "passed")

    def test_brief_task_recovers_baseline_until_a_source_file_changes(self) -> None:
        inputs = ("tests/test_total.py",)
        with patch.dict(os.environ, {"PYTHONPATH": str(self.root)}):
            with closing(MemoryGraph.open(self.storage)) as graph:
                self.assertFalse(graph.compile_project(self.root).run.errors)
            with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
                verified = self._run_checks(patterns=("test_total.py",), diff=True, extra_inputs=inputs)
                self.assertEqual(verified["status"], "passed")
                self.assertIsNotNone(verified["receipt"])
                self.assertIsNotNone(self._matching_pass())
                request = QueryContextRequest.from_raw(text="total", scopes=["code"], task_id=self.task)
                result = graph.query_context_service.execute(request, brief=True)
                self.assertIn("verified_checks", result.payload,
                              (result.freshness, result.source_revision, result.payload.get("engineering_context")))
                self.assertEqual(result.payload["verified_checks"]["checks"], verified["checks"])
                self.assertEqual(result.payload["verification_test_source"]["path"], inputs[0])
                self.assertNotIn(inputs[0], result.payload["task_source_paths"])
                text = graph.query_context_service.render(result, brief=True)
                self.assertIn("Saved verification test", text)
                self.assertIn("Verified baseline on matching source", text)
                self.assertIn("`reql project compile`", text)
                untouched = graph.query_context_result(request).payload
                self.assertNotIn("verified_checks", untouched)
                with patch.dict(os.environ, {"VERIFICATION_TEST_RUNTIME": "changed"}):
                    prior = self._matching_pass()
                    self.assertFalse(prior["environment_matches"])
                    rerun = self._run_checks()
                    self.assertFalse(rerun["reused"])
                self.source.write_text("def total():\n    return 8\n", encoding="utf-8")
                updated = graph.query_context_service.execute(request, brief=True)
                self.assertNotIn("verified_checks", updated.payload)
                self.assertNotIn("verification_test_source", updated.payload)
                self.assertEqual(updated.payload["verification_plan"]["patterns"], ["test_total.py"])
                self.assertEqual(updated.payload["verification_plan"]["id"], result.payload["verification_plan"]["id"])
                text = graph.query_context_service.render(updated, brief=True)
                self.assertNotIn("Verified baseline on matching source", text)
                self.assertIn("`reql project compile`", text)

    def test_assigned_brief_points_to_the_recorded_failing_assertion(self) -> None:
        """Reuse the established failure location and retain the saved verification after it becomes stale."""
        path = "tests/test_total.py"
        self.source.write_text("def total():\n    return 8\n", encoding="utf-8")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
        with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
            red = self._run_checks(patterns=("test_total.py",), diff=True, extra_inputs=(path,))
            self.assertEqual(red["status"], "failed")
            request = QueryContextRequest.from_raw(text="total", scopes=["code"], task_id=self.task)
            service = graph.query_context_service
            with patch("memory.testing.verification._run_check", side_effect=AssertionError("Retrieval must not run checks")):
                result = service.execute(request, brief=True)
                failure = result.payload["baseline_failure"]["checks"][0]["failure"]
                self.assertEqual(failure, {"case": "test_total.TotalTest.test_total", "outcome": "FAIL",
                                           "path": path, "line": 5})
                command = "reql agent finish SUMMARY"
                bound = service.render(result, brief=True, completion_command=command)
                self.assertIn("Recorded baseline on matching inputs: test_total.py (failed), REQL source check (passed).", bound)
                self.assertIn("Recorded failure: `TotalTest.test_total` at `tests/test_total.py:5`.", bound)
                self.assertNotIn("unittest discover", bound)
                self.assertIn("unittest discover", service.render(result, brief=True))
                self.assertIn(command, bound)
                self.assertNotIn(str(self.root), bound)
                self.source.write_text("def total():\n    return 7\n", encoding="utf-8")
                current = service.execute(request, brief=True).payload
                self.assertNotIn("baseline_failure", current)
                self.assertEqual(current["verification_plan"], result.payload["verification_plan"])

    def test_project_compile_updates_the_graph_without_running_checks(self) -> None:
        """The standalone verify command is gone and compile still builds the graph."""
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with redirect_stderr(StringIO()) as error, self.assertRaises(SystemExit) as rejected:
            main(["verify", "--help"])
        self.assertEqual(rejected.exception.code, 2)
        self.assertIn("invalid choice: 'verify'", error.getvalue())
        self.assertFalse(self.storage.exists())
        with (patch.object(verification_module, "_run_check", side_effect=AssertionError("compile runs no checks")),
              redirect_stdout(StringIO())):
            self.assertEqual(main(["project", "compile"]), 0)
        with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
            self.assertIn("invoice.py", graph.project_history(self.root)[0].manifest)

    def test_changed_inputs_prevent_recording_a_passing_receipt(self) -> None:
        """A successful subprocess is insufficient if its declared inputs changed."""

        def edit_during_check(*args) -> tuple[int, str]:
            self.source.write_text("def total():\n    return 8\n", encoding="utf-8")
            return 0, "Ran 1 test in 0.01s\nOK"

        with patch("memory.testing.verification._run_check", side_effect=edit_during_check):
            result = self._run_checks(patterns=("test_total.py",))
        self.assertEqual(result["status"], "passed")
        self.assertIsNone(result["receipt"])
        self.assertIsNone(self._matching_pass())

    def test_compile_checks_checkpoint_only_after_applying_changes(self) -> None:
        """An already large WAL does not cause two full checkpoints in one compile."""
        self._source_revision()
        self.source.write_text("def total():\n    return 6 + 1\n", encoding="utf-8")
        checkpoint = BlockGraphStore.checkpoint_if_needed
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with (patch.object(BlockGraphStore, "checkpoint_if_needed", autospec=True,
                           side_effect=lambda store: checkpoint(store, wal_bytes_threshold=0)) as writes,
              redirect_stdout(StringIO())):
            self.assertEqual(main(["project", "compile"]), 0)
        self.assertEqual(writes.call_count, 1)
        self.assertFalse(self.storage.with_suffix(".reql.wal").exists())

    def test_finish_reuses_saved_checks_after_refresh_runs_out_of_memory(self) -> None:
        """A failed refresh leaves the task open and the retry reuses its passing checks."""
        agent = self._claimed_task()
        self.source.write_text("def total():\n    return 3 + 4\n", encoding="utf-8")
        args = [*agent, "finish", "--task", self.task, "--json", "--", "-Updated and verified"]
        with (patch("memory.cli._open_write_graph", side_effect=MemoryError),
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO())):
            self.assertEqual(main(args), 1)
        failure = json.loads(output.getvalue())
        self.assertEqual(failure["verification"]["status"], "passed")
        self.assertEqual(failure["verification"]["refresh"]["status"], "failed")
        self.assertIn("Insufficient memory", failure["verification"]["refresh"]["errors"][0])
        self.assertEqual(self.work.show(self.task)["status"], "in_progress")
        command = shlex.split(failure["retry_command"])[1:]
        self.assertEqual(command, [*agent, "finish", "--task", self.task, "--", "-Updated and verified"])
        with (patch.object(verification_module, "_run_check", side_effect=AssertionError("saved checks reran")),
              redirect_stdout(StringIO()) as response):
            self.assertEqual(main([*command[:-2], "--json", *command[-2:]]), 0)
        self.assertTrue(json.loads(response.getvalue())["verification"]["reused"])
        self.assertEqual(self.work.show(self.task)["status"], "done")

    def test_finish_retry_uses_current_saved_checks(self) -> None:
        """A retry reads the current selection and does not reuse a replaced plan."""
        agent = self._claimed_task()
        args = [*agent, "finish", "Verified", "--task", self.task, "--json"]
        with (patch("memory.cli._open_write_graph", side_effect=MemoryError),
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO())):
            self.assertEqual(main(args), 1)
        retry = shlex.split(json.loads(output.getvalue())["retry_command"])[1:]
        self._save_plan(diff=True)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main([*retry, "--json"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual([check["name"] for check in result["verification"]["checks"]],
                         [verification_module.SOURCE_CHECK_NAME])
        self.assertEqual(result["completed_task"]["status"], "done")

    def test_verified_finish_leaves_failed_work_open_and_completes_after_repair(self) -> None:
        """One normal CLI call verifies and refreshes before saving the handoff."""
        agent = self._claimed_task()
        args = [*agent, "finish", "Verified invoice result", "--task", self.task, "--json"]
        self.source.write_text("def total():\n    return 23\n", encoding="utf-8")
        with (patch("memory.cli._refresh_project", side_effect=AssertionError("failed checks cannot refresh")),
              redirect_stdout(StringIO()) as output):
            self.assertEqual(main(args), 1)
        failed = json.loads(output.getvalue())
        self.assertEqual(failed["status"], "verification_failed")
        self.assertFalse(failed["verification"]["checks"][0]["passed"])
        self.assertEqual(self.work.show(self.task)["status"], "in_progress")
        self.assertEqual(self.work.show(self.task)["revision"], 2)
        self.assertIn("return 23", self.source.read_text(encoding="utf-8"))
        self.assertTrue(self.storage.exists())

        self.source.write_text("def total():\n    return 3 + 4\n", encoding="utf-8")
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(args), 0)
        passed = json.loads(output.getvalue())
        self.assertEqual(passed["completed_task"]["status"], "done")
        self.assertEqual(self.work.show(self.task)["revision"], 3)
        self.assertEqual(passed["verification"]["refresh"]["status"], "completed")
        self.assertGreater(passed["verification"]["refresh"]["files_changed"], 0)
        self.assertEqual(self.work.show(passed["checkpoint"]["id"])["status"], "done")
        self.assertTrue(passed["closed_session_id"])

    def test_verified_claim_suggests_finish_using_the_claimed_revision(self) -> None:
        """The supplied command closes the intended explicit session with its saved checks."""
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["project", "compile"]), 0)
            self.assertEqual(self._run_checks(patterns=("test_total.py",))["status"], "passed")
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["agent", "--agent", "agent:owner", "--activity=-verification suite",
                                   "init", "--task", self.task, "--query", "total"]), 0)
        expected = (f"reql agent --agent agent:owner --activity='-verification suite' "
                    f"finish 'SUMMARY' --task {self.task}")
        self.assertIn(expected, output.getvalue())
        self.assertNotIn("--verification-plan", output.getvalue())
        self.assertNotIn("--edit-plan", output.getvalue())
        self.assertIn("reql edit --plan -", output.getvalue())
        self.assertIn(f"--task {self.task} --follow-up", output.getvalue())
        self.assertIn("Pipe a JSON array to stdin, even for one edit.", output.getvalue())
        self.assertIn("{path,find,text}", output.getvalue())
        command = shlex.split(expected)
        with redirect_stdout(StringIO()) as result:
            self.assertEqual(main([*command[1:], "--json"]), 0)
        finished = json.loads(result.getvalue())
        self.assertEqual(finished["completed_task"]["revision"], 3)
        self.assertTrue(finished["verification"]["reused"])

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
        self.assertIn(prereq["id"], text)
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

    def test_verified_finish_rejects_a_changed_task_revision_after_checks(self) -> None:
        """A concurrent reassignment cannot be overwritten by a passing check."""
        agent = self._claimed_task()

        def concurrent_edit(*args) -> dict:
            self.work.put(record_id=self.task, expected_revision=2, next_action="A concurrent revision",
                          agent_id="agent:reviewer", session_id=None)
            return {"status": "completed", "files_changed": 0, "files_seen": 2, "errors": []}

        with (patch("memory.cli._refresh_project", side_effect=concurrent_edit),
              redirect_stdout(StringIO()), redirect_stderr(StringIO())):
            self.assertEqual(main([*agent, "finish", "Verified", "--task", self.task]), 2)
        current = self.work.show(self.task)
        self.assertEqual((current["status"], current["revision"]), ("in_progress", 3))
        self.assertEqual(current["next_action"], "A concurrent revision")
        self.assertFalse(any(row["kind"] == "checkpoint" for row in self.work.records()))

    def test_verified_finish_rejects_source_changed_after_checks(self) -> None:
        """A post-check edit must not inherit a successful task completion."""
        agent = self._claimed_task()

        def concurrent_edit(*args) -> dict:
            self.source.write_text("def total():\n    return 13\n", encoding="utf-8")
            return {"status": "completed", "files_changed": 0, "files_seen": 2, "errors": []}

        with patch("memory.cli._refresh_project", side_effect=concurrent_edit), redirect_stdout(StringIO()) as output:
            self.assertEqual(main([*agent, "finish", "Verified", "--task", self.task,
                                   "--json"]), 1)
        self.assertEqual(json.loads(output.getvalue())["verification"]["refresh"]["status"], "source_changed")
        self.assertEqual(self.work.show(self.task)["revision"], 2)
