"""Regression tests for configurable extraction, retrieval and query policies."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from api import MemoryGraph
from memory.config import default_config, merge_config
from memory.domain.models import MemoryEdge, MemoryNode


class SearchPolicyTests(TestCase):
    """Exercise effective project settings through the public graph entry points."""

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.storage = self.root / ".reql" / "memory.reql"

    def test_lexical_policy_rebuilds_checkpoint_postings_and_wal_overlay(self) -> None:
        custom = merge_config(default_config(), {
            "lexical.normalization.fold_accents": False,
            "lexical.normalization.expand_plurals": False,
            "lexical.normalization.split_identifiers": False,
            "lexical.seeds.entity_limit": 1,
            "lexical.seeds.entity_confidence": 0.81,
        })
        with closing(MemoryGraph.open(self.storage)) as graph:
            graph.store.upsert_node(MemoryNode(id="coffee", type="Entity", text="caffè"))
            graph.store.upsert_node(MemoryNode(id="camel", type="Entity", text="ComputeTotals"))
            graph.store.upsert_node(MemoryNode(id="accruals", type="Entity", text="Accruals"))
            graph.store.checkpoint_if_needed(wal_bytes_threshold=0)
            graph.store.upsert_node(MemoryNode(id="wal", type="Entity", text="東京支店"))
            self.assertEqual([node.id for node, _ in graph.store.lexical_search("caffe")], ["coffee"])
            self.assertEqual([node.id for node, _ in graph.store.lexical_search("compute")], ["camel"])
            self.assertEqual([node.id for node, _ in graph.store.lexical_search("accrual")], ["accruals"])
        with closing(MemoryGraph.open(self.storage, config=custom, read_only=True)) as graph:
            self.assertEqual(graph.store.lexical_search("caffe"), [])
            self.assertEqual(graph.store.lexical_search("compute"), [])
            self.assertEqual(graph.store.lexical_search("accrual"), [])
            self.assertEqual([node.id for node, _ in graph.store.lexical_search("caffè")], ["coffee"])
            self.assertEqual([node.id for node, _ in graph.store.lexical_search("東京支店")], ["wal"])
            self.assertEqual(graph.extractor.extract('"First mention" "Second mention"').entities,
                             [("First mention", "named_thing", 0.81)])
        with closing(MemoryGraph.open(self.storage, config=custom, defer_lexical_index=True)) as graph:
            checkpoint = graph.store.checkpoint_if_needed()
            self.assertEqual(checkpoint["reason"], "lexical_policy_changed")
        with closing(MemoryGraph.open(self.storage, config=custom, read_only=True)) as graph:
            self.assertEqual([node.id for node, _ in graph.store.lexical_search("東京支店")], ["wal"])
        with closing(MemoryGraph.open(self.storage, read_only=True)) as graph:
            self.assertEqual([node.id for node, _ in graph.store.lexical_search("caffe")], ["coffee"])

    def test_lexical_indexes_complete_text_and_searchable_metadata(self) -> None:
        """Find all normalized terms and refresh postings for a change deep in metadata."""
        terms = [f"documentterm{index:04}" for index in range(1200)]
        values = [f"exporteditem{index:04}" for index in range(100)]
        values.append("extendedcontext " * 21 + "lastcollectionmarker")
        with closing(MemoryGraph.open(self.storage)) as graph:
            graph.store.upsert_node(MemoryNode(
                id="wide", type="Concept", text=" ".join(terms),
                properties={"name": "extendedcontext " * 40 + "lastscalarmarker", "re_exports": values},
            ))
            for term in (*terms, *values[:100], "lastcollectionmarker", "lastscalarmarker"):
                self.assertEqual([node.id for node, _ in graph.store.lexical_search(term)], ["wide"], term)
            graph.store.checkpoint_if_needed(wal_bytes_threshold=0)
            graph.store.update_node_fields(
                "wide", properties={"name": "extendedcontext " * 40 + "lastscalarmarker",
                                    "re_exports": [*values[:-1], "extendedcontext " * 21 + "replacementmarker"]},
            )
        with closing(MemoryGraph.open(self.storage, read_only=True, defer_lexical_index=True)) as graph:
            self.assertEqual(graph.store.lexical_search("lastcollectionmarker"), [])
            for term in ("replacementmarker", "lastscalarmarker", terms[-1]):
                self.assertEqual([node.id for node, _ in graph.store.lexical_search(term)], ["wide"])

    def test_lexical_top_k_uses_the_final_score_for_every_candidate(self) -> None:
        """A salience boost can promote a result outside the former candidate window."""
        with closing(MemoryGraph.open(self.storage)) as graph:
            graph.store.batch_upsert_nodes([
                MemoryNode(id=f"candidate-{index:02}", type="Concept", text="filterbalance",
                           properties={"project_id": "target"})
                for index in range(60)
            ] + [
                MemoryNode(id="winner", type="Concept", text="filterbalance", salience=1.0,
                           properties={"project_id": "target"}),
                MemoryNode(id="outside", type="Concept", text="filterbalance", salience=1.0,
                           properties={"project_id": "outside"}),
            ])
            matches = graph.store.lexical_search("filterbalance", project_id="target", top_k=None)
            self.assertEqual(len(matches), 61)
            self.assertEqual(matches[0][0].id, "winner")
            self.assertEqual(graph.store.lexical_search("filterbalance", project_id="target", top_k=1), matches[:1])

    def test_document_limits_take_effect_when_compiling_unchanged_source(self) -> None:
        (self.root / "handbook.md").write_text(
            "# Handbook\n\nCompiler pipeline source retrieval.\n\nCompiler pipeline context ranking.\n",
            encoding="utf-8",
        )
        def active_terms(graph: MemoryGraph) -> list[MemoryNode]:
            return [node for node in graph.store.find_nodes_by_types(("Concept",))
                    if node.status == "active" and node.properties.get("extractor") == "document_processor"]

        base = merge_config(default_config(), {"compile.documents.markdown": True})
        with closing(MemoryGraph.open(self.storage, config=base)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            self.assertGreater(len(active_terms(graph)), 1)
        custom = merge_config(base, {"lexical.documents.max_terms": 1, "lexical.documents.max_events_per_term": 1})
        with closing(MemoryGraph.open(self.storage, config=custom)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            terms = active_terms(graph)
            self.assertEqual(len(terms), 1)
            self.assertEqual(terms[0].properties["processor_signature"]["raw_event_count"], 1)
            self.assertEqual(terms[0].properties["processor_signature"]["relation_count"], 0)

    def test_rank_and_confidence_use_effective_policy(self) -> None:
        (self.root / "orders.py").write_text("def process_order():\n    return 1\n", encoding="utf-8")
        config = merge_config(default_config(), {
            "retrieval.context.min_confidence": 0.95,
            "retrieval.search.exact_identifier": 0.25,
            "retrieval.search.exact_text": 0.3,
            "retrieval.ranking.direct_weight": 0.5,
            "retrieval.ranking.coverage_weight": 0.0,
            "retrieval.ranking.path_weight": 0.0,
            "retrieval.ranking.seed_weight": 0.0,
            "retrieval.ranking.exact_symbol_bonus": 0.0,
        })
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            result = graph.query_context_payload("process order", scopes=["code"])
            self.assertEqual(result["confidence"]["threshold"], 0.95)
            self.assertTrue(result["confidence"]["targeted_rg_fallback_allowed"])
            ranked = graph.retrieve("process order", max_depth=0).ranked_nodes
            owner = next(item for item in ranked if item.node.type == "Function")
            self.assertEqual(owner.reasons["match_score"], 0.25)

    def test_expansion_checks_all_neighbors_within_the_requested_depth(self) -> None:
        """Visit a qualifying branch after the former neighbor and visit caps."""
        config = merge_config(default_config(), {
            "retrieval.ranking.min_edge_weight": 0.6,
            "retrieval.ranking.context_min_edge_weight": 1.0,
            "retrieval.ranking.depth_penalty_per_hop": 0.0,
            "retrieval.ranking.direct_weight": 0.2,
            "retrieval.ranking.coverage_weight": 0.1,
            "retrieval.ranking.resolved_edge_bonus": 1.0,
        })
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            decoys = [f"neighbor-{index}" for index in range(180)]
            graph.store.batch_upsert_nodes([
                MemoryNode(id=node_id, type="Function", label=node_id)
                for node_id in ("entrypoint", "bridge", "weak", "final", *decoys)
            ])
            with graph.store.transaction():
                for endpoint, weight in [("bridge", 0.7), ("weak", 0.5), *((key, 0.9) for key in decoys)]:
                    graph.store.upsert_edge(MemoryEdge(
                        id=endpoint, from_id="entrypoint", to_id=endpoint, type="CALLS", weight=weight,
                    ))
                graph.store.upsert_edge(MemoryEdge(
                    id="second", from_id="bridge", to_id="final", type="CALLS", weight=0.9,
                    properties={"resolver": "symbol_table"},
                ))
            result = graph.retrieve("entrypoint", max_depth=1, edge_types={"CALLS"})
            self.assertNotIn("final", {item.node.id for item in result.ranked_nodes})
            result = graph.retrieve("entrypoint", top_k=200, max_depth=2, edge_types={"CALLS"})
            self.assertEqual({item.node.id for item in result.ranked_nodes}, {"entrypoint", "bridge", "final", *decoys})
            limited = graph.retrieve("entrypoint", top_k=1, max_depth=2, edge_types={"CALLS"})
            self.assertEqual([item.node.id for item in limited.ranked_nodes], ["final"])
            lexical = graph.retrieve("neighbor", top_k=1, max_depth=0, node_types={"Function"})
            self.assertEqual(set(lexical.seed_node_ids), set(decoys))
            self.assertEqual(len(lexical.ranked_nodes), 1)

    def test_verified_source_and_document_scope_use_project_policy(self) -> None:
        (self.root / "orders.py").write_text("def order_invoice():\n    return 'invoice'\n", encoding="utf-8")
        (self.root / "handbook.md").write_text("# Billing\n\nCall order_invoice to order an invoice.\n", encoding="utf-8")
        config = merge_config(default_config(), {
            "compile.documents.markdown": True,
        })
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            context = graph.query_context_payload("order_invoice", scopes=["code"], include_source=True)["payload"]
            self.assertTrue(any(row.get("source_status") == "verified" and "return 'invoice'" in row.get("text", "")
                                for row in context["snippets"]), context["snippets"])
            mentions = graph.query_explore("order_invoice", views=["docs_mentions"])["sections"]["docs_mentions"]
            self.assertTrue(any(row["mention"]["location"].startswith("handbook.md") for row in mentions), mentions)

    def test_path_ranking_and_query_columns_apply_to_execution(self) -> None:
        statement = "PATH FROM ID start TO ID finish"
        config = merge_config(default_config(), {
            "query.path.edge_weight": 0.0,
            "query.path.salience_weight": 1.0,
            "query.defaults.path_columns": ["node_ids", "score"],
            "query.defaults.node_columns": ["id"],
        })
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            graph.store.batch_upsert_nodes([
                MemoryNode(id=node_id, type="Function", label=node_id, salience=salience)
                for node_id, salience in (("start", 0.2), ("decoy", 0.9), ("finish", 0.6))
            ])
            graph.store.upsert_edge(MemoryEdge(id="strong", type="CALLS", from_id="start", to_id="decoy", weight=0.9))
            graph.store.upsert_edge(MemoryEdge(id="target", type="CALLS", from_id="start", to_id="finish", weight=0.4))
            result = graph.query(statement)
            self.assertEqual(result.rows, [{"node_ids": ["start", "finish"], "score": 0.4}])
            self.assertEqual(graph.query(statement + " RETURN edge_ids").rows, [{"edge_ids": ["target"]}])
            self.assertEqual(graph.query(statement + " DEPTH 0").rows, [])
            self.assertEqual(graph.query("FIND NODES ORDER BY id ASC LIMIT 1").rows, [{"id": "decoy"}])

    def test_template_duplicates_use_parsed_markup_and_shared_formats(self) -> None:
        (self.root / "summary.html").write_text(
            '<article data-rule="one > two"><header>Title</header><p>Body</p></article>',
            encoding="utf-8",
        )
        (self.root / "alternate.jinja").write_text(
            '<!-- <aside><b>hidden</b></aside> -->'
            '<article data-rule="different"><header>Other</header><p>Value</p></article>',
            encoding="utf-8",
        )
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            duplicates = graph.query_explore("summary.html", views=["structural_duplicates"])["sections"]["structural_duplicates"]
            self.assertEqual(len(duplicates), 1, duplicates)
            self.assertEqual(duplicates[0]["similarity"], 1.0)
            self.assertEqual(duplicates[0]["duplicate_tag_count"], 3)
            self.assertTrue(duplicates[0]["duplicate"]["location"].startswith("alternate.jinja"))
