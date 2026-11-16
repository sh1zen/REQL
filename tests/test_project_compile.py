"""Exercise explicit project compilation and its public command surface."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mcp.tools import MCPToolError, call_tool, list_tools
from memory.cli import build_parser, main
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

    def test_retired_cli_command_and_flags_are_rejected(self) -> None:
        """Retired arguments fail parsing before any graph access."""
        parser = build_parser()
        for args in (
            ["project", "watch-status"],
            ["project", "compile", "--watch"],
            ["project", "compile", "--watch-interval", "1"],
            ["project", "compile", "--watch-debounce", "1"],
            ["project", "compile", "--watch-iterations", "1"],
        ):
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                parser.parse_args(args)
            self.assertEqual(error.exception.code, 2)

    def test_python_and_mcp_offer_explicit_compilation(self) -> None:
        """Public Python and MCP clients can discover the supported compile entry points."""
        self.assertTrue(callable(MemoryGraph.compile_project))
        self.assertFalse(hasattr(MemoryGraph, "watch_project"))
        self.assertFalse(hasattr(MemoryGraph, "watch_project_once"))
        names = {tool["name"] for tool in list_tools()}
        self.assertIn("reql_compile_project", names)
        self.assertNotIn("reql_watch_project", names)
        with self.assertRaisesRegex(MCPToolError, "Unknown REQL MCP tool"):
            call_tool("reql_watch_project")


if __name__ == "__main__":
    unittest.main()
