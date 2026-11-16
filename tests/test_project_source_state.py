"""Exercise REQL source state through its public entry points."""
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mcp.tools import query_context, reql_project_status
from memory.artifacts.scanner import ProjectScanner
from memory.cli import main
from memory.domain.exceptions import StorageError
from memory.testing.evidence_store import TestEvidenceStore
from memory.testing.verification import run_saved_checks
from reql import MemoryGraph, QueryContextRequest


class ProjectSourceStateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.source = self.root / "service.py"
        self.source.write_text("def dispatch():\n    return 'initial'\n", encoding="utf-8")
        self.storage = self.root / ".reql" / "memory.reql"
        self.request = QueryContextRequest.from_raw(text="dispatch", scopes=["code"], include_source=True)

    def test_status_and_source_verification_read_native_manifest(self) -> None:
        """Detect additions, deletions and same-size edits without an external process."""
        deleted = self.root / "old.py"
        deleted.write_text("VALUE = 1\n", encoding="utf-8")
        with (patch.dict(os.environ, {"PATH": "", "REQL_MCP_ALLOWED_ROOTS": str(self.root)}),
              patch("subprocess.Popen", side_effect=AssertionError("source state must stay in process"))):
            with closing(MemoryGraph.open(self.storage)) as graph:
                self.assertEqual(graph.workspace_status(self.root)["freshness"], "unknown")
                revision = graph.compile_project(self.root).revision
                self.assertIsNotNone(revision)
                self.assertEqual(graph.workspace_status(self.root)["freshness"], "current")
                original_mtime = self.source.stat().st_mtime_ns
                self.source.write_text("def dispatch():\n    return 'changed'\n", encoding="utf-8")
                os.utime(self.source, ns=(original_mtime, original_mtime))
                deleted.unlink()
                (self.root / "new.py").write_text("VALUE = 2\n", encoding="utf-8")
                status = graph.workspace_status(self.root)
                self.assertEqual(status["freshness"], "stale")
                self.assertEqual(status["source_revision"], revision.id)
                self.assertEqual({row["path"]: row["status"] for row in status["changes"]},
                                 {"service.py": "modified", "old.py": "deleted", "new.py": "added"})
                result = graph.query_context_result(self.request)
                self.assertEqual(result.freshness.status, "stale")
                self.assertFalse(any(row["source_status"] == "verified" for row in result.payload["snippets"]))
            status = reql_project_status(storage_path=str(self.storage), path=str(self.root))
            self.assertEqual(status["workspace"]["freshness"], "stale")
            result = query_context(storage_path=str(self.storage), query="dispatch", code=True, include_source=True)
            self.assertEqual(result["freshness"]["status"], "stale")

            with (redirect_stdout(StringIO()) as output,
                  patch.object(ProjectScanner, "scan", autospec=True, side_effect=ProjectScanner.scan) as scan):
                self.assertEqual(main(["stats", "--json"]), 0)
            scan.assert_called_once()
            self.assertTrue(status["found"])
            stats = json.loads(output.getvalue())
            self.assertEqual(stats["project"],
                             {key: value for key, value in status.items() if key not in {"found", "workspace"}})
            self.assertEqual(stats["workspace"], status["workspace"])
            self.assertEqual(stats["nodes"], sum(stats["node_types"].values()))
            self.assertGreater(stats["edges"], 0)
            self.assertEqual(stats["cache"]["total_artifacts"], 2)
            self.assertEqual(stats["cache"]["cached_artifacts"], 0)
            self.assertEqual(stats["cache"]["dirty_artifacts"], 2)
            self.assertEqual(stats["cache"]["deleted_artifacts"], 1)
            self.assertNotIn("project", stats["cache"])
            self.assertEqual(stats["tests"]["observed"], 0)
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(["stats"]), 0)
            self.assertIn("Source: stale", output.getvalue())
            self.assertIn("modified\tservice.py", output.getvalue())
            self.assertIn("Cached: 0, dirty: 2, deleted: 1", output.getvalue())
            self.assertIn("Test evidence", output.getvalue())
            with closing(MemoryGraph.open(self.storage)) as graph:
                result = graph.compile_project(self.root)
                self.assertFalse(result.run.errors)
                self.assertEqual(graph.workspace_status(self.root)["freshness"], "current")

    def test_stats_does_not_initialize_storage(self) -> None:
        """Report an uncompiled folder without creating a store."""
        with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as error:
            self.assertEqual(main(["stats", "--json"]), 0)
        self.assertEqual(error.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue()), {
            "nodes": 0, "edges": 0, "node_types": {},
            "project": None, "workspace": None, "cache": None, "tests": None,
        })
        self.assertFalse(self.storage.parent.exists())

    def test_stats_reports_other_storage_errors(self) -> None:
        """Keep storage failures distinct from an uninitialized store."""
        failure = StorageError("Cannot inspect REQL storage at missing REQL storage/sample: permission denied")
        with (patch.object(MemoryGraph, "open", side_effect=failure),
              redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as error):
            self.assertEqual(main(["stats", "--json"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("permission denied", error.getvalue())

    def test_stats_honors_disabled_cache_and_configured_scan_size(self) -> None:
        """Use the configured scan limit for workspace and cache even with caching disabled."""
        config = self.root / "reql.conf"
        settings = 'cache:\n  enabled: false\nscan:\n  include: ["*.py"]\n'
        config.write_text(settings, encoding="utf-8")
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["project", "compile"]), 0)
        config.write_text(settings + "  max_file_size_mb: 0.00001\n", encoding="utf-8")
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["stats", "--json"]), 0)
        stats = json.loads(output.getvalue())
        self.assertEqual(stats["workspace"]["freshness"], "stale")
        self.assertEqual([(change["path"], change["status"]) for change in stats["workspace"]["changes"]],
                         [("service.py", "deleted")])
        self.assertFalse(stats["cache"]["cache_enabled"])
        self.assertEqual(stats["cache"]["total_artifacts"], 0)

    def test_scan_commands_reject_removed_size_override(self) -> None:
        """Use project configuration as the CLI source of the size limit."""
        for command in (["project", "compile"], ["stats"]):
            with self.subTest(command=command), redirect_stderr(StringIO()) as error:
                with self.assertRaises(SystemExit) as rejected:
                    main([*command, "--max-file-size-mb", "5"])
                self.assertEqual(rejected.exception.code, 2)
                self.assertIn("unrecognized arguments: --max-file-size-mb", error.getvalue())
    def test_list_workspaces_api_reads_registered_roots_without_scanning(self) -> None:
        """List only roots in the selected store, including their last compiled revisions."""
        other = TemporaryDirectory()
        self.addCleanup(other.cleanup)
        root = Path(other.name).resolve()
        (root / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
        with (patch.dict(os.environ, {"PATH": ""}),
              patch("subprocess.Popen", side_effect=AssertionError("workspace commands must stay in process"))):
            with closing(MemoryGraph.open(self.storage)) as graph:
                revisions = {path: graph.compile_project(path).revision for path in (self.root, root)}
            with closing(MemoryGraph.open(root / ".reql" / "memory.reql")) as graph:
                graph.compile_project(root)
            self.source.write_text("VALUE = 2\n", encoding="utf-8")
            with patch("memory.artifacts.scanner.ProjectScanner.scan", side_effect=AssertionError("list must not scan")):
                with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
                    rows = graph.list_workspaces()
                self.assertEqual(rows, [
                    {"project_id": revisions[path].project_id, "root_path": path.as_posix(),
                     "name": path.name, "source_revision": revisions[path].id}
                    for path in sorted(revisions, key=Path.as_posix)
                ])
                with closing(MemoryGraph.open(root / ".reql" / "memory.reql", read_only=True)) as graph:
                    self.assertEqual(graph.list_workspaces(), [row for row in rows if row["root_path"] == root.as_posix()])

    def test_scan_uses_reql_exclusions_for_file_membership(self) -> None:
        """Apply both project exclusions and built-in rules during compilation."""
        (self.root / "generated.py").write_text("VALUE = 3\n", encoding="utf-8")
        bundle = self.root / "nested" / "skills" / "reql-agent"
        bundle.mkdir(parents=True)
        (bundle / "SKILL.md").write_text("# Generated instructions\n", encoding="utf-8")
        (self.root / "nested" / "state.sqlite-wal").write_bytes(b"generated")
        (self.root / "reql.conf").write_text('scan:\n  exclude: ["generated.py"]\n', encoding="utf-8")
        with patch("subprocess.Popen", side_effect=AssertionError("scan must stay in process")):
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["project", "compile"]), 0)
            with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
                manifest = graph.project_history(self.root)[0].manifest
                self.assertIn("service.py", manifest)
                self.assertNotIn("generated.py", manifest)
                self.assertNotIn("nested/skills/reql-agent/SKILL.md", manifest)
                self.assertNotIn("nested/state.sqlite-wal", manifest)

    def test_incomplete_live_scan_reports_unknown_without_deletions(self) -> None:
        """An unreadable source cannot establish changes to the manifest."""
        with closing(MemoryGraph.open(self.storage)) as graph:
            revision = graph.compile_project(self.root).revision
            with patch("memory.artifacts.scanner.fingerprint_file", side_effect=OSError("read denied")):
                status = graph.workspace_status(self.root)
            self.assertEqual(status["freshness"], "unknown")
            self.assertEqual(status["source_revision"], revision.id)
            self.assertEqual(status["changes"], [])
            self.assertEqual(status["errors"][0]["relative_path"], "service.py")
        with (patch("memory.artifacts.scanner.fingerprint_file", side_effect=OSError("read denied")),
              redirect_stdout(StringIO()) as output):
            self.assertEqual(main(["stats", "--json"]), 0)
        stats = json.loads(output.getvalue())
        self.assertEqual(stats["workspace"], status)
        self.assertIsNone(stats["cache"])

    def test_native_diff_checks_added_and_modified_text(self) -> None:
        """Only changed project files are checked against the last native revision."""
        self.source.write_bytes(b"VALUE = 1 \r\n")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertIsNotNone(graph.compile_project(self.root).revision)
        added = self.root / "new.py"
        added.write_bytes(b"VALUE = 2\t\r\n<<<<<<< edit\r\n")
        evidence = TestEvidenceStore(self.storage)
        task_id = "task:source-check"
        evidence.record_verification({"root": str(self.root), "task_id": task_id, "patterns": [], "diff": True,
                                      "extra_inputs": [], "fingerprint": "", "checks": []})
        with patch("subprocess.Popen", side_effect=AssertionError("source checks must stay in process")):
            result = run_saved_checks(self.root, evidence, task_id=task_id, timeout=20)
            self.assertEqual(result["status"], "failed")
            check = result["checks"][0]
            self.assertIn("new.py:1: trailing whitespace", check["error"])
            self.assertIn("new.py:2: unresolved conflict marker", check["error"])
            self.assertNotIn("service.py", check["error"])
            added.write_bytes(b"VALUE = 2\r\n")
            result = run_saved_checks(self.root, evidence, task_id=task_id, timeout=20)
            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["checks"][0]["passed"])
