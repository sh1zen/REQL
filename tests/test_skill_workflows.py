"""Exercise installed skill workflows through real compilation and CLI processes."""
from __future__ import annotations

import json
import os
import re
import shlex
from argparse import ArgumentParser
from contextlib import closing
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agents.clients import CLIENT_PROFILES
from agents.install import HOOK_ID, install_agent_files, uninstall_agent_files
from api import MemoryGraph
from config import VERSION
from memory.artifacts.project import ProjectRegistry
from memory.artifacts.scanner import ProjectScanner
from memory.cli import build_parser
from memory.query import parse_reql


class SkillWorkflowTests(unittest.TestCase):
    """Protect failures found during the isolated skill evaluation."""

    def setUp(self) -> None:
        scratch = Path(".tmp")
        scratch.mkdir(exist_ok=True)
        self.temp = TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "invoice service"
        self.root.mkdir()
        self.storage = self.root / ".reql" / "memory.reql"

    def write(self, relative: str, content: str) -> None:
        """Keep source and generated evaluation artifacts within the temporary root."""
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def test_status_distinguishes_registration_failed_compile_and_recovery(self) -> None:
        self.write("invoices.py", "def settle_invoice(amount):\n    return amount > 0\n")
        for index in range(6):
            self.write(f"batch_{index}.py", "value = 1\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertIsNone(graph.project_status(self.root))
            ProjectRegistry(graph.store).scan_path(self.root)
            self.assertIsNone(graph.project_status(self.root)["last_compilation"])
            with patch("memory.code_analysis.base.TreeSitterCodeParser.parse_artifact",
                       side_effect=ModuleNotFoundError("missing parser dependency")):
                failed = graph.compile_project(self.root)
            self.assertEqual(failed.run.status, "failed")
            status = graph.project_status(self.root)
            self.assertEqual(status["project"]["status"], "active")
            self.assertEqual(status["last_compilation"]["status"], "failed")
            self.assertEqual(status["last_compilation"]["id"], failed.run.id)
            self.assertEqual(status["last_compilation"]["error_count"], 7)
            self.assertEqual(len(status["last_compilation"]["errors"]), 5)
            self.assertIn("missing parser dependency", status["last_compilation"]["errors"][0])
            # Another project's newer run must not mask this project's failure.
            other = self.root / "other"
            other.mkdir()
            (other / "other.py").write_text("value = 1\n", encoding="utf-8")
            graph.compile_project(other)
            self.assertEqual(graph.project_status(self.root)["last_compilation"]["id"], failed.run.id)
            recovered = graph.compile_project(self.root)
            status = graph.project_status(self.root)
            self.assertEqual(status["last_compilation"]["status"], "completed")
            self.assertEqual(status["last_compilation"]["id"], recovered.run.id)
            self.assertEqual(status["last_compilation"]["error_count"], 0)
            self.assertEqual(status["last_compilation"]["errors"], [])

    def test_installed_command_supports_bootstrap_question_resolution_and_recovery(self) -> None:
        """Recover questions and file-scoped failures through installed commands across sessions."""
        self.write("invoices.py", "def settle_invoice(amount):\n    return amount > 0\n")
        test_paths = {"qa/test_invoices.py", "packages/billing/invoices_test.py", "tests/test_contract.py"}
        for path in sorted(test_paths):
            self.write(path, "from invoices import settle_invoice\n\ndef test_positive_amount():\n    assert settle_invoice(10)\n")
        self.write("docs/requirements.md", "# Invoice settlement\nOPEN: Does the budget include the first attempt?\n")
        installed = install_agent_files(("codex", "claude"), project=True, project_dir=self.root,
                                        command_dir=self.root.parent / "command tools", hooks=False)
        command = next(item.path for item in installed.actions if item.kind == "command")
        self.write(".claude/skills/team-conventions/SKILL.md", "# Maintained team conventions\n")
        paths = {artifact.relative_path for artifact in ProjectScanner().scan(self.root).artifacts}
        self.assertFalse(any("skills/reql-agent/" in path for path in paths))
        self.assertIn(".claude/skills/team-conventions/SKILL.md", paths)
        self.assertIn("AGENTS.md", paths)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")

        def run(*args: str, expected: int = 0) -> str:
            """Invoke the real installed shim, including executable paths containing spaces."""
            result = subprocess.run([str(command), *args], cwd=self.root, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            return result.stdout

        self.assertIsNone(json.loads(run("stats", "--json"))["project"])
        run("project", "compile")
        self.assertIn("Last compilation: completed", run("stats"))
        stats = json.loads(run("stats", "--json"))
        self.assertEqual(stats["project"]["last_compilation"]["status"], "completed")
        self.assertEqual(stats["project"]["last_compilation"]["error_count"], 0)
        self.assertEqual(stats["workspace"]["freshness"], "current")
        views = json.loads(run("query_explore", "--query", "settle_invoice",
                               "--view", "owners", "--view", "callers", "--json"))
        self.assertEqual(set(views["sections"]), {"owners", "callers"})
        html = self.root.parent / "pipeline.html"
        self.assertEqual(Path(run("project", "pipeline", "--out", str(html)).strip()), html)
        self.assertTrue(html.is_file())
        missing = json.loads(run("locate", "no-such-path.py", "--json", expected=1))
        self.assertEqual(missing["matches"], [])
        context = json.loads(run("query_context", "--query", "settle_invoice", "--code", "--json"))
        self.assertEqual({row["path"] for row in context["payload"]["test_targets"]}, test_paths)
        for target in context["payload"]["test_targets"]:
            self.assertEqual(target["kind"], "test")
            self.assertTrue((self.root / target["path"]).is_file())
            self.assertGreater(target["line_start"], 0)
        self.assertIn("requirements.md", run("query_context", "--query", "docs/requirements.md", "--docs"))
        run("agent", "init", "--name", "Clarify settlement budget")
        question = json.loads(run("agent", "record", "question", "Does the budget include the first attempt?",
                                  "--file", "invoices.py", "--next", "Ask product", "--json"))["node"]
        resolved = json.loads(run("agent", "record", "question", question["content"], "--id", question["id"],
                                  "--revision", str(question["revision"]), "--status", "resolved",
                                  "--why", "Confirmed: three total attempts", "--next", "", "--json"))["node"]
        self.assertEqual(resolved["next_action"], "")
        failure = json.loads(run("agent", "record", "failure", "Collapse duplicate guards",
                                 "--why", "test_contract rejects omitted keys",
                                 "--file", "invoices.py", "--file", "tests/test_contract.py", "--json"))["node"]
        run("agent", "finish", "Budget clarified; implementation remains")
        fresh = json.loads(run("agent", "--agent", "agent:fresh", "init",
                               "--name", "Review current contracts", "--json"))
        self.assertNotEqual(fresh["agent_id"], failure["agent_id"])
        recovered = json.loads(run("project", "context", "--file", "invoices.py", "--json"))
        retained = next(item for item in recovered["records"] if item["id"] == question["id"])
        self.assertEqual(retained["status"], "resolved")
        self.assertEqual(retained["next_action"], "")
        self.assertIn("three total attempts", retained["rationale"])
        for path in ("invoices.py", "tests/test_contract.py"):
            scoped = json.loads(run("project", "context", "--file", path, "--json"))
            remembered = next(item for item in scoped["records"] if item["id"] == failure["id"])
            self.assertEqual(remembered["content"], failure["content"])
            self.assertEqual(remembered["rationale"], failure["rationale"])
            self.assertEqual(remembered["status"], "active")
            self.assertIn("file", remembered["reasons"])
            self.assertIn(failure["rationale"], run("project", "context", "--file", path))
        unrelated = json.loads(run("project", "context", "--file", "docs/requirements.md", "--json"))
        self.assertNotIn(failure["id"], {item["id"] for item in unrelated["records"]})
        current = json.loads(run("query_context", "--query", "settle_invoice", "--code", "--json"))
        attached = next(item for item in current["payload"]["engineering_context"]["records"]
                        if item["id"] == failure["id"])
        self.assertIn("file", attached["reasons"])
        self.assertIn(failure["rationale"], run("query_context", "--query", "settle_invoice", "--code"))
        run("agent", "--agent", "agent:fresh", "finish", "Shared history recovered; no changes")
        for platform in ("codex", "claude"):
            bundle = self.root / f".{platform}" / "skills" / "reql-agent"
            self.assertTrue((bundle / "SKILL.md").is_file())
            self.assertTrue((bundle / "references" / "bootstrap.md").is_file())
            self.assertTrue((bundle / "references" / "query-advanced.md").is_file())

    def test_generated_bundles_have_reachable_references_and_valid_command_examples(self) -> None:
        """Check every platform and scope for broken routes and unsupported CLI examples."""
        parser = build_parser()
        for project in (True, False):
            for platform in CLIENT_PROFILES:
                with self.subTest(project=project, platform=platform):
                    root = self.root / platform / ("project" if project else "user")
                    home = root / "home"
                    with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home / ".claude")}):
                        installed = install_agent_files(
                            (platform,), project=project, project_dir=root, home_dir=home,
                            command_dir=root / "bin", hooks=True)
                    skill_names = {action.path.parent.name for action in installed.actions if action.kind == "skill"}
                    expected_skills = set() if platform in {"cursor", "copilot"} else {"reql-agent"}
                    self.assertEqual(skill_names, expected_skills)
                    stamps = [action.path for action in installed.actions if action.kind == "version"]
                    self.assertEqual(len(stamps), 1)
                    stamp = json.loads(stamps[0].read_text(encoding="utf-8"))
                    self.assertEqual(stamp["package_version"], VERSION)
                    for action in installed.actions:
                        if action.kind == "hook":
                            self.check_hook_output(action.path, platform=platform, root=root)
                        if action.kind == "skill":
                            self.check_bundle(action.path, parser=parser)
                        if action.kind in {"instructions", "rule"}:
                            self.check_discovery_policy(action.path.read_text(encoding="utf-8"))

    def test_installed_guidance_uses_context_before_preflights(self) -> None:
        """Keep automatic instructions bounded and leave setup details on demand."""
        root = self.root / "compact"
        installed = install_agent_files(
            ("codex",), project=True, project_dir=root, home_dir=root / "home", command_dir=root / "bin",
        )
        guidance = (root / "AGENTS.md").read_text(encoding="utf-8")
        skill = next(action.path for action in installed.actions
                     if action.kind == "skill" and action.path.parent.name == "reql-agent")
        route = skill.read_text(encoding="utf-8")
        self.assertLess(len(guidance), 900)
        self.assertIn("--brief", guidance)
        self.assertNotIn("project status", guidance)
        self.assertNotIn("agent dashboard", route)
        self.assertIn("references/bootstrap.md", route)
        self.assertIn('agent init --task ID --query "literal terms"', route)
        self.assertIn("project context --task ID", route)
        workspace = (skill.parent / "references" / "agent-workspace.md").read_text(encoding="utf-8")
        self.assertIn("query_context --task TASK_ID --follow-up", workspace)
        self.assertIn('--query "DOCUMENT_PATH_OR_HEADING" --docs --brief', workspace)
        self.assertIn("`--seen-source`", workspace)
        self.assertIn("`storage locks`", workspace)
        self.assertIn("`agent record --id ID --revision N`", workspace)
        plan = json.loads(workspace.split("\n```json\n", 1)[1].split("\n```", 1)[0])
        check, = plan["focused"]
        self.assertEqual(check["command"][:3], ["python", "-m", "unittest"])
        self.assertIn("tests/test_serializer.py", check["inputs"])
        self.assertIn("agent init --next", workspace)
        self.assertIn("agent task update --id TASK_ID --status open", workspace)
        self.assertIn("project context --task WORK_ID", workspace)
        self.assertIn("project overview", workspace)
        self.assertIn("private session", workspace)
        mcp = (skill.parent / "references" / "reports-exports.md").read_text(encoding="utf-8")
        self.assertIn("reql_work_claim_next", mcp)
        self.assertIn("reql_work_context", mcp)

    def test_install_and_uninstall_preserve_local_skill_references(self) -> None:
        """Manage the current generated reference while preserving local guidance."""
        reference_dir = self.root / ".codex" / "skills" / "reql-agent" / "references"
        reference_dir.mkdir(parents=True)
        generated = reference_dir / "graph-updates.md"
        local = reference_dir / "local.md"
        local.write_text("Local guide", encoding="utf-8")
        settings = {"project": True, "project_dir": self.root, "command_dir": self.root / "bin"}

        actions = install_agent_files(("codex",), dry_run=True, **settings).actions
        self.assertEqual(next(action.status for action in actions if action.path == generated), "would-create")
        self.assertFalse(generated.exists())

        install_agent_files(("codex",), **settings)
        self.assertTrue(generated.is_file())
        self.assertEqual(local.read_text(encoding="utf-8"), "Local guide")

        uninstall_agent_files(("codex",), **settings)
        self.assertFalse(generated.exists())
        self.assertEqual(local.read_text(encoding="utf-8"), "Local guide")

    def test_install_updates_startup_hook_and_preserves_user_hooks(self) -> None:
        """Reinstall and uninstall manage only REQL's startup command."""
        for platform, user_event in (("claude", "PreToolUse"), ("gemini", "BeforeTool")):
            with self.subTest(platform=platform):
                root = self.root / platform
                path = root / f".{platform}" / "settings.json"
                managed = {"type": "command", "command": f"echo {HOOK_ID}"}
                shared = {"type": "command", "command": "echo custom"}
                startup = {"matcher": "startup", "hooks": [managed, shared]}
                other = {"matcher": "CustomTool", "hooks": [shared]}
                original = {"hooks": {user_event: [other], "SessionStart": [startup]}}
                self.write(f"{platform}/.{platform}/settings.json", json.dumps(original))
                settings = {"project": True, "project_dir": root, "command_dir": root / "bin"}
                planned = install_agent_files((platform,), **settings, dry_run=True)
                self.assertEqual(next(action.status for action in planned.actions if action.kind == "hook"), "would-update")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

                install_agent_files((platform,), **settings)
                config = json.loads(path.read_text(encoding="utf-8"))
                expected = {"hooks": {user_event: [other], "SessionStart": [{**startup, "hooks": [shared]}]}}
                existing_startup, reql_startup = config["hooks"]["SessionStart"]
                self.assertEqual(existing_startup, expected["hooks"]["SessionStart"][0])
                self.assertEqual(config["hooks"][user_event], [other])
                self.assertEqual(reql_startup["matcher"], "startup")
                self.assertIn(HOOK_ID, reql_startup["hooks"][0]["command"])
                repeated = install_agent_files((platform,), **settings)
                self.assertEqual(next(action.status for action in repeated.actions if action.kind == "hook"), "unchanged")

                uninstall_agent_files((platform,), **settings)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)

    def check_discovery_policy(self, text: str) -> None:
        """Require the same fallback boundary on each generated instruction surface."""
        self.assertIn("Use REQL for repository discovery.", text)
        self.assertIn("Use other discovery tools only if REQL cannot run or reports an operational error.", text)
        self.assertIn("Zero results do not count as a failure, including a `locate` miss with exit code `1`.", text)

    def check_bundle(self, skill: Path, *, parser: ArgumentParser) -> None:
        """Traverse installed reference links and parse their executable examples."""
        bundle = skill.parent
        pending = [skill]
        visited = set()
        while pending:
            document = pending.pop()
            if document in visited:
                continue
            visited.add(document)
            text = document.read_text(encoding="utf-8")
            if bundle.name == "reql-agent" and document.name in {"SKILL.md", "query.md"}:
                self.check_discovery_policy(text)
            for reference in re.findall(r"`((?:references/)?[a-z][a-z-]*\.md)`", text):
                target = (document.parent / reference).resolve()
                self.assertTrue(target.is_relative_to(bundle), reference)
                self.assertTrue(target.is_file(), reference)
                pending.append(target)
            for line in text.splitlines():
                if line.startswith("reql "):
                    arguments = shlex.split(line)[1:]
                    arguments = ["1" if value in {"N", "CURRENT_N"} else value for value in arguments]
                    parsed = parser.parse_args(arguments)
                    if parsed.command == "query":
                        parse_reql(" ".join(parsed.statement))
        self.assertTrue(set((bundle / "references").glob("*.md")).issubset(visited))

    def check_hook_output(self, settings_path: Path, *, platform: str, root: Path) -> None:
        """Execute installed shell hooks with and without a REQL project marker."""
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(set(settings["hooks"]), {"SessionStart"})
        group, = settings["hooks"]["SessionStart"]
        self.assertEqual(group["matcher"], "startup")
        hooks = group["hooks"]
        self.assertEqual(len(hooks), 1)
        for hook in hooks:
            absent = subprocess.run(hook["command"], shell=True, cwd=root, capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(absent.returncode, 0, absent.stderr)
            self.assertEqual(absent.stdout, "")
            (root / "reql.conf").write_text("", encoding="utf-8")
            present = subprocess.run(hook["command"], shell=True, cwd=root, capture_output=True,
                                     text=True, timeout=10)
            self.assertEqual(present.returncode, 0, present.stderr)
            payload = json.loads(present.stdout)
            output = payload["hookSpecificOutput"]
            if platform == "claude":
                self.assertEqual(output["hookEventName"], "SessionStart")
            context = output["additionalContext"]
            self.assertIn("Use REQL for repository discovery.", context)
            self.assertLessEqual(len(context), 120)
            (root / "reql.conf").unlink()


if __name__ == "__main__":
    unittest.main()
