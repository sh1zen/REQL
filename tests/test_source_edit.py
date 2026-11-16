"""Exercise revision-bound source edits through the public CLI."""
from codecs import BOM_UTF8
from contextlib import closing, redirect_stderr, redirect_stdout
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from api import MemoryGraph
from memory.artifacts.revision import RevisionRepository
from memory.cli import main
from memory.domain.exceptions import StorageError


class SourceEditTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "project"
        self.root.mkdir()
        self.readme = self.root / "README.md"
        self.code = self.root / "sample.py"
        self.original_readme = BOM_UTF8 + b"# Usage\r\n\r\nRun this.\r\nKeep this.\n"
        self.original_code = b"def run():\n    return 1"
        self.readme.write_bytes(self.original_readme)
        self.code.write_bytes(self.original_code)
        with closing(MemoryGraph.open(self.root / ".reql/memory.reql")) as graph:
            result = graph.compile_project(self.root)
            self.assertFalse(result.run.errors)
            self.project_id = result.scan.project.id
            self.source_revision = RevisionRepository(graph.store).latest(self.project_id).id

    def _edit(self, plan: list[dict] | dict) -> tuple[int, str]:
        """Run the public JSON command and keep the output on refresh failure."""
        with (patch("sys.stdin", StringIO(json.dumps(plan))), redirect_stdout(StringIO()) as out,
              redirect_stderr(StringIO()) as error):
            code = main(["edit", "--path", str(self.root), "--plan", "-", "--json"])
        return code, out.getvalue() or error.getvalue()

    @staticmethod
    def _range(path: str, start: int, end: int, text: str) -> dict:
        return {"path": path, "start_line": start, "end_line": end, "text": text}

    def test_revision_bound_batch_preserves_bom_mixed_endings_and_eof(self) -> None:
        code, output = self._edit([
            self._range("README.md", 3, 3, "Run something new."),
            self._range("README.md", 2, 1, "A new heading note"),
            self._range("sample.py", 2, 2, "    return 2"),
        ])
        self.assertEqual(code, 0, output)
        result = json.loads(output)
        self.assertEqual(result["changed"], ["README.md", "sample.py"])
        self.assertEqual(result["refresh"]["status"], "completed")
        self.assertEqual(result["refresh"]["files_changed"], 2)
        self.assertEqual(self.readme.read_bytes(),
                         BOM_UTF8 + b"# Usage\r\nA new heading note\r\n\r\nRun something new.\r\nKeep this.\n")
        self.assertEqual(self.code.read_bytes(), b"def run():\n    return 2")
        with closing(MemoryGraph.open(self.root / ".reql/memory.reql", read_only=True)) as graph:
            revision = RevisionRepository(graph.store).latest(self.project_id)
        self.assertEqual(revision.parent_id, self.source_revision)
        for path in (self.readme, self.code):
            self.assertEqual(revision.manifest[path.name]["sha256"], sha256(path.read_bytes()).hexdigest())

    def test_successive_edits_use_the_latest_compiled_revision(self) -> None:
        """A second plan uses the revision published by the first edit."""
        for before, after in ((1, 2), (2, 3)):
            code, output = self._edit([
                {"path": "sample.py", "find": f"    return {before}", "text": f"    return {after}"},
            ])
            self.assertEqual(code, 0, output)
            self.assertEqual(json.loads(output)["refresh"]["status"], "completed")
        self.assertEqual(self.code.read_bytes(), b"def run():\n    return 3")

    def test_edit_requires_a_compiled_revision_for_its_own_root(self) -> None:
        """A graph with revisions for another root cannot authorize edits here."""
        other = self.root.parent / "other"
        other.mkdir()
        target = other / self.code.name
        target.write_bytes(self.original_code)
        with closing(MemoryGraph.open(other / ".reql/memory.reql")) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
        plan = json.dumps([self._range(target.name, 2, 2, "    return 2")])
        with patch("sys.stdin", StringIO(plan)), redirect_stderr(StringIO()) as error:
            self.assertEqual(main(["edit", "--path", str(other), "--plan", "-"]), 2)
        self.assertIn("Run `reql project compile` from its root first", error.getvalue())
        self.assertEqual(target.read_bytes(), self.original_code)

    def test_edit_rejects_the_removed_revision_option(self) -> None:
        """Reject the obsolete flag before reading an edit plan."""
        with redirect_stderr(StringIO()) as error, self.assertRaises(SystemExit) as rejected:
            main(["edit", "--plan", "-", "--source-revision", self.source_revision])
        self.assertEqual(rejected.exception.code, 2)
        self.assertIn("unrecognized arguments: --source-revision", error.getvalue())

    def test_no_op_skips_graph_refresh(self) -> None:
        """A plan that writes no files should incur no compilation."""
        with patch.object(MemoryGraph, "compile_project", side_effect=AssertionError("unexpected compile")):
            code, output = self._edit([self._range("sample.py", 2, 2, "    return 1")])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output), {
            "changed": [], "unchanged": ["sample.py"], "refresh": {"status": "skipped"},
        })

    def test_refresh_failure_reports_applied_files(self) -> None:
        """Keep the edit report when compilation fails after source writes."""
        with patch.object(MemoryGraph, "compile_project", side_effect=StorageError("Cannot read project files")):
            code, output = self._edit([self._range("sample.py", 2, 2, "    return 2")])
        self.assertEqual(code, 1, output)
        result = json.loads(output)
        self.assertEqual(result["changed"], ["sample.py"])
        self.assertEqual(result["refresh"]["status"], "failed")
        self.assertIn("Cannot read project files", result["refresh"]["errors"][0])
        self.assertEqual(self.code.read_bytes(), b"def run():\n    return 2")

    def test_refresh_uses_the_edited_project_configuration(self) -> None:
        """Apply the new scan rules in the same command that edits them."""
        config = self.root / "reql.conf"
        config.write_text("scan:\n  exclude: []\n", encoding="utf-8")
        with closing(MemoryGraph.open(self.root / ".reql/memory.reql")) as graph:
            result = graph.compile_project(self.root)
            self.assertFalse(result.run.errors)

        code, output = self._edit([self._range("reql.conf", 2, 2, '  exclude: ["sample.py"]')])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["refresh"]["status"], "completed")
        with closing(MemoryGraph.open(self.root / ".reql/memory.reql", read_only=True)) as graph:
            revision = RevisionRepository(graph.store).latest(self.project_id)
        self.assertNotIn("sample.py", revision.manifest)

    def test_stale_or_invalid_inputs_leave_other_files_untouched(self) -> None:
        self.code.write_bytes(b"def run():\n    return 7")
        first = self._range("README.md", 3, 3, "Should not be written.")
        code, output = self._edit([first, self._range("sample.py", 2, 2, "    return 8")])
        self.assertEqual(code, 2, output)
        self.assertIn("Source changed", output)
        self.assertEqual(self.readme.read_bytes(), self.original_readme)
        code, output = self._edit([first, self._range("../outside.md", 1, 1, "outside")])
        self.assertEqual(code, 2, output)
        self.assertEqual(self.readme.read_bytes(), self.original_readme)
        code, output = self._edit([first, self._range("README.md", 2, 4, "overlap")])
        self.assertEqual(code, 2, output)
        self.assertIn("overlap", output)
        self.assertEqual(self.readme.read_bytes(), self.original_readme)

    def test_deleting_unterminated_last_line_keeps_unedited_bytes(self) -> None:
        code, output = self._edit([self._range("sample.py", 2, 2, "")])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.code.read_bytes(), b"def run():\n")

    def test_single_edit_requires_a_json_array(self) -> None:
        """Point a malformed plan at the supported format before any writes."""
        change = self._range("sample.py", 2, 2, "    return 3")
        code, output = self._edit(change)
        self.assertEqual(code, 2, output)
        self.assertIn("JSON array", output)
        self.assertEqual(self.code.read_bytes(), self.original_code)
        code, output = self._edit([change])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.code.read_bytes(), b"def run():\n    return 3")

    def test_plan_file_is_loaded_only_inside_the_project(self) -> None:
        """Keep project confinement and UTF-8 BOM support for file-based edit plans."""
        document = json.dumps([self._range("sample.py", 2, 2, "    return 2")]).encode("utf-8")
        external = self.root.parent / "plan.json"
        external.write_bytes(document)
        arguments = ["edit", "--path", str(self.root), "--plan"]
        with redirect_stderr(StringIO()) as error:
            self.assertEqual(main([*arguments, str(external), "--json"]), 2)
        self.assertIn("Edit plan must be inside the project", error.getvalue())
        self.assertEqual(self.code.read_bytes(), self.original_code)

        local = self.root / "plan.json"
        local.write_bytes(BOM_UTF8 + document)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main([*arguments, str(local), "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["changed"], ["sample.py"])
        self.assertEqual(self.code.read_bytes(), b"def run():\n    return 2")

    def test_literal_anchors_and_ranges_share_the_original_file(self) -> None:
        """Use exact text from the brief together with coordinates, preserving bytes."""
        code, output = self._edit([
            {"path": "README.md", "find": "Run this.\nKeep this.\n", "text": "Use this.\nKeep this."},
            self._range("README.md", 2, 2, "Read first."),
            {"path": "sample.py", "find": "    return 1", "text": "    return 2"},
        ])
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["changed"], ["README.md", "sample.py"])
        self.assertEqual(self.readme.read_bytes(), BOM_UTF8 + b"# Usage\r\nRead first.\r\nUse this.\r\nKeep this.\n")
        self.assertEqual(self.code.read_bytes(), b"def run():\n    return 2")

    def test_missing_and_overlapping_anchors_refuse_the_entire_plan(self) -> None:
        """An inexact line or overlap cannot write another staged file first."""
        first = self._range("README.md", 3, 3, "Leave this unchanged.")
        code, output = self._edit([first, {"path": "sample.py", "find": "return 1", "text": "    return 2"}])
        self.assertEqual(code, 2, output)
        self.assertIn("anchor was not found", output)
        code, output = self._edit([first, {"path": "sample.py", "find": "def run():\n    return 1", "text": ""},
                                   self._range("sample.py", 2, 2, "    return 2")])
        self.assertEqual(code, 2, output)
        self.assertIn("ranges overlap", output)
        self.assertEqual(self.readme.read_bytes(), self.original_readme)
        self.assertEqual(self.code.read_bytes(), self.original_code)

    def test_adjacent_duplicate_lines_are_ambiguous(self) -> None:
        """Repeated text cannot become an arbitrary first-match write."""
        self.readme.write_bytes(b"# Usage\r\nSame line.\r\nSame line.\n")
        with closing(MemoryGraph.open(self.root / ".reql/memory.reql")) as graph:
            result = graph.compile_project(self.root)
            self.assertFalse(result.run.errors)
        code, output = self._edit([{"path": "README.md", "find": "Same line.", "text": "Changed."}])
        self.assertEqual(code, 2, output)
        self.assertIn("ambiguous in README.md at lines 2 and 3", output)
        self.assertEqual(self.readme.read_bytes(), b"# Usage\r\nSame line.\r\nSame line.\n")
