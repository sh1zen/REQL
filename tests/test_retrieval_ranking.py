from pathlib import Path
import tempfile
import unittest

from config import VERSION
from memory.domain.models import MemoryQuery
from reql import MemoryGraph, QueryContextRequest


class RetrievalRankingTests(unittest.TestCase):
    def test_compound_query_matches_working_set_owner_by_its_indexed_name(self) -> None:
        """Ordinary query vocabulary can also be the name of a code owner."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = "packages/src/dispatcher.py"
            (root / path).parent.mkdir(parents=True)
            (root / path).write_text("def workflow():\n    pass\n", encoding="utf-8")
            graph = MemoryGraph.open(root / ".reql/memory.reql")
            try:
                self.assertFalse(graph.compile_project(root).run.errors)
                payload = graph.query_context_payload(
                    "Investigate workflow after migration", scopes=["code"],
                )["payload"]
                self.assertTrue(any(
                    row["path"] == path and any(name.rsplit(".", 1)[-1] == "workflow" for name in row["symbols"])
                    for row in payload["working_set"]
                ), payload)
                exact = graph.query_context_payload(path, scopes=["code"])["payload"]
                self.assertEqual(exact["working_set"][0]["path"], path)
            finally:
                graph.close()

    def test_exact_corroborated_homonym_and_stable_owner_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "billing.py").write_text(
                "from tax import sales_tax\n\ndef invoice_total(total):\n    return total + sales_tax(total)\n",
                encoding="utf-8",
            )
            (root / "tax.py").write_text("def sales_tax(total):\n    return total * .1\n", encoding="utf-8")
            (root / "metrics.py").write_text("def invoice_total(count):\n    return count\n", encoding="utf-8")
            graph = MemoryGraph.open(root / ".reql/memory.reql")
            try:
                graph.compile_project(root)
                query = "invoice_total sales_tax"
                subgraph = graph.retrieval.retrieve(MemoryQuery(query, context_scopes={"code"}, store_trace=False))
                functions = {item.node.properties["relative_path"] for item in subgraph.ranked_nodes
                             if item.node.type == "Function"}
                self.assertEqual(functions, {"billing.py", "tax.py"})
                request = QueryContextRequest.from_raw(text=query, scopes=["code"], include_source=True, max_items=5)
                first = graph.query_context_result(request)
                second = graph.query_context_result(request)
                self.assertEqual(first.payload["owner_candidates"], second.payload["owner_candidates"])
                self.assertEqual(first.payload["ranking_profile"], VERSION)
                self.assertTrue(any(row["source_status"] == "verified" for row in first.payload["snippets"]))
            finally:
                graph.close()
