from pathlib import Path
import tempfile
import unittest

from reql import MemoryGraph


class SymbolResolutionTests(unittest.TestCase):
    def test_reexports_receivers_and_incremental_equivalence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = {
                "a.py": "def run():\n    return 3\n",
                "b.py": "def run():\n    return 4\n",
                "entry.py": (
                    "from a import run as selected\n"
                    "class Base:\n    def own(self):\n        return selected()\n"
                    "class Derived(Base):\n    def invoke(self):\n        return self.own()\n"
                    "def dynamic(receiver):\n    return receiver.run()\n"
                    "def shadowed(selected):\n    return selected()\n"
                ),
                "auth.ts": "export function refresh() { return 3; }\n",
                "barrel.ts": "export { refresh as refreshAuth } from './auth';\n",
                "entry.ts": (
                    "import { refreshAuth as run } from './barrel';\n"
                    "export function invoke() { return run(); }\n"
                    "class Client { run() { return 3; } execute() { return this.run(); } }\n"
                ),
                "auth.test.ts": "import { refresh } from './auth';\ntest('refresh session', () => { refresh(); });\n",
            }
            for name, source in fixture.items():
                (root / name).write_text(source, encoding="utf-8")
            graph = MemoryGraph.open(root / ".reql/memory.reql")
            try:
                self.assertFalse(graph.compile_project(root).run.errors)
                pairs = self._pairs(graph)
                self.assertIn(("entry.py", "own", "a.py", "run"), pairs)
                self.assertIn(("entry.py", "invoke", "entry.py", "own"), pairs)
                self.assertIn(("entry.ts", "invoke", "auth.ts", "refresh"), pairs)
                self.assertIn(("entry.ts", "execute", "entry.ts", "run"), pairs)
                self.assertIn(("auth.test.ts", "refresh session", "auth.ts", "refresh"), pairs)
                target = next(node for node in graph.store.find_nodes_by_property("name", "refresh") if node.type == "Function")
                self.assertEqual(graph.find_tests_for(symbol_id=target.id)[0].evidence_kind, "static")
                self.assertFalse(any(a[1] in {"dynamic", "shadowed"} for a in pairs))
                shadowed = next(node for node in graph.store.find_nodes_by_property("name", "shadowed") if node.type == "Function")
                self.assertIn("dynamic", {call["status"] for call in shadowed.properties["unresolved_calls"]})
                (root / "a.py").write_text("def run():\n    return 3\n\ndef run():\n    return 8\n", encoding="utf-8")
                (root / "barrel.ts").rename(root / "renamed.ts")
                graph.compile_project(root)
                expected = self._pairs(graph)
                self.assertNotIn(("entry.py", "own", "a.py", "run"), expected)
                self.assertNotIn(("entry.ts", "invoke", "auth.ts", "refresh"), expected)
                self.assertEqual(graph.compile_project(root).run.files_changed, 0)
            finally:
                graph.close()
            clean = MemoryGraph.open(root / ".reql/clean.reql")
            try:
                clean.compile_project(root)
                self.assertEqual(self._pairs(clean), expected)
            finally:
                clean.close()

    @staticmethod
    def _pairs(graph: MemoryGraph) -> set[tuple[str, str, str, str]]:
        return {
            (source.properties["relative_path"], source.properties["name"],
             target.properties["relative_path"], target.properties["name"])
            for edge in graph.store.get_edges(type_="CALLS", limit=None, clone=False)
            if edge.properties.get("status") != "archived"
            and (source := graph.get_node(edge.from_id)) is not None
            and (target := graph.get_node(edge.to_id)) is not None
        }
