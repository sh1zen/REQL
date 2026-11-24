"""Regression coverage for project graph policies and export boundaries."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from api import MemoryGraph
from memory.analysis import BridgeDetector, CentralityCalculator, SpecificityScorer
from memory.artifacts.fingerprint import normalize_path, project_id
from memory.config import ConfigError, default_config, load_effective_config, merge_config
from memory.domain.models import ActivationOptions, MemoryEdge, MemoryNode
from memory.reporting import render_pipeline_html


class GraphPolicyTests(TestCase):
    """Exercise project settings through the compiled graph and report APIs."""

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.storage = self.root / ".reql" / "memory.reql"

    def test_dotted_and_nested_settings_replace_vocabularies_and_validate_ranges(self) -> None:
        """File and programmatic overrides use the normal project precedence."""
        path = self.root / "reql.conf"
        path.write_text(
            'analysis:\n  workflow:\n    entry_names: ["launch"]\n'
            '  architecture:\n    domain_tokens: ["models"]\n',
            encoding="utf-8",
        )
        config = load_effective_config(
            path, env={"REQL_CONFIG_OVERRIDES": '{"analysis.workflow.entry_names": ["serve"]}'},
            overrides={"analysis": {"workflow": {"entry_names": ["run"]}}, "analysis.enable_hubs": False},
        )
        self.assertEqual(config.analysis.workflow.entry_names, ("run",))
        self.assertEqual(config.analysis.architecture.domain_tokens, ("models",))
        self.assertFalse(config.analysis.enable_hubs)
        for key, value in (
            ("analysis.activation.min_edge_weight", -1),
            ("analysis.salience.half_life_days", 0),
            ("analysis.hubs.min_score", float("inf")),
            ("analysis.communities.bridge_confidence", 2),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, re.escape(key)):
                merge_config(config, {key: value})
        for key, value in (
            ("analysis.architecture.layer_precedence", ["unknown"]),
            ("analysis.workflow.entry_names", [""]),
            ("reporting.markdown.delta_limit", -1),
            ("reporting.html.pipeline_search_limit", 1.5),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, re.escape(key)):
                load_effective_config(path, env={}, overrides={key: value})

    def test_activation_and_salience_consume_project_policy(self) -> None:
        """Use an allowed default edge, a latent target, and an inhibitory signal."""
        config = merge_config(default_config(), {
            "analysis.activation.blocked_edge_types": [],
            "analysis.activation.latent_factor": 0.5,
            "analysis.activation.negative_factor": 0.4,
            "analysis.salience.half_life_days": 2,
            "analysis.salience.type_priors.Function": 0.4,
            "analysis.salience.signal_weight": 0,
            "analysis.salience.volatility_weight": 0,
        })
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            (source, _), (latent, _), (inhibited, _), (extra, _) = graph.store.batch_upsert_nodes([
                MemoryNode(type="Function", label="source"),
                MemoryNode(type="Function", label="latent", status="latent"),
                MemoryNode(type="Function", label="inhibited"),
                MemoryNode(type="Function", label="extra"),
            ])
            graph.store.batch_upsert_edges([
                MemoryEdge(from_id=source.id, to_id=latent.id, type="EXTRACTED_FROM"),
                MemoryEdge(from_id=source.id, to_id=inhibited.id, type="INHIBITS", polarity=-1),
                MemoryEdge(from_id=source.id, to_id=extra.id, type="CALLS", weight=0.1),
            ])
            result = graph.activation.activate(
                [source.id, inhibited.id],
                ActivationOptions(max_depth=1, depth_decay=0.5, traverse_incoming=False, update_store=False),
            )
            self.assertAlmostEqual(result.activation_by_node[latent.id], 0.25)
            self.assertAlmostEqual(result.activation_by_node[inhibited.id], 0.8)
            self.assertAlmostEqual(result.activation_by_node[extra.id], 0.05)
            with patch("memory.engines.salience.seconds_since", return_value=2 * 86400):
                self.assertAlmostEqual(graph.salience.recency_score(source), 0.5)
                self.assertAlmostEqual(graph.salience.recency_score(source, half_life_days=1), 0.25)
                self.assertAlmostEqual(graph.salience.compute_node_salience(source), 0.4)

    def test_analysis_uses_configured_scores_and_persisted_bridge_metadata(self) -> None:
        """Use the shared settings with explicit analysis and composite scorers."""
        config = merge_config(default_config(), {
            "analysis.centrality.activation_saturation": 5,
            "analysis.hubs.min_score": 0.9,
            "analysis.specificity.generic_types": ["Function"],
            "analysis.specificity.generic_type_penalty": 0.2,
            "analysis.communities.bridge_weight": 0.22,
            "analysis.communities.bridge_confidence": 0.33,
            "analysis.bridges.min_score": 0,
            "analysis.bridges.max_generic_penalty": 1,
        })
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            (left, _), (left_peer, _), (right, _), (right_peer, _) = graph.store.batch_upsert_nodes([
                MemoryNode(type="Function", label=label, usage_count=5)
                for label in ("left worker", "left handler", "right worker", "right handler")
            ])
            graph.store.batch_upsert_edges([
                MemoryEdge(from_id=left.id, to_id=left_peer.id, type="CALLS"),
                MemoryEdge(from_id=right.id, to_id=right_peer.id, type="CALLS"),
                MemoryEdge(from_id=left.id, to_id=right.id, type="CALLS", weight=0.1),
            ])
            members = graph.community_detector.detect().membership
            self.assertNotEqual(members[left.id], members[right.id])
            bridges = graph.store.get_edges(type_="BRIDGES_COMMUNITY")
            self.assertEqual({(edge.weight, edge.confidence) for edge in bridges}, {(0.22, 0.33)})
            centrality = CentralityCalculator(graph.store, policy=config.analysis.centrality).calculate()
            self.assertEqual(centrality[left.id].activation_frequency, 1)
            metrics = CentralityCalculator(graph.store, policy=config.analysis.centrality).calculate(edge_limit=1)
            self.assertEqual(sum(item.out_degree for item in metrics.values()), 1)
            specificity = SpecificityScorer(graph.store, policy=config.analysis.specificity).score(left)
            self.assertAlmostEqual(specificity.generic_penalty, 0.2)
            hubs = graph.hub_analyzer.analyze(persist=False).hubs
            self.assertTrue(hubs)
            self.assertTrue(all(not hub.is_hub for hub in hubs))
            candidates = BridgeDetector(graph.store, config=config.analysis).find_candidates()
            self.assertEqual({candidate.bridge_node_id for candidate in candidates}, {left.id, right.id})
            strict = merge_config(config, {"analysis.bridges.min_score": 1})
            self.assertEqual(BridgeDetector(graph.store, config=strict.analysis).find_candidates(), [])

    def test_activation_scores_a_qualifying_neighbor_after_the_stronger_edges(self) -> None:
        """Every eligible neighbor receives activation in the public Python API."""
        neighbors = [f"neighbor-{index}" for index in range(135)]
        with closing(MemoryGraph.open(self.storage)) as graph:
            graph.store.batch_upsert_nodes([MemoryNode(id=node_id, type="Function")
                                          for node_id in ("seed", "tail", *neighbors)])
            graph.store.batch_upsert_edges([
                MemoryEdge(from_id="seed", to_id=node_id, type="CALLS", weight=0.9) for node_id in neighbors
            ] + [MemoryEdge(id="tail-edge", from_id="seed", to_id="tail", type="CALLS", weight=0.2)])
            result = graph.activation.activate(
                ["seed"], ActivationOptions(max_depth=1, traverse_incoming=False, update_store=True),
            )
            self.assertEqual(len(result.active_nodes), len(neighbors) + 2)
            self.assertAlmostEqual(result.activation_by_node["tail"], 0.11)
            self.assertEqual(graph.store.get_edge("tail-edge").co_activation_count, 1)

    def test_communities_propagate_until_stable_over_a_long_weighted_chain(self) -> None:
        """The strongest end of the chain takes more than twelve passes to reach the other end."""
        nodes = [f"node-{index:02}" for index in range(30)]
        with closing(MemoryGraph.open(self.storage)) as graph:
            graph.store.batch_upsert_nodes([MemoryNode(id=node_id, type="Function") for node_id in nodes])
            graph.store.batch_upsert_edges([
                MemoryEdge(from_id=left, to_id=right, type="CALLS", weight=(index + 1) / len(nodes))
                for index, (left, right) in enumerate(zip(nodes, nodes[1:]))
            ])
            early = graph.community_detector.detect(options={"max_iterations": 12}, persist=False)
            self.assertLess(len(early.membership), len(nodes))
            complete = graph.detect_communities()
            self.assertEqual(len(complete.community_nodes), 1)
            self.assertEqual(set(complete.membership), set(nodes))
            self.assertEqual(graph.store.count_edges(edge_types={"BELONGS_TO_COMMUNITY"}), len(nodes))

    def test_hub_analysis_ranks_nodes_outside_the_old_degree_candidates(self) -> None:
        """Apply the Python result limit after every eligible node has a score."""
        config = merge_config(default_config(), {
            "analysis.hubs.degree_weight": 0,
            "analysis.hubs.weighted_degree_weight": 0,
            "analysis.hubs.salience_weight": 1,
        })
        nodes = [f"worker-{index:03}" for index in range(132)]
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            graph.store.batch_upsert_nodes([
                MemoryNode(id=node_id, type="Function", label=node_id) for node_id in nodes
            ] + [MemoryNode(id="winner", type="Function", label="specific winner", salience=1)])
            graph.store.batch_upsert_edges([
                MemoryEdge(from_id=left, to_id=right, type="CALLS") for left, right in zip(nodes, nodes[1:])
            ])
            full = graph.hub_analyzer.analyze(limit=None, persist=False)
            self.assertEqual(len(full.hubs), len(nodes) + 1)
            limited = graph.analyze_hubs(limit=1)
            self.assertEqual(limited.hubs, full.hubs[:1])
            self.assertEqual(limited.hubs[0].node_id, "winner")

    def test_bridge_analysis_keeps_evidence_from_every_reachable_community(self) -> None:
        """A crowded neighborhood can hide weaker links and cross-community evidence."""
        config = merge_config(default_config(), {
            "analysis.bridges.min_score": 0,
            "analysis.bridges.max_generic_penalty": 1,
        })
        members = [f"local-{index:03}" for index in range(510)]
        remote = [f"remote-{index}" for index in range(8)]
        communities = [f"community-{index}" for index in range(len(remote) + 1)]
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            graph.store.batch_upsert_nodes([
                MemoryNode(id=node_id, type="Function", label=node_id) for node_id in ("bridge", *members, *remote)
            ] + [MemoryNode(id=node_id, type="Community", label=node_id) for node_id in communities])
            graph.store.batch_upsert_edges([
                MemoryEdge(from_id=node_id, to_id=communities[0], type="BELONGS_TO_COMMUNITY")
                for node_id in ("bridge", *members)
            ] + [
                MemoryEdge(from_id=node_id, to_id=community, type="BELONGS_TO_COMMUNITY")
                for node_id, community in zip(remote, communities[1:])
            ] + [
                MemoryEdge(from_id="bridge", to_id=node_id, type="CALLS", weight=0.9) for node_id in members
            ] + [
                MemoryEdge(from_id="bridge", to_id=node_id, type="CALLS", weight=0.2) for node_id in remote
            ] + [MemoryEdge(from_id=remote[0], to_id=remote[1], type="CALLS", weight=0.7)])
            candidates = BridgeDetector(graph.store, config=config.analysis).find_candidates(limit=None)
            bridge = next(candidate for candidate in candidates if candidate.bridge_node_id == "bridge")
            self.assertEqual(set(bridge.community_ids), set(communities))
            self.assertEqual(len(bridge.evidence_node_ids), len(communities))
            self.assertTrue(set(remote) <= set(bridge.evidence_node_ids))
            self.assertAlmostEqual(bridge.cross_community_distance, 1.0 - 0.7 / 36)

    def test_projection_settings_control_layers_and_explanations_follow_long_workflows(self) -> None:
        """Apply project roles and include every participant in a deep cyclic call chain."""
        source = self.root / "src" / "api" / "models" / "widget.py"
        source.parent.mkdir(parents=True)
        source.write_text("def calculate_total(): return 1\n", encoding="utf-8")
        jobs = self.root / "src" / "models" / "jobs.py"
        jobs.parent.mkdir(parents=True)
        jobs.write_text(
            "from src.api.models.widget import calculate_total\n"
            "def launch(): return launch_stage()\n"
            "def launch_stage():\n"
            "    value = stage_0()\n"
            "    if not value: raise ValueError()\n"
            "    return value\n"
            + "".join(f"def stage_{index}(): return stage_{index + 1}()\n" for index in range(5))
            + "def stage_5():\n    if False: launch()\n    return calculate_total()\n",
            encoding="utf-8",
        )
        config = merge_config(default_config(), {
            "analysis.architecture.layer_precedence": ["domain", "interface"],
            "analysis.architecture.domain_tokens": ["models"],
            "analysis.workflow.entry_names": ["launch"],
            "analysis.workflow.interface_path_tokens": ["api"],
            "reporting.html.graph_label_chars": 8,
            "reporting.html.pipeline_search_limit": 5,
        })
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            explanation = graph.explain_project(self.root)
            self.assertEqual({cap.layer for cap in explanation.capabilities}, {"domain"})
            workflow = next(item for item in explanation.workflows if any(member.role == "trigger" and member.target.label == "launch" for member in item.participants))
            self.assertEqual(
                {item.target.label for item in workflow.participants},
                {"launch", "launch_stage", "calculate_total", *(f"stage_{index}" for index in range(6))},
            )
            self.assertEqual(workflow.invariants, ["launch_stage raises ValueError"])
            pipeline = graph.project_pipeline(self.root)
            self.assertEqual({item.layer for item in pipeline.components}, {"domain"})
            self.assertIn("calculate_total", {symbol.label for item in pipeline.components for symbol in item.symbols})
            pipeline_html = render_pipeline_html(pipeline, policy=config.reporting.html)
            self.assertIn('"pipeline_search_limit":5', pipeline_html)
            self.assertIn("@version 10.1.5", pipeline_html)
            self.assertNotIn("<script src=", pipeline_html)
            graph_html = graph.export_html(self.root / "graph.html").read_text(encoding="utf-8")
            payload = graph_payload(graph_html)
            self.assertEqual(payload["display"]["graph_label_chars"], 8)
            self.assertTrue(all(len(node.get("label", "")) <= 8 for node in payload["nodes"]))
            self.assertIn("@version 10.1.5", graph_html)
            self.assertNotIn("<script src=", graph_html)

    def test_graph_and_pipeline_share_uncapped_inferred_roots(self) -> None:
        """Cover disconnected cyclic workflows and carry the selected IDs into HTML."""
        codes = []
        for index in range(26):
            codes.append(f"def source_{index}(): return stage_{index}()\ndef stage_{index}(): return source_{index}()")
        (self.root / "work.py").write_text("\n".join(codes), encoding="utf-8")
        with closing(MemoryGraph.open(self.storage)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            workflow_ids = {item.trigger.node_id for item in graph.project_pipeline(self.root).workflows if item.inferred}
            self.assertEqual(len(workflow_ids), 26)
            payload = graph_payload(graph.export_html(self.root / "graph.html").read_text(encoding="utf-8"))
            self.assertEqual(set(payload["entrypoint_ids"]), workflow_ids)
        disabled = merge_config(default_config(), {"analysis.workflow.infer_roots": False})
        with closing(MemoryGraph.open(self.storage, config=disabled)) as graph:
            self.assertEqual(graph.project_pipeline(self.root).workflows, [])
            payload = graph_payload(graph.export_html(self.root / "graph.html").read_text(encoding="utf-8"))
            self.assertEqual(payload["entrypoint_ids"], [])

    def test_report_limits_display_after_counting_the_analysis_results(self) -> None:
        """Calculate communities and hubs in memory before limiting report rows."""
        (self.root / "workers.py").write_text("def process(): return 1\n", encoding="utf-8")
        config = merge_config(default_config(), {
            "reporting.markdown.section_limit": 0,
            "reporting.markdown.analysis_limit": 1,
        })
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            scope = project_id(normalize_path(self.root))
            pairs = [(f"left-{index}", f"right-{index}") for index in range(4)]
            graph.store.batch_upsert_nodes([
                MemoryNode(id=node_id, type="Function", label=node_id, properties={"project_id": scope})
                for pair in pairs for node_id in pair
            ])
            graph.store.batch_upsert_edges([
                MemoryEdge(from_id=left, to_id=right, type="CALLS", properties={"project_id": scope})
                for left, right in pairs
            ])
            report = graph.project_report(self.root, output_dir=self.root / ".reql" / "reports").graph_report
            communities, hubs = report.read_text(encoding="utf-8").split("## Communities\n")[1].split("## God nodes / hubs\n")
            self.assertRegex(communities, r"\d+ more omitted \(1 of \d+ shown\)")
            self.assertRegex(hubs, r"\d+ more omitted \(1 of \d+ shown\)")
            self.assertEqual(graph.store.count_nodes(node_types={"Community"}), 0)

    def test_report_counts_ignore_collection_caps_and_disclose_display_limits(self) -> None:
        """Count full scoped rows and apply Markdown limits after ordering."""
        config = merge_config(default_config(), {
            "analysis.enable_hubs": False,
            "analysis.enable_communities": False,
            "reporting.markdown.section_limit": 1,
            "reporting.markdown.delta_limit": 1,
            "reporting.markdown.cache_limit": 1,
        })
        for name in ("alpha.py", "omega.py"):
            (self.root / name).write_text("VERSION = 1\n", encoding="utf-8")
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            self.assertFalse(graph.compile_project(self.root).run.errors)
            project = graph.projects.project_status(self.root)
            assert project is not None
            project_id = project["project"]["id"]
            props = {"project_id": project_id}
            graph.store.batch_upsert_nodes([
                MemoryNode(type="Function", label=f"function-{index}", properties=props)
                for index in range(10001)
            ], return_clones=False)
            graph.store.batch_upsert_nodes([
                MemoryNode(type="Concept", label=str(index), properties=props, status="archived")
                for index in range(1001)
            ], return_clones=False)
            graph.store.batch_upsert_nodes([
                MemoryNode(type="GraphDelta", id=f"delta-{index}", properties={**props, "created_at": f"2099-11-2{index}"})
                for index in (1, 2)
            ])
            files = graph.project_report(self.root, output_dir=self.root / "reports")
            report = files.graph_report.read_text(encoding="utf-8")
            self.assertIn("- functions: 10001\n", report)
            self.assertIn("- Archived nodes: 1001\n", report)
            self.assertIn("- 10000 more omitted (1 of 10001 shown).", report)
            cache = files.cache_report.read_text(encoding="utf-8")
            self.assertIn("alpha", cache)
            self.assertNotIn("omega", cache)
            self.assertIn("- 1 more omitted (1 of 2 shown).", cache)
            deltas = files.graph_deltas.read_text(encoding="utf-8")
            self.assertIn("### delta-2", deltas)
            self.assertNotIn("### delta-1", deltas)
            self.assertIn("more omitted", deltas)
        unlimited = merge_config(config, {"reporting.markdown.cache_limit": 0, "reporting.markdown.delta_limit": 0})
        with closing(MemoryGraph.open(self.storage, config=unlimited)) as graph:
            files = graph.project_report(self.root, output_dir=self.root / "reports")
            self.assertIn("omega", files.cache_report.read_text(encoding="utf-8"))
            self.assertIn("### delta-1", files.graph_deltas.read_text(encoding="utf-8"))

    def test_report_shows_all_parser_errors_for_each_listed_artifact(self) -> None:
        """Keep every diagnostic attached to a listed artifact."""
        with closing(MemoryGraph.open(self.storage)) as graph:
            graph.store.upsert_node(MemoryNode(type="SourceArtifact", label="partial.md", properties={
                "project_id": project_id(normalize_path(self.root)),
                "relative_path": "partial.md",
                "parser_errors": ["first diagnostic", "second diagnostic", "third diagnostic"],
            }))
            files = graph.project_report(self.root, output_dir=self.storage.parent / "reports")
            report = files.graph_report.read_text(encoding="utf-8")
        for diagnostic in ("first diagnostic", "second diagnostic", "third diagnostic"):
            self.assertIn(diagnostic, report)

    def test_report_uses_hub_warnings_for_recommended_actions(self) -> None:
        """Respect the hub analyzer's warning threshold in both report sections."""
        config = merge_config(default_config(), {
            "analysis.enable_communities": False,
            "analysis.hubs.warning_centrality": 0,
            "analysis.hubs.warning_generic_penalty": 0.5,
            "analysis.specificity.generic_types": ["Function"],
        })
        output_dir = self.storage.parent / "reports"
        with closing(MemoryGraph.open(self.storage, config=config)) as graph:
            props = {"project_id": project_id(normalize_path(self.root))}
            (generic, _), *peers = graph.store.batch_upsert_nodes([
                MemoryNode(type="Function", label="x", properties=props),
                *(MemoryNode(type="Function", label=f"specific_worker_{index}", properties=props) for index in range(3)),
            ])
            graph.store.batch_upsert_edges([
                MemoryEdge(from_id=generic.id, to_id=peer.id, type="CALLS") for peer, _ in peers
            ])
            report = graph.project_report(self.root, output_dir=output_dir).graph_report.read_text(encoding="utf-8")
            self.assertIn("Generic high-degree candidate penalized: x", report)
            self.assertIn("Rename or split generic high-degree nodes", report)

        strict = merge_config(config, {"analysis.hubs.warning_generic_penalty": 1})
        with closing(MemoryGraph.open(self.storage, config=strict)) as graph:
            report = graph.project_report(self.root, output_dir=output_dir).graph_report.read_text(encoding="utf-8")
            self.assertIn("Generic hub warnings:\n- No data yet.", report)
            self.assertNotIn("Rename or split generic high-degree nodes", report)


def graph_payload(html: str) -> dict[str, Any]:
    """Extract the standalone graph's JSON block."""
    match = re.search(r'<script id="graph-data" type="application/json">(.*?)</script>', html, re.DOTALL)
    assert match is not None
    return json.loads(match[1])
