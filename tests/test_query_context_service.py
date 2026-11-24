from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from mcp.tools import call_tool
from memory.artifacts.options import CompilationOptions
from memory.domain.models import MemoryNode
from memory.extraction.deterministic import DeterministicExtractor
from memory.services.coordination import CoordinationStore
from memory.domain.query_context import (
    ContextResult,
    QueryContextRequest,
    QueryMode,
    RetrievalBudget,
)
from tests.config_helpers import open_graph_with_documents


class QueryContextContractTests(unittest.TestCase):
    """Validate provider values and the advertised MCP request schema."""

    def test_request_factory_normalizes_provider_values(self) -> None:
        request = QueryContextRequest.from_raw(
            text="  shared context  ",
            mode="CLEANUP",
            scopes=[" Code ", "test", "code"],
            top_k=7,
            max_depth=2,
            max_items=9,
            include_archived=True,
        )

        self.assertEqual(request.text, "  shared context  ")
        self.assertIs(request.mode, QueryMode.CLEANUP)
        self.assertEqual(request.scopes, frozenset({"code", "test"}))
        self.assertEqual(request.budget, RetrievalBudget(top_k=7, max_depth=2, max_items=9))
        self.assertTrue(request.include_archived)
        with self.assertRaises(FrozenInstanceError):
            request.include_archived = False  # type: ignore[misc]

    def test_request_factory_enforces_shared_limits_and_labels(self) -> None:
        invalid_cases = (
            {"text": ""},
            {"text": "x", "mode": "edit"},
            {"text": "x", "scopes": ["unknown"]},
            {"text": "x", "top_k": 0},
            {"text": "x", "top_k": 51},
            {"text": "x", "max_depth": 6},
            {"text": "x", "max_items": 51},
            {"text": "x", "task_id": "  "},
        )
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                QueryContextRequest.from_raw(**values)

class QuerySeedExtractionTests(unittest.TestCase):
    def test_lexical_mentions_share_the_same_type_and_weight(self) -> None:
        """Apply the same rules to arbitrary quoted names, acronyms and identifiers."""
        result = DeterministicExtractor().extract('"OK" "SQLite" ORBIT7 LLM "nebula"')
        self.assertEqual(
            {name: (kind, weight) for name, kind, weight in result.entities},
            dict.fromkeys(("OK", "SQLite", "ORBIT7", "LLM", "nebula"), ("named_thing", 0.65)),
        )


class QueryContextServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_root = Path.cwd() / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=tmp_root)
        self.db = Path(self.tmp.name) / "memory.reql"
        self.graph = MemoryGraph.open(self.db)
        self.node = MemoryNode(
            id="function:typed-query-context",
            type="Function",
            label="typed_query_context",
            text="typed query context common service provider",
            canonical_key="function:typed-query-context",
            properties={
                "relative_path": "src/typed_context.py",
                "context_scope": "code",
                "qualified_name": "typed_context.typed_query_context",
                "line_start": 3,
                "line_end": 8,
            },
            salience=0.9,
        )
        self.graph.add_node(self.node)

    def tearDown(self) -> None:
        self.graph.close()
        self.tmp.cleanup()

    def test_service_retrieves_once_and_returns_versioned_result(self) -> None:
        request = QueryContextRequest.from_raw(
            text="typed query context common service",
            scopes=["code"],
            top_k=8,
        )
        with patch.object(self.graph.retrieval, "retrieve", wraps=self.graph.retrieval.retrieve) as retrieve:
            result = self.graph.query_context_result(request)

        self.assertIsInstance(result, ContextResult)
        self.assertEqual(retrieve.call_count, 1)
        self.assertEqual(result.schema_version, 2)
        self.assertEqual(len(result.graph_revision), 64)
        self.assertNotIn("confidence", result.payload)
        self.assertIn("trace_id", result.payload)
        self.assertIn("ranked_nodes", result.payload)
        self.assertIn("seed_node_ids", result.payload)
        self.assertNotIn("context", result.payload)
        self.assertEqual(result.payload["kind"], "code")
        for field in ("followups", "working_set", "contracts", "read_plan", "change_chain", "targeted_reads"):
            self.assertIn(field, result.payload)
        envelope = result.to_dict()
        self.assertIn("payload", envelope)
        self.assertEqual(envelope["schema_version"], 2)
        self.assertEqual(envelope["freshness"]["status"], "unknown")
        self.assertIn("source_revision", envelope)
        self.assertEqual(envelope["graph_revision"], result.graph_revision)
        self.assertEqual(envelope["confidence"]["status"], result.confidence.status)

    def test_graph_revision_is_stable_and_changes_with_relevant_state(self) -> None:
        request = QueryContextRequest.from_raw(
            text="typed query context common service",
            scopes=["code"],
            top_k=8,
        )

        first = self.graph.query_context_result(request)
        second = self.graph.query_context_result(request)
        self.assertEqual(first.graph_revision, second.graph_revision)

        self.graph.store.update_node_fields(
            self.node.id,
            text="typed query context common service provider changed",
        )
        changed = self.graph.query_context_result(request)
        self.assertNotEqual(first.graph_revision, changed.graph_revision)

    def test_assigned_task_recovers_causal_work_with_source_without_claiming_it(self) -> None:
        """Reuse the exact read-only handoff and keep unrelated changes out of it."""
        work = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        provenance = {"agent_id": "agent:planner", "session_id": "session:plan"}
        failure = work.put("failure", "Do not replay the unsupported storage call",
                           rationale="The interface accepts ids only.", **provenance)["node"]
        old = work.put("decision", "Use the old contract", rationale="Initial approach", **provenance)["node"]
        task = work.put("task", "Finish typed context", depends_on=[old["id"], failure["id"]],
                        next_action="Run the supported verification once.", **provenance)["node"]
        current = work.put("decision", "Use the public filter", rationale="Preserve exact lookup semantics.",
                           supersedes=[old["id"]], **provenance)["node"]
        unrelated = work.put("failure", "typed query context provider", rationale="Unrelated trial",
                             files=["src/typed_context.py"], **provenance)["node"]
        request = QueryContextRequest.from_raw(text="typed_query_context", scopes=["code"], task_id=task["id"])
        before = work.show(task["id"])
        result = self.graph.query_context_result(request)
        rows = {row["id"]: row for row in result.payload["engineering_context"]["records"]}
        self.assertEqual(set(rows), {task["id"], failure["id"], current["id"]})
        self.assertEqual(rows[task["id"]]["obsolete_dependencies"], [old["id"]])
        self.assertEqual([row["id"] for row in result.payload["failure_context"]["records"]], [failure["id"]])
        text = self.graph.query_context_service.render(result, brief=True)
        self.assertLess(text.index("Engineering context"), text.index("# REQL Context"))
        self.assertIn("Run the supported verification once.", text)
        self.assertIn("The interface accepts ids only.", text)
        self.assertNotIn("direct prerequisites shown in full", text)
        self.assertNotIn(unrelated["id"], text)
        self.assertEqual(work.show(task["id"]), before)
        self.assertEqual(self.graph.query_context_payload("typed_query_context", scopes=["code"])["graph_revision"],
                         result.graph_revision)

        self.graph.close()
        transferred = call_tool("query_context", {
            "storage_path": str(self.db), "query": "typed_query_context", "code": True, "task_id": task["id"],
        })
        self.assertEqual(transferred["payload"]["engineering_context"], result.payload["engineering_context"])
        self.graph = MemoryGraph.open(self.db)
        with self.assertRaisesRegex(ValueError, "Work record not found"):
            self.graph.query_context_payload("typed_query_context", task_id="work:missing")

    def test_unassigned_brief_compacts_background_work_and_preserves_evidence(self) -> None:
        """Return skimmable work summaries while keeping contracts and failure evidence."""
        work = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        author = {"agent_id": "agent:planner", "session_id": "session:plan", "files": ["src/typed_context.py"]}
        observation = work.put("observation", "Caching reduces repeated typed context reads.",
                               rationale="The previous measurement included the bytes in every response. " * 3,
                               next_action="Measure all responses with an identical query. " * 3, **author)["node"]
        checkpoint = work.put("checkpoint", "Earlier typed context benchmark completed.",
                              rationale="Ran this experiment with a fixed interpreter. " * 3, **author)["node"]
        constraint = work.put("constraint", "Keep verified source spans.",
                              rationale="An edit must still be based on the exact source.", **author)["node"]
        failure = work.put("failure", "Avoid the stale serializer adapter.",
                           rationale="The adapter loses an empty field.", **author)["node"]
        request = QueryContextRequest.from_raw(text="typed_query_context", scopes=["code"])
        service = self.graph.query_context_service
        result = service.execute(request, brief=True)
        brief = service.render(result, brief=True)
        detailed = service.render(result)
        brief_work = brief.split("## Engineering context", 1)[1]
        detailed_work = detailed.split("## Engineering context", 1)[1]
        for row in (observation, checkpoint, constraint, failure):
            self.assertIn(row["id"], brief_work)
            self.assertIn(row["content"], brief_work)
        for row in (observation, checkpoint):
            self.assertNotIn(row["rationale"], brief_work)
            self.assertIn(row["rationale"], detailed_work)
        self.assertNotIn(observation["next_action"], brief_work)
        self.assertIn(constraint["rationale"], brief_work)
        self.assertEqual(brief_work.count(failure["rationale"]), 1)
        self.assertEqual(brief_work.count("reql agent show ID --json"), 1)
        self.assertEqual(work.show(observation["id"])["next_action"], observation["next_action"])
        token_pattern = re.compile(r"\w+|[^\w\s]")
        self.assertLessEqual(len(token_pattern.findall(brief_work)), 0.75 * len(token_pattern.findall(detailed_work)))

    def test_long_handoff_renders_complete_prerequisites_and_shared_failure_once(self) -> None:
        """Keep the attached operational record complete alongside code evidence."""
        work = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        author = {"agent_id": "agent:planner", "session_id": "session:plan"}
        failure = work.put("failure", "Avoid the stale serializer adapter",
                           rationale="The adapter loses an empty field.", **author)["node"]
        task = work.put("task", "Update typed context", rationale="Prior context. " * 250,
                        depends_on=[failure["id"]], **author)["node"]
        request = QueryContextRequest.from_raw(text="typed_query_context", scopes=["code"], task_id=task["id"])
        service = self.graph.query_context_service
        result = service.execute(request, brief=True)
        row, = result.payload["failure_context"]["records"]
        self.assertEqual(row["engineering_context_ref"], failure["id"])
        self.assertNotIn("rationale", row)
        text = service.render(result, brief=True)
        self.assertIn(task["rationale"], text)
        self.assertIn("direct prerequisites shown in full", text)
        self.assertEqual(text.count("The adapter loses an empty field."), 1)
        self.assertIn("evidence unknown", text)
        self.assertNotIn("rationale", row)

    def test_compiled_context_exposes_implementation_links_and_affected_callers(self) -> None:
        """Ordinary code context must reveal existing forwarding and override boundaries."""
        project = Path(self.tmp.name) / "implementation-project"
        project.mkdir()
        (project / "base.py").write_text(
            "class Base:\n    def execute(self, value):\n        return value\n\n"
            "def target(value):\n    return value\n", encoding="utf-8")
        (project / "derived.py").write_text(
            "from base import Base, target\n\nclass Derived(Base):\n"
            "    def execute(self, value):\n        return super().execute(value)\n\n"
            "def proxy(value):\n    return target(value)\n\n"
            "def local_request(value):\n    result = proxy(value)\n    return result\n\n"
            "def other_request(value):\n    result = proxy(value)\n    return result\n", encoding="utf-8")
        (project / "api.py").write_text(
            "from derived import proxy\n\ndef request(value):\n    return proxy(value)\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(project).run.errors)
        for query, relation in (("proxy", "WRAPS"), ("Derived.execute", "OVERRIDES")):
            with self.subTest(query=query):
                result = self.graph.query_context_result(QueryContextRequest.from_raw(text=query, scopes=["code"], top_k=12))
                links = result.payload["impact"]["implementation_links"]
                self.assertTrue(any(row["edge_type"] == relation for row in links))
                rendered = self.graph.query_context_service.render(result)
                self.assertIn("## Change boundaries", rendered)
                self.assertIn(relation + ":", rendered)
                self.assertIn("base.py", rendered.split("## Change boundaries")[1])
                self.assertIn("derived.py", rendered.split("## Change boundaries")[1])
                if query == "proxy":
                    self.assertIn("CALLS:", rendered)
                    self.assertIn("api.py", rendered.split("## Change boundaries")[1])
                    brief = self.graph.query_context_service.render(result, brief=True)
                    grouped = next(line for line in brief.splitlines()
                                   if line.startswith("- CALLS: `derived.py`"))
                    self.assertIn("`local_request` [10-12]", grouped)
                    self.assertIn("`other_request` [14-16]", grouped)
                    self.assertTrue(grouped.endswith("-> `proxy` [7-8]"))
                    self.assertEqual(grouped.count("derived.py"), 1)
                    self.assertIn("CALLS: `request` @ api.py:3-4", brief)
                    self.assertTrue({"derived.local_request", "derived.other_request"}.issubset(
                        row["caller"]["label"] for row in result.payload["impact"]["callers"]))

    def test_document_classification_follows_markup_across_filenames(self) -> None:
        """Compile Markdown based on its markup and keep custom markers as text."""
        project = Path(self.tmp.name) / "document-project"
        project.mkdir()
        markdown = "# Reconciliation guide\n\nReconcile the ledger automatically.\n"
        custom = "=== Ledger handbook ===\nContributors: team\nTags: ledger\n\n== Overview ==\n"
        for name, content in {
            "readme.txt": markdown,
            "operations.txt": markdown,
            "handbook": markdown,
            "faq.txt": "Reconcile the ledger manually.\n",
            "legacy.txt": custom,
            "ledger.md": custom + "\n" + markdown,
        }.items():
            (project / name).write_text(content, encoding="utf-8")
        result = self.graph.compile_project(project, parsing_options=CompilationOptions())
        self.assertFalse(result.run.errors)
        expected = {
            "readme.txt": ("markdown", ["Reconciliation guide"]),
            "operations.txt": ("markdown", ["Reconciliation guide"]),
            "handbook": ("markdown", ["Reconciliation guide"]),
            "faq.txt": ("text", []),
            "legacy.txt": ("text", []),
            "ledger.md": ("markdown", ["Reconciliation guide"]),
        }
        self.assertEqual({item.relative_path for item in result.scan.artifacts}, set(expected))
        for artifact in result.scan.artifacts:
            with self.subTest(path=artifact.relative_path):
                artifact_type, headings = expected[artifact.relative_path]
                self.assertEqual(artifact.artifact_type, artifact_type)
                fragments = self.graph.store.find_nodes_by_property("artifact_id", artifact.id, type_="SourceFragment")
                self.assertEqual(
                    [node.text for node in fragments if node.properties["fragment_type"] == "heading"], headings,
                )
                if artifact.relative_path in {"legacy.txt", "ledger.md"}:
                    self.assertTrue(any("== Overview ==" in node.text for node in fragments))

    def test_related_files_require_indexed_evidence_for_each_role(self) -> None:
        """Use the same result rules for FAQ terms and arbitrary query terms."""
        project = Path(self.tmp.name) / "context-project"
        project.mkdir()
        evidence = "Ledger reconciliation, bilancio tracciabile, FAQ"
        for name, content in {
            "handbook.md": "# Operating guide\n\n" + evidence + "\n",
            "balance.py": f'def reconcile():\n    """{evidence}."""\n    return 0\n',
            "messages.po": f'msgid "{evidence}"\nmsgstr ""\n',
            "unrelated.pot": 'msgid "Export shipment manifest"\nmsgstr ""\n',
        }.items():
            (project / name).write_text(content, encoding="utf-8")
        result = self.graph.compile_project(project, parsing_options=CompilationOptions())
        self.assertFalse(result.run.errors)

        for query in ("ledger reconciliation", "bilancio tracciabile", "FAQ"):
            with self.subTest(query=query):
                related = self.graph.query_context_payload(query)["payload"]["related_files"]
                self.assertEqual(
                    [(row["path"], row["role"], row["action"], row["reason"]) for row in related],
                    [
                        ("handbook.md", "documentation", "open", "matching terms in project documentation"),
                        ("balance.py", "implementation", "review", "matching terms in project code"),
                        ("messages.po", "translation_catalog", "review", "matching terms in the project translation catalog"),
                    ],
                )
                self.assertTrue(all(row["matched_terms"] and row["node_ids"] for row in related))

    def test_exact_document_path_wins_across_query_surfaces(self) -> None:
        self.graph.close()
        project = Path(self.tmp.name) / "project"
        project.mkdir()
        (project / "CONTRIBUTING.md").write_text(
            "# Contribution Guide\n\n"
            "Use the focused review workflow for patches.\n\n"
            + "\n".join(f"## Topic {index}\n\nGuidance term {index}." for index in range(80)),
            encoding="utf-8",
        )
        source = project / "pipeline.py"
        source.write_text(
            'class PipelineSymbol:\n    """One source symbol contributing to a pipeline component."""\n',
            encoding="utf-8",
        )
        self.graph = open_graph_with_documents(self.db)
        self.graph.compile_project(project)

        context = self.graph.query_context_payload("CONTRIBUTING.md", top_k=12)
        docs_context = self.graph.query_context_payload("CONTRIBUTING.md", scopes=["docs"], top_k=12)
        memories = self.graph.query_memories_payload("CONTRIBUTING.md", limit=8)
        query_graph = self.graph.query_graph("CONTRIBUTING.md", top_k=12, max_depth=2)
        document_content = self.graph.query_context_payload(
            "focused review workflow patches",
            scopes=["docs"],
            top_k=12,
        )
        code_context = self.graph.query_context_payload("pipeline.py", scopes=["code"], top_k=12)
        wrong_scope = self.graph.query_context_payload("CONTRIBUTING.md", scopes=["code"], top_k=12)
        raw_retrieval = self.graph.query(
            "RETRIEVE 'CONTRIBUTING.md' LIMIT 5 RETURN id,type,score,relative_path"
        )
        located = self.graph.locate("CONTRIBUTING.md")

        self.assertEqual(context["payload"]["kind"], "general")
        self.assertEqual(context["payload"]["results"][0]["location"], "CONTRIBUTING.md")
        self.assertEqual(docs_context["payload"]["kind"], "general")
        self.assertEqual(docs_context["confidence"]["status"], "sufficient")
        self.assertEqual(docs_context["payload"]["results"][0]["location"], "CONTRIBUTING.md")
        self.assertEqual(memories["memories"][0]["location"], "CONTRIBUTING.md")
        self.assertTrue(query_graph["seed_nodes"])
        self.assertTrue(
            all(node["properties"].get("relative_path") == "CONTRIBUTING.md" for node in query_graph["seed_nodes"])
        )
        self.assertEqual(document_content["confidence"]["status"], "sufficient")
        self.assertTrue(document_content["payload"]["results"][0]["location"].startswith("CONTRIBUTING.md"))
        self.assertEqual(code_context["payload"]["kind"], "code")
        self.assertEqual(code_context["payload"]["working_set"][0]["path"], "pipeline.py")
        self.assertEqual(wrong_scope["confidence"]["status"], "insufficient")
        self.assertEqual(wrong_scope["payload"]["working_set"], [])
        self.assertEqual(raw_retrieval.rows[0]["relative_path"], "CONTRIBUTING.md")
        self.assertEqual(located["matches"][0]["relative_path"], "CONTRIBUTING.md")
        self.assertNotIn("pipeline.py", self.graph.query_context("CONTRIBUTING.md", top_k=12))
