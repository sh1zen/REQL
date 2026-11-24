"""Public query regressions for candidate completeness, aliases, and limits."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from contextlib import closing, redirect_stdout
import io
import json
import os
from unittest.mock import patch

from mcp.tools import MCPToolError, reql_query
from memory.cli import main

from api import MemoryGraph
from memory.domain.models import MemoryEdge, MemoryNode
from memory.query.errors import REQLSyntaxError


class QuerySemanticsTests(unittest.TestCase):
    """Query the real graph store without relying on an optimizer-specific path."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.graph = MemoryGraph.open(Path(self.temp.name) / ".reql" / "memory.reql")
        self.addCleanup(self.graph.close)
        self.graph.store.batch_upsert_nodes([
            MemoryNode(id="Source", type="Function", label="Invoice", utility=10,
                       properties={"name": "ComputeTotal", "custom": "MixedCase", "start_line": 7,
                                   "metadata": {"group": "Billing"}, "enabled": True}),
            MemoryNode(id="Target", type="Function", label="Receipt", utility=1,
                       properties={"name": "save", "enabled": False}),
            MemoryNode(id="archived", type="Function", label="Old", status="archived"),
        ])
        self.graph.store.upsert_edge(MemoryEdge(id="call", from_id="Source", to_id="Target", type="CALLS",
                                               properties={"custom": "MixedCase"}))

    def test_equality_resolves_native_custom_nested_and_aliased_fields(self) -> None:
        """An exact storage index must not change REQL's field or equality semantics."""
        for predicate in ("id = 'source'", "label = 'invoice'", "name = 'computetotal'",
                          "custom = 'mixedcase'", "metadata.group = 'billing'", "line_start = 7",
                          "properties.custom = 'mixedcase'", "utility = 10", "node.label = 'invoice'"):
            with self.subTest(predicate=predicate):
                result = self.graph.query(f"FIND nodes WHERE {predicate} RETURN id")
                self.assertEqual(result.rows, [{"id": "Source"}])
        self.assertEqual(self.graph.query("FIND nodes WHERE missing = NULL RETURN id ORDER BY id ASC").rows,
                         [{"id": "Source"}, {"id": "Target"}])
        self.assertEqual(self.graph.query("FIND nodes WHERE enabled = TRUE RETURN id").rows, [{"id": "Source"}])

    def test_node_type_and_status_constraints_are_case_insensitive_and_conjunctive(self) -> None:
        """Structural indexes must agree with the predicate evaluator, including archived rows."""
        result = self.graph.query("FIND nodes TYPE function WHERE status = 'ACTIVE' RETURN id ORDER BY id ASC")
        self.assertEqual(result.rows, [{"id": "Source"}, {"id": "Target"}])
        result = self.graph.query("FIND nodes WHERE status = 'ARCHIVED' INCLUDE ARCHIVED RETURN id")
        self.assertEqual(result.rows, [{"id": "archived"}])
        self.assertEqual(self.graph.query("FIND nodes TYPE Class WHERE id = 'Source' RETURN id").rows, [])
        self.assertEqual(self.graph.query("STATS TYPE function").rows, [{"nodes": 3, "edges": 1}])
        self.assertEqual(self.graph.query("STATS BY type TYPE function").rows, [{"type": "Function", "count": 3}])
        self.assertEqual(self.graph.query("FIND nodes WHERE type = 'Class' OR id = 'Source' RETURN id").rows,
                         [{"id": "Source"}])
        self.assertEqual(self.graph.query("FIND nodes WHERE NOT type = 'Function' RETURN id").rows, [])

    def test_edge_predicates_resolve_endpoints_and_native_fields(self) -> None:
        """FIND EDGES supports the same field resolution used during final evaluation."""
        for predicate in ("id = 'CALL'", "weight = 1", "from_id = 'source'", "from.label = 'invoice'",
                          "to.name = 'SAVE'", "r.type = 'calls'", "custom = 'mixedcase'"):
            with self.subTest(predicate=predicate):
                self.assertEqual(self.graph.query(f"FIND edges TYPE calls WHERE {predicate} RETURN id").rows,
                                 [{"id": "call"}])

    def test_where_and_ordering_are_applied_before_limit(self) -> None:
        """The correct row can be outside the old candidate window or initial storage order."""
        self.graph.store.batch_upsert_nodes([
            MemoryNode(id=f"filler:{index}", type="Function", salience=1, utility=index,
                       properties={"name": "shared", "selected": False}) for index in range(1001)
        ])
        source = self.graph.store.get_node("Source")
        source.properties.update(name="shared", selected=True)
        self.graph.store.update_node_fields("Source", properties=source.properties, utility=10000)
        for query in (
            "FIND nodes WHERE type = 'Function' AND selected = TRUE RETURN id LIMIT 1",
            "FIND nodes WHERE type = 'Function' RETURN id ORDER BY utility DESC LIMIT 1",
            "SYMBOLS WHERE type = 'Function' RETURN id ORDER BY utility DESC LIMIT 1",
            "SYMBOLS RETURN id ORDER BY utility DESC LIMIT 1",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.graph.query(query).rows, [{"id": "Source"}])
        self.assertEqual(self.graph.query("FIND nodes TYPE Function RETURN id LIMIT 1").diagnostics["matched"], 1003)
        self.assertEqual(len(self.graph.query("FIND nodes TYPE Function RETURN id").rows), 1003)
        self.assertEqual(len(self.graph.query("SYMBOLS TYPE Function RETURN id").rows), 1003)

    def test_path_reaches_a_weak_neighbor_and_follows_a_long_chain(self) -> None:
        """A high-degree node can hide the only route past the former neighbor and depth caps."""
        decoys = [f"decoy-{index}" for index in range(100)]
        dead_end = [f"cycle-{index}" for index in range(11)]
        chain = ["Source", *(f"step-{index}" for index in range(6)), "Target"]
        self.graph.store.batch_upsert_nodes([
            MemoryNode(id=node_id, type="Function") for node_id in [*decoys, *dead_end, *chain[1:-1]]
        ])
        with self.graph.store.transaction():
            for node_id in decoys:
                self.graph.store.upsert_edge(MemoryEdge(
                    id=node_id, from_id="Source", to_id=node_id, type="LINKS", weight=1.0,
                ))
            for index, (left, right) in enumerate(zip(chain, chain[1:])):
                self.graph.store.upsert_edge(MemoryEdge(
                    id=f"chain-{index}", from_id=left, to_id=right, type="LINKS", weight=0.1,
                ))
            # This dense component can only be left through an already visited node.
            for index, left in enumerate(dead_end):
                for right in ["step-1", *dead_end[index + 1:]]:
                    self.graph.store.upsert_edge(MemoryEdge(
                        id=f"{left}:{right}", from_id=left, to_id=right, type="LINKS",
                    ))
        query = "PATH FROM ID 'Source' TO ID 'Target' VIA LINKS RETURN node_ids"
        self.assertEqual(self.graph.query(query).rows, [{"node_ids": chain}])
        self.assertEqual(self.graph.query(query + " DEPTH 6").rows, [])
        self.assertEqual(len(self.graph.query("EXPLAIN NODE 'Source'").rows), 102)

    def test_path_ranks_late_candidates_and_keeps_parallel_relationships(self) -> None:
        """Sort every route before slicing and preserve its actual relationship IDs."""
        intermediate = [f"branch-{index:02}" for index in range(12)]
        self.graph.store.batch_upsert_nodes([
            MemoryNode(id=node_id, type="Function") for node_id in intermediate
        ])
        with self.graph.store.transaction():
            for index, node_id in enumerate(intermediate):
                weight = 1.0 if index == 11 else 0.1
                self.graph.store.upsert_edge(MemoryEdge(
                    id=f"first-{index:02}", from_id="Source", to_id=node_id, type="LINKS", weight=1.0,
                ))
                self.graph.store.upsert_edge(MemoryEdge(
                    id=f"last-{index:02}", from_id=node_id, to_id="Target", type="LINKS", weight=weight,
                ))
            self.graph.store.upsert_edge(MemoryEdge(
                id="alternate", from_id=intermediate[-1], to_id="Target", type="CONNECTS", weight=0.95,
            ))
        statement = "PATH FROM ID 'Source' TO ID 'Target' VIA LINKS,CONNECTS RETURN edge_ids"
        full = self.graph.query(statement)
        self.assertEqual(len(full.rows), 13)
        self.assertEqual(full.rows[:2], [
            {"edge_ids": ["first-11", "last-11"]},
            {"edge_ids": ["first-11", "alternate"]},
        ])
        limited = self.graph.query(statement + " LIMIT 1")
        self.assertEqual(limited.rows, full.rows[:1])
        self.assertEqual(limited.diagnostics["candidate_paths"], 13)
        self.assertEqual(self.graph.query(statement + " LIMIT 0").rows, [])

    def test_text_endpoint_and_ranked_commands_use_all_matches(self) -> None:
        """A path can start at a lexical match beyond the old leading results."""
        matches = [f"symbol-{index:02}" for index in range(24)]
        long_text = ("detail " * 100) + "tail"
        self.graph.store.batch_upsert_nodes([
            MemoryNode(id=node_id, type="Function", label="shared probe",
                       text=f"shared probe {node_id} {long_text}", salience=1.0 if index < 23 else 0.1)
            for index, node_id in enumerate(matches)
        ])
        self.graph.store.upsert_edge(MemoryEdge(
            id="selected", from_id=matches[-1], to_id="Target", type="LINKS",
        ))
        statement = """PATH FROM TEXT 'shared probe' TO ID 'Target' VIA LINKS RETURN edge_ids"""
        result = self.graph.query(statement)
        self.assertEqual(result.diagnostics["start_matches"], len(matches))
        self.assertEqual(result.rows, [{"edge_ids": ["selected"]}])
        for statement in ("SEARCH 'shared probe'", "EXPLAIN SEARCH 'shared probe'"):
            with self.subTest(statement=statement):
                self.assertEqual({row["id"] for row in self.graph.query(statement).rows}, set(matches))
                self.assertEqual(len(self.graph.query(statement + " TOP 2").rows), 2)
        statement = "RETRIEVE 'shared probe' NO SOURCES NO FILTER RETURN id,text"
        rows = self.graph.query(statement).rows
        self.assertEqual({row["id"] for row in rows}, set(matches))
        self.assertTrue(all("tail" in row["text"] for row in rows))
        self.assertEqual(self.graph.query(statement + " LIMIT 0").rows, [])
        shortened = self.graph.query(statement + " MAX TEXT 100").rows
        self.assertEqual({row["id"] for row in shortened}, set(matches))
        self.assertTrue(all(len(row["text"]) <= 100 for row in shortened))

    def test_match_aliases_cannot_be_overwritten_by_implicit_names(self) -> None:
        """User aliases, including uppercase spelling, bind to their actual pattern roles."""
        query = "MATCH (b:Function)-[a:CALLS]->(r:Function) RETURN b.id,a.id,r.id"
        self.assertEqual(self.graph.query(query).rows, [{"b.id": "Source", "a.id": "call", "r.id": "Target"}])
        query = "MATCH (Origin:Function)-[Link:CALLS]->(Destination:Function) RETURN origin.id,link.id,destination.id"
        self.assertEqual(self.graph.query(query).rows,
                         [{"origin.id": "Source", "link.id": "call", "destination.id": "Target"}])
        result = self.graph.query("MATCH (origin)-[link:CALLS]->(destination)")
        self.assertEqual(result.columns, ["origin.id", "origin.type", "origin.label", "link.type", "link.weight",
                                          "destination.id", "destination.type", "destination.label"])

    def test_repeated_match_node_alias_requires_the_same_node(self) -> None:
        """A repeated node variable denotes a self-loop, not two unrelated endpoints."""
        self.graph.store.upsert_edge(MemoryEdge(id="self", from_id="Source", to_id="Source", type="CALLS"))
        result = self.graph.query("MATCH (n)-[r:CALLS]->(n) RETURN n.id,r.id")
        self.assertEqual(result.rows, [{"n.id": "Source", "r.id": "self"}])
        result = self.graph.query("MATCH (n)-[r:CALLS]-(n) RETURN n.id,r.id")
        self.assertEqual(result.rows, [{"n.id": "Source", "r.id": "self"}])
        with self.assertRaises(REQLSyntaxError):
            self.graph.query("MATCH (a)-[a:CALLS]->(b)")

    def test_numeric_comparisons_and_sorting_preserve_large_integers(self) -> None:
        """Distinct integer values above float precision must remain distinct."""
        low, high = 2 ** 53, 2 ** 53 + 1
        self.graph.store.update_node_fields("Source", utility=low)
        self.graph.store.update_node_fields("Target", utility=high)
        for predicate in (f"utility = {high}", f"utility > {low}", f"utility BETWEEN {high} AND {high}"):
            with self.subTest(predicate=predicate):
                self.assertEqual(self.graph.query(f"FIND nodes WHERE {predicate} RETURN id").rows, [{"id": "Target"}])
        self.assertEqual(self.graph.query("FIND nodes RETURN id ORDER BY utility DESC LIMIT 1").rows, [{"id": "Target"}])

    def test_query_results_do_not_expose_borrowed_property_collections(self) -> None:
        """Projection must isolate the returned rows from internal store views."""
        node = self.graph.store.get_node("Source")
        node.properties["collection"] = {"nested": [{"values": ["kept"]}]}
        self.graph.store.update_node_fields(node.id, properties=node.properties)
        result = self.graph.query("FIND nodes WHERE id = 'Source' RETURN collection")
        result.rows[0]["collection"]["nested"][0]["values"].clear()
        self.assertEqual(self.graph.store.get_node("Source").properties["collection"], {"nested": [{"values": ["kept"]}]})

    def test_cli_and_mcp_share_complete_query_semantics_after_reopen(self) -> None:
        """Provider adapters use the corrected evaluator against persisted structural indexes."""
        statement = "FIND edges TYPE calls WHERE from.label = 'invoice' RETURN id"
        expected = self.graph.query(statement).rows
        storage_path = str(self.graph.store.path)
        self.graph.close()
        output = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.temp.name)
            with redirect_stdout(output):
                status = main(["query", statement, "--json"])
        finally:
            os.chdir(previous)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["rows"], expected)
        with patch.dict(os.environ, {"REQL_MCP_ALLOWED_ROOTS": self.temp.name}):
            self.assertEqual(reql_query(storage_path=storage_path, statement=statement)["rows"], expected)

    def test_cli_activation_persists_and_mcp_hub_explanation_is_read_only(self) -> None:
        """Activation takes the writer path and hub explanation works with a read lease."""
        storage_path = self.graph.store.path
        self.graph.close()
        activation = "ACTIVATE NODE 'Source' DEPTH 1 RETURN id"
        output = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.temp.name)
            with redirect_stdout(output):
                self.assertEqual(main(["query", activation, "--json"]), 0)
        finally:
            os.chdir(previous)
        self.assertEqual({row["id"] for row in json.loads(output.getvalue())["rows"]}, {"Source", "Target"})
        with closing(MemoryGraph.open(storage_path, read_only=True)) as reader:
            self.assertEqual(reader.store.get_edge("call").co_activation_count, 1)
        with patch.dict(os.environ, {"REQL_MCP_ALLOWED_ROOTS": self.temp.name}):
            with self.assertRaises(MCPToolError):
                reql_query(storage_path=str(storage_path), statement=activation)
            result = reql_query(storage_path=str(storage_path), statement="EXPLAIN HUB 'Source'")
            self.assertEqual(result["rows"][0]["node_id"], "Source")

if __name__ == "__main__":
    unittest.main()
