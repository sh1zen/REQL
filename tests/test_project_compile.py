"""Exercise explicit project compilation and its public command surface."""
from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mcp.tools import list_tools
from memory.cli import main
from reql import MemoryGraph


class ProjectCompileTests(unittest.TestCase):
    def test_compile_creates_and_refreshes_the_graph_on_demand(self) -> None:
        """Explicit compilation detects changes and reuses unchanged results."""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(directory.name)
        source = Path("app.py")
        for increment, changed in ((1, 1), (2, 1), (None, 0)):
            with self.subTest(increment=increment, changed=changed):
                if increment is not None:
                    source.write_text(f"def emit_event(value):\n    return value + {increment}\n", encoding="utf-8")
                with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
                    code = main(["project", "compile"])
                self.assertEqual(code, 0, errors.getvalue())
                self.assertIn("Status: completed", output.getvalue())
                self.assertIn(f"Changed: {changed}", output.getvalue())

    def test_mcp_advertises_explicit_compilation(self) -> None:
        names = {tool["name"] for tool in list_tools()}
        self.assertIn("reql_compile_project", names)

    def test_compile_accepts_a_deadline_for_a_busy_graph(self) -> None:
        """Use the native writer lease and report an expired timeout without compiling."""
        with TemporaryDirectory() as folder:
            previous = Path.cwd()
            try:
                os.chdir(folder)
                Path("sample.py").write_text("def value():\n    return 10\n", encoding="utf-8")
                with closing(MemoryGraph.open(Path(".reql/memory.reql"))):
                    with redirect_stderr(StringIO()) as errors:
                        self.assertEqual(main(["project", "compile", "--lock-timeout", "0.01"]), 1)
                    self.assertIn("storage is locked", errors.getvalue())
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(["project", "compile", "--lock-timeout", "0.01"]), 0)
                self.assertIn("Changed: 1", output.getvalue())
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
