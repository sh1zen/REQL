"""Exercise source-backed context through compilation and providers."""
from __future__ import annotations

import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from api import MemoryGraph
from mcp.tools import MCPToolError, list_tools, query_context as mcp_query_context
from memory.artifacts.compiler import ArtifactCompiler
from memory.artifacts.options import CompilationOptions
from memory.artifacts.revision import RevisionRepository
from memory.cli import main
from memory.domain.query_context import QueryContextRequest
from memory.extraction.deterministic import DeterministicExtractor
from memory.services.coordination import CoordinationStore
from memory.services.retrieval.context.source import _read_bounded_source, capture_source_excerpts


class SourceContextTests(unittest.TestCase):
    """Keep source excerpts accurate, bounded, optional, and independent of the cwd."""

    def setUp(self) -> None:
        test_root = Path.cwd() / ".tmp" / "regression-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.root = Path(self.temp.name) / "catalog"
        self.root.mkdir()
        self.path = self.root / "tax_rules.py"
        self.body = ('def calculate_invoice_total(prices):\n'
                     '    """Apply invoice rules to the supplied prices."""\n'
                     '    total = sum(prices)\n'
                     + ''.join(f'    total += {value}\n' for value in range(20))
                     + '    return total\n')
        self.path.write_text(self.body, encoding="utf-8")
        self.db = Path(self.temp.name) / ".reql" / "memory.reql"
        self.graph = MemoryGraph.open(self.db)
        compiled = self.graph.compile_project(self.root)
        self.assertFalse(compiled.run.errors)
        self.artifact_id = compiled.scan.artifacts[0].id
        self.project_id = compiled.scan.project.id
        self.node = next(node for node in self.graph.store.all_nodes()
                         if node.type == "Function" and node.properties.get("name") == "calculate_invoice_total")

    def tearDown(self) -> None:
        self.graph.close()
        self.temp.cleanup()

    def test_assigned_markdown_without_a_passage_has_a_bounded_verified_outline(self) -> None:
        """Give a focused way to read an assigned document without echoing the whole file."""
        document = self.root / "README.md"
        document.write_text("# Guide\n\n" + "".join(
            f"## Section {index}\n\nLong prose body {index}\n\n" for index in range(11)), encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root, parsing_options=CompilationOptions(ingest_documents=True)).run.errors)
        board = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        task = board.put("task", "Document invoice totals", files=[self.path.name, document.name],
                         agent_id="agent:planner", session_id="session:plan")["node"]
        service = self.graph.query_context_service
        request = QueryContextRequest.from_raw(text="calculate_invoice_total", scopes=["code"], task_id=task["id"])
        result = service.execute(request, brief=True)
        outline = result.payload["task_documentation_outline"]
        self.assertEqual(len(outline["headings"]), 8)
        self.assertEqual(outline["omitted"], 3)
        self.assertEqual(outline["headings"][0], {"title": "Section 0", "line_start": 3, "line_end": 6})
        rendered = service.render(result, brief=True)
        self.assertIn("Assigned document sections", rendered)
        self.assertIn("3-6 Section 0", rendered)
        self.assertNotIn("Long prose body", rendered)
        document.write_text("# Changed\n", encoding="utf-8")
        self.assertNotIn("task_documentation_outline", service.execute(request, brief=True).payload)

    def _payload(self, **kwargs):
        """Use the public Python boundary with explicit source scope."""
        return self.graph.query_context_payload("calculate_invoice_total", scopes=["code"], **kwargs)

    def _read(self, start: int = 1, end: int = 24):
        """Construct a graph-provenanced source request for boundary tests."""
        return {"path": "tax_rules.py", "line_start": start, "line_end": end, "node_id": self.node.id}

    def test_context_preserves_read_spans_and_supported_followups(self) -> None:
        """Return source locations without linking to the removed command."""
        code = self._payload()["payload"]
        read = next(row for row in code["read_plan"] if row["node_id"] == self.node.id)
        self.assertEqual((read["path"], read["line_start"], read["line_end"]), (self.path.name, 1, 24))
        self.assertNotIn("command", read)

        docs = self.root / "invoice.md"
        docs.write_text("# Invoice policy\n\nApply invoice rules.\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(
            self.root, parsing_options=CompilationOptions(ingest_documents=True),
        ).run.errors)
        general = self.graph.query_context_payload("invoice policy", scopes=["docs"])["payload"]
        self.assertTrue(general["results"])
        explore = self.graph.query_explore("calculate_invoice_total")
        cleanup = self._payload(mode="cleanup")["payload"]
        for name, payload in (("code", code), ("docs", general), ("explore", explore), ("cleanup", cleanup)):
            with self.subTest(projection=name):
                commands = [item["command"] for item in payload["followups"]]
                if name != "code":
                    self.assertTrue(any(command.startswith("reql query_graph ") for command in commands))
                self.assertFalse(any(command.startswith("reql inspect ") for command in commands))

        text = self.graph.query_context("calculate_invoice_total", mode="cleanup", scopes=["code"])
        self.assertIn("research graph:", text)
        self.assertNotIn("reql inspect", text)

    def test_opt_in_returns_complete_verified_body(self) -> None:
        payload = self._payload(include_source=True)
        snippets = payload["payload"]["snippets"]
        row = next(item for item in snippets if item.get("text"))
        self.assertEqual(row["text"], self.body.rstrip("\n"))
        self.assertEqual(row["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(row["source_status"], "verified")
        self.assertFalse(row["truncated"])
        self.assertTrue(payload["payload"]["read_plan"][0]["snippet_embedded"])
        rendered = self.graph.query_context("calculate_invoice_total", scopes=["code"], include_source=True)
        self.assertIn("Current source", rendered)
        self.assertIn("    return total", rendered)
        compiled = self.graph.compile_project(self.root, cache_enabled=False)
        self.assertFalse(compiled.run.errors)
        rows = self._payload(include_source=True)["payload"]["snippets"]
        self.assertTrue(any(item.get("text") == self.body.rstrip("\n") for item in rows))

    def test_default_does_not_capture_source_or_change_the_payload(self) -> None:
        """Return freshness from the committed revision without excerpt capture."""
        with patch("memory.services.retrieval.context.projections.code.capture_source_excerpts",
                   side_effect=AssertionError("unexpected source capture")):
            original = self._payload()
            explicit = self._payload(include_source=False)
            rendered = self.graph.query_context("calculate_invoice_total", scopes=["code"])
        for result in (original, explicit):
            result["payload"].pop("trace_id", None)
        self.assertEqual(original, explicit)
        self.assertNotIn("source_mode", original["payload"])
        self.assertIsNotNone(original["source_revision"])
        self.assertEqual(original["freshness"]["status"], "current")
        self.assertNotIn("snapshot", original["freshness"])
        self.assertIn("Graph freshness: current", rendered)
        self.assertNotIn("snapshot", rendered.casefold())

    def test_retired_monitor_state_does_not_override_source_freshness(self) -> None:
        """A legacy sidecar cannot mark matching committed source as stale."""
        storage = self.graph.store.path
        storage.with_name(f"{storage.name}.watch.json").write_text(
            '{"format":"reql-watch-state-v1","status":"refreshing","pending_paths":3}', encoding="utf-8",
        )
        result = self._payload(include_source=True)
        self.assertEqual(result["freshness"]["status"], "current")
        self.assertEqual(result["freshness"]["pending_paths"], 0)

    def test_exact_path_bypasses_only_the_builtin_extractor(self) -> None:
        """An indexed path skips unused built-in seeds while preserving custom extraction."""
        request = QueryContextRequest.from_raw(text="tax_rules.py", scopes=["code"])
        with patch.object(self.graph.retrieval.extractor, "extract", side_effect=AssertionError("unused")):
            result = self.graph.query_context_result(request)
        self.assertEqual(result.payload["working_set"][0]["path"], "tax_rules.py")

        class CustomExtractor(DeterministicExtractor):
            """Represent an explicitly supplied semantic extractor."""

        custom = CustomExtractor()
        with patch.object(self.graph.retrieval, "extractor", custom):
            with patch.object(custom, "extract", wraps=custom.extract) as extract:
                self.graph.query_context_result(request)
        extract.assert_called_once_with("tax_rules.py")

    def test_brief_returns_one_complete_verified_owner(self) -> None:
        """Use one verified source read, and keep normal JSON independent of brief text."""
        request = QueryContextRequest.from_raw(text=self.node.properties["name"], scopes=["code"])
        with patch("memory.services.retrieval.context.source._read_bounded_source",
                   wraps=_read_bounded_source) as read:
            result = self.graph.query_context_service.execute(request, brief=True)
        read.assert_called_once_with(self.path.resolve())
        row = next(item for item in result.payload["snippets"] if item.get("text"))
        self.assertEqual(row["text"], self.body.rstrip("\n"))
        self.assertFalse(row["truncated"])
        text = self.graph.query_context_service.render(result, brief=True)
        self.assertIn("Current source", text)
        self.assertIn("(complete, verified)", text)
        self.assertEqual(text.count("return total"), 1)
        self.assertNotIn("source_revision=", text)
        self.assertIsNotNone(result.source_revision)

        # Additional projection roles can refer to the same verified body.
        with_roles = replace(result, payload={**result.payload, "task_local_source": [row], "task_contract_source": [row]})
        text = self.graph.query_context_service.render(with_roles, brief=True)
        self.assertEqual(text.count("return total"), 1)
        self.assertNotIn("Referenced declarations", text)

        self.graph.close()
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            arguments = ["query_context", "--query", self.node.properties["name"], "--code"]
            for options in (["--brief"], ["--json"], ["--brief", "--json"]):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main([*arguments, *options]), 0)
                if options == ["--brief"]:
                    self.assertIn("Current source", output.getvalue())
                else:
                    envelope = json.loads(output.getvalue())
                    envelope["payload"].pop("trace_id")
                    if options == ["--json"]:
                        expected = envelope
                    else:
                        self.assertEqual(envelope, expected)
        finally:
            os.chdir(previous)

    def test_brief_locates_one_current_markdown_section_for_an_explicit_reference(self) -> None:
        """Follow a verified owner's project-relative anchor within the current source revision."""
        self.path.write_text(self.body.replace("Apply invoice rules to the supplied prices.",
                                               "See docs/invoice.md#invoice-policy for the contract."), encoding="utf-8")
        doc = self.root / "docs" / "invoice.md"
        doc.parent.mkdir()
        section = "## Invoice policy\n\nUse the supplied tax rate.\n\n### Rounding\n\nRound once at the end."
        document = f"# Invoices\n\n{section}\n\n## Billing reports\n\nSkip this unrelated section.\n"
        doc.write_text(document, encoding="utf-8")
        request = QueryContextRequest.from_raw(text=self.node.properties["name"], scopes=["code"])
        disabled = CompilationOptions(ingest_documents=False)
        self.assertFalse(self.graph.compile_project(self.root, parsing_options=disabled).run.errors)
        self.assertNotIn("linked_section", self.graph.query_context_service.execute(request, brief=True).payload)
        options = CompilationOptions(ingest_documents=True)
        self.assertFalse(self.graph.compile_project(self.root, parsing_options=options).run.errors)
        with patch("memory.services.retrieval.context.source._read_bounded_source",
                   wraps=_read_bounded_source) as read:
            result = self.graph.query_context_service.execute(request, brief=True)
        self.assertEqual([call.args[0] for call in read.call_args_list], [self.path.resolve(), doc.resolve()])
        linked = result.payload["linked_section"]
        self.assertEqual(linked["source_status"], "verified")
        self.assertEqual(linked["sha256"], hashlib.sha256(doc.read_bytes()).hexdigest())
        self.assertEqual((linked["line_start"], linked["line_end"]), (3, 9))
        rendered = self.graph.query_context_service.render(result, brief=True)
        self.assertLess(rendered.index("## Current source"), rendered.index("## Linked guidance"))
        self.assertIn("docs/invoice.md [3-9]` (verified section)", rendered)
        self.assertNotIn("Use the supplied tax rate", rendered)
        self.assertNotIn("Skip this unrelated section", rendered)
        self.assertNotIn("linked_section", self.graph.query_context_service.execute(request).payload)

        doc.write_text(document.replace("round", "truncate") + "\nChanged after indexing.\n", encoding="utf-8")
        self.assertNotIn("linked_section", self.graph.query_context_service.execute(request, brief=True).payload)
        doc.write_text(document + "\n## Invoice policy\n\nAmbiguous heading.\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root, parsing_options=options).run.errors)
        self.assertNotIn("linked_section", self.graph.query_context_service.execute(request, brief=True).payload)

    def test_assigned_brief_supplies_one_complete_current_test_file(self) -> None:
        """Keep the edit-ready test bounded, source-backed and restricted to the task."""
        tests = self.root / "tests"
        tests.mkdir()
        assigned = tests / "test_totals.py"
        body = "def test_totals():\n    assert sum([2, 3]) == 5\n"
        assigned.write_bytes(body.replace("\n", "\r\n").encode("utf-8-sig"))
        oversized = tests / "test_bulk.py"
        oversized.write_text("# fixture\n" * 600, encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        work = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        task = work.put("task", "Verify invoice totals", agent_id="agent:owner", session_id="session:work",
                        files=["tests/test_bulk.py", "tests/test_totals.py"])["node"]
        request = QueryContextRequest.from_raw(text=self.node.properties["name"], scopes=["code"],
                                               task_id=task["id"])
        result = self.graph.query_context_service.execute(request, brief=True)
        captured = result.payload["task_test_source"]
        self.assertEqual(captured["text"], body.rstrip())
        self.assertEqual(captured["sha256"], hashlib.sha256(assigned.read_bytes()).hexdigest())
        self.assertEqual((captured["encoding"], captured["line_endings"]), ("UTF-8 BOM", "CRLF"))
        self.assertIn("tests/test_totals.py [1-2]` (whole file, verified) [UTF-8 BOM, line endings: CRLF]",
                      self.graph.query_context_service.render(result, brief=True))
        self.assertNotIn("task_test_source", self.graph.query_context_result(request).payload)
        assigned.write_text("def test_totals():\n    assert True\n", encoding="utf-8")
        self.assertNotIn("task_test_source", self.graph.query_context_service.execute(request, brief=True).payload)

    def test_assigned_brief_embeds_small_current_guidance_without_reading_twice(self) -> None:
        """Supply the verified section body and patch format only for assigned work."""
        source = self.body.replace("Apply invoice rules to the supplied prices.",
                                   "See docs/invoice.md#invoice-policy for the contract.")
        self.path.write_bytes(source.replace("\n", "\r\n").encode("utf-8-sig"))
        doc = self.root / "docs" / "invoice.md"
        doc.parent.mkdir()
        section = "## Invoice policy\n\nUse the supplied tax rate."
        unrelated = "\n\n## Reporting\n\nOmit the reporting guidance.\n"
        doc.write_text(section + unrelated, encoding="utf-8")
        options = CompilationOptions(ingest_documents=True)
        self.assertFalse(self.graph.compile_project(self.root, parsing_options=options).run.errors)
        work = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        task = work.put("task", "Apply invoice policy", agent_id="agent:owner", session_id="session:work",
                        files=[self.path.name])["node"]
        request = QueryContextRequest.from_raw(text=self.node.properties["name"], scopes=["code"], task_id=task["id"])
        service = self.graph.query_context_service
        with patch("memory.services.retrieval.context.source._read_bounded_source",
                   wraps=_read_bounded_source) as read:
            result = service.execute(request, brief=True)
        self.assertEqual([call.args[0] for call in read.call_args_list], [self.path.resolve(), doc.resolve()])
        self.assertEqual(result.payload["linked_section"]["text"], section)
        primary = result.payload["snippets"][0]
        self.assertEqual((primary["encoding"], primary["line_endings"]), ("UTF-8 BOM", "CRLF"))
        rendered = service.render(result, brief=True)
        self.assertIn("Use the supplied tax rate.", rendered)
        self.assertNotIn("Omit the reporting guidance.", rendered)
        self.assertIn("[UTF-8 BOM, line endings: CRLF]", rendered)
        self.assertNotIn("linked_section", self.graph.query_context_result(request).payload)

        doc.write_text(section + "\n\n" + "Long guidance.\n" * 30 + unrelated, encoding="utf-8")
        self.assertNotIn("linked_section", service.execute(request, brief=True).payload)
        self.assertFalse(self.graph.compile_project(self.root, parsing_options=options).run.errors)
        linked = service.execute(request, brief=True).payload["linked_section"]
        self.assertNotIn("text", linked)
        self.assertEqual(linked["source_status"], "verified")

    def test_assigned_brief_resolves_only_current_pinned_ranges_for_task_files(self) -> None:
        """Recover the exact saved edit evidence without trusting shifted or external lines."""
        support = self.root / "format_rules.py"
        support.write_bytes(b"MODE = 1\n\nSTABLE_SETTING = 2\n")
        digest = hashlib.sha256(support.read_bytes()).hexdigest()
        same_body_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        reference = f"`format_rules.py#L1-L2@{digest}`"
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        work = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        task = work.put("task", "Change the invoice mode", agent_id="agent:owner", session_id="session:work",
                        files=["format_rules.py", self.path.name],
                        next_action=(f"{reference} {reference} "
                                     f"`{self.path.name}#L1-L2@{same_body_hash}` "
                                     f"`missing.py#L1-L2@{digest}`"))["node"]
        request = QueryContextRequest.from_raw(text=self.node.properties["name"], scopes=["code"],
                                               task_id=task["id"])
        result = self.graph.query_context_service.execute(request, brief=True)
        spans = result.payload["task_edit_spans"]["spans"]
        self.assertEqual(len(spans), 1)
        self.assertEqual((spans[0]["path"], spans[0]["sha256"], spans[0]["text"]),
                         (support.name, digest, "MODE = 1\n"))
        self.assertEqual((spans[0]["encoding"], spans[0]["line_endings"]), ("UTF-8", "LF"))
        text = self.graph.query_context_service.render(result, brief=True)
        self.assertIn("Assigned edit spans", text)
        self.assertIn("`format_rules.py#L1-L2`", text)
        self.assertNotIn(reference, text)
        self.assertIn(f"missing.py#L1-L2@{digest}", text)
        self.assertIn(reference, result.payload["engineering_context"]["records"][0]["next_action"])
        low_confidence = replace(result, confidence=replace(result.confidence, status="insufficient"))
        self.assertIn(reference, self.graph.query_context_service.render(low_confidence, brief=True))
        self.assertEqual(text.count("MODE = 1"), 1)
        self.assertNotIn("STABLE_SETTING", text)
        self.assertNotIn("task_edit_spans", self.graph.query_context_result(request).payload)
        support.write_text("MODE = 7\n\nSTABLE_SETTING = 2\n", encoding="utf-8")
        for compile_first in (False, True):
            if compile_first:
                self.assertFalse(self.graph.compile_project(self.root).run.errors)
            result = self.graph.query_context_service.execute(request, brief=True)
            row = result.payload["task_edit_spans"]["spans"][0]
            self.assertEqual(row["source_status"], "stale")
            self.assertNotIn("text", row)
            self.assertIn(reference, self.graph.query_context_service.render(result, brief=True))

    def test_assigned_ranges_replace_covered_owner_and_share_its_budget(self) -> None:
        """Spend the space from a repeated owner on other pinned task files."""
        self.path.write_text(self.body + "\nMODE = 1\n", encoding="utf-8")
        tests = self.root / "tests"
        tests.mkdir()
        test_path = tests / "test_assignment.py"
        test_path.write_text("# regression setup\n" * 20 + "# " + "x" * 4700 + "\n", encoding="utf-8")
        guide = self.root / "guide.md"
        guide.write_text("# Invoice guide\n" + "Use current totals.\n" * 19, encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        files = [(self.path, 26), (test_path, 20), (guide, 20)]
        paths = [path.relative_to(self.root).as_posix() for path, _ in files]
        references = " ".join(f"`{relative}#L1-L{end}@{hashlib.sha256(path.read_bytes()).hexdigest()}`"
                              for relative, (path, end) in zip(paths, files))
        work = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        task = work.put("task", "Apply the invoice changes", agent_id="agent:owner", session_id="session:work",
                        files=paths, next_action=references)["node"]
        request = QueryContextRequest.from_raw(text=self.node.properties["name"], scopes=["code"], task_id=task["id"])
        service = self.graph.query_context_service
        result = service.execute(request, brief=True)
        spans = result.payload["task_edit_spans"]["spans"]
        self.assertEqual([item["path"] for item in spans], paths)
        self.assertTrue(all(item["source_status"] == "verified" for item in spans))
        self.assertTrue(result.payload["snippets"])
        rendered = service.render(result, brief=True)
        self.assertEqual(rendered.count("def calculate_invoice_total(prices):"), 1)
        self.assertIn("guide.md [1-20]", rendered)
        self.assertIn("MODE = 1", rendered)
        self.assertNotIn("assigned source budget exceeded", rendered)

        targets = [
            {"kind": "test", "path": "tests/test_unrelated.py", "evidence_kind": "candidate"},
            {"kind": "test", "path": "tests/test_confirmed.py", "evidence_kind": "static"},
        ]
        projected = replace(result, payload={**result.payload, "test_targets": targets})
        rendered = service.render(projected, brief=True)
        self.assertNotIn("tests/test_unrelated.py", rendered)
        self.assertIn("tests/test_confirmed.py", rendered)
        self.assertEqual(projected.payload["test_targets"], targets)

    def test_pinned_markdown_heading_includes_complete_bounded_example(self) -> None:
        guide = self.root / "README.md"
        guide.write_bytes(b"# Invoice\r\n\r\n## Example\r\n\r\nCall total().\r\n\r\n## Other\r\nIgnore this.\r\n")
        digest = hashlib.sha256(guide.read_bytes()).hexdigest()
        self.assertFalse(self.graph.compile_project(self.root, parsing_options=CompilationOptions(ingest_documents=True)).run.errors)
        board = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        task = board.put("task", "Document the example", files=[guide.name],
                         next_action=f"`README.md#L1-L3@{digest}`", agent_id="agent:owner",
                         session_id="session:work")["node"]
        request = QueryContextRequest.from_raw(text=self.node.properties["name"], scopes=["code"], task_id=task["id"])
        service = self.graph.query_context_service
        result = service.execute(request, brief=True)
        row, = result.payload["task_edit_spans"]["spans"]
        self.assertEqual((row["pinned_line_end"], row["line_end"]), (3, 5))
        self.assertIn("\n\nCall total().", row["text"])
        self.assertNotIn("Ignore this.", row["text"])
        self.assertIn("Apply shown line edits and refresh with `reql edit --plan -`", service.render(result, brief=True))

        guide.write_bytes(b"# Invoice\r\n\r\n## Example\r\n\r\n```python\r\ntotal()\r\n```\r\n## Other\r\n")
        digest = hashlib.sha256(guide.read_bytes()).hexdigest()
        self.assertFalse(self.graph.compile_project(self.root, parsing_options=CompilationOptions(ingest_documents=False)).run.errors)
        board.put(record_id=task["id"], expected_revision=task["revision"], next_action=f"`README.md#L1-L4@{digest}`",
                  agent_id="agent:owner", session_id="session:work")
        result = service.execute(request, brief=True)
        row, = result.payload["task_edit_spans"]["spans"]
        self.assertEqual((row["pinned_line_end"], row["line_end"]), (4, 7))
        self.assertIn("```python\ntotal()\n```", row["text"])
        self.assertIn("\n````\n" + row["text"] + "\n````\n", service.render(result, brief=True))

    def test_assigned_brief_recovers_verified_contracts_from_a_prerequisite(self) -> None:
        """Follow exact decision evidence once and keep it separate from edit spans."""
        adapter = self.root / "rules_client.py"
        adapter.write_text("from rows import DecisionRow\n\n"
                           "class RuleClient:\n"
                           "    def check(self, *, price: int) -> list[DecisionRow]:\n"
                           "        return [DecisionRow(price)]\n\n"
                           "    def reset(self):\n"
                           "        return None\n\n"
                           "    def unrelated(self):\n"
                           "        return None\n", encoding="utf-8")
        model = self.root / "rows.py"
        model.write_text("from dataclasses import dataclass\n\n@dataclass\n"
                         "class DecisionRow:\n    price: int\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        board = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        author = {"agent_id": "agent:owner", "session_id": "session:work"}
        decision = board.put("decision", "After RuleClient.reset, use RuleClient.check(price=...) for invoice rules",
                             rationale="Use the price field on DecisionRow.", files=[adapter.name], **author)["node"]
        board.put("decision", "Use RuleClient.unrelated() for invoice rules",
                  rationale="For another task only.", files=[adapter.name], **author)
        task = board.put("task", "Update invoice rules", depends_on=[decision["id"]], files=[self.path.name],
                         **author)["node"]
        service = self.graph.query_context_service
        request = QueryContextRequest.from_raw(text=self.node.properties["name"], scopes=["code"], task_id=task["id"])
        with patch("memory.services.retrieval.context.source._read_bounded_source", wraps=_read_bounded_source) as reads:
            result = service.execute(request, brief=True)
        paths = [call.args[0] for call in reads.call_args_list]
        self.assertEqual(paths.count(adapter), 1)
        self.assertEqual(paths.count(model), 1)
        call, returned, reset = result.payload["task_contract_source"]
        self.assertEqual((call["path"], call["line_start"], call["line_end"]), (adapter.name, 4, 5))
        self.assertEqual(call["sha256"], hashlib.sha256(adapter.read_bytes()).hexdigest())
        self.assertEqual(returned["path"], model.name)
        self.assertIn("price: int", returned["text"])
        self.assertIn("def reset(", reset["text"])
        self.assertNotIn("task_edit_spans", result.payload)
        rendered = service.render(result, brief=True)
        self.assertIn("Referenced declarations", rendered)
        self.assertEqual(rendered.count("def check("), 1)
        self.assertNotIn("def unrelated(", rendered)
        self.assertNotIn("task_contract_source", service.execute(request).payload)
        self.assertNotIn("task_contract_source", service.execute(replace(request, task_id=None), brief=True).payload)

        # The exact name in the annotation cannot safely select either declaration.
        (self.root / "other_rows.py").write_text("class DecisionRow:\n    amount: str\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        call, reset = service.execute(request, brief=True).payload["task_contract_source"]
        self.assertEqual(call["path"], adapter.name)
        self.assertEqual(reset["path"], adapter.name)
        adapter.write_text(adapter.read_text(encoding="utf-8").replace("price: int", "price: str"), encoding="utf-8")
        self.assertNotIn("task_contract_source", service.execute(request, brief=True).payload)

    def test_assigned_brief_recovers_clear_owner_when_global_brief_has_no_body(self) -> None:
        """Named edit files can provide a verified body even for a broad task query."""
        worker = self.root / "policy.py"
        body = ('def register_policy(rule):\n'
                '    """Register the invoice validator for this policy."""\n'
                '    return rule\n')
        worker.write_text(body, encoding="utf-8")
        guide = self.root / "guide.md"
        guide.write_text("# Guide\n\nReview the validator contract.\n", encoding="utf-8")
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_policy.py").write_text("def test_policy():\n    assert True\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        digest = hashlib.sha256(guide.read_bytes()).hexdigest()
        board = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        task = board.put("task", "Edit the validator policy", agent_id="agent:owner", session_id="session:work",
                         files=[worker.name, "tests/test_policy.py", guide.name],
                         next_action=f"See `{guide.name}#L1-L3@{digest}`.")["node"]
        query = "invoice validator policy registration onboarding configuration"
        service = self.graph.query_context_service
        request = QueryContextRequest.from_raw(text=query, scopes=["code"], task_id=task["id"])
        self.assertFalse(service.execute(replace(request, task_id=None), brief=True).payload["snippets"])
        result = service.execute(request, brief=True)
        excerpt, = result.payload["snippets"]
        self.assertEqual((excerpt["path"], excerpt["text"], excerpt["source_status"]),
                         (worker.name, body.rstrip(), "verified"))
        self.assertEqual(result.payload["task_test_source"]["path"], "tests/test_policy.py")
        self.assertEqual(result.payload["task_edit_spans"]["spans"][0]["path"], guide.name)
        rendered = service.render(result, brief=True)
        self.assertIn("Current source", rendered)
        self.assertEqual(rendered.count("def register_policy(rule):"), 1)

        worker.write_text(body.replace("return rule", "return None"), encoding="utf-8")
        stale = service.execute(request, brief=True)
        self.assertEqual(stale.freshness.status, "stale")
        self.assertEqual(stale.payload["snippets"][0]["source_status"], "stale")
        self.assertNotIn("text", stale.payload["snippets"][0])

        worker.write_text(body + "\n" + body.replace("register_policy", "prepare_policy"), encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        self.assertFalse(service.execute(request, brief=True).payload["snippets"])

    def test_assigned_exact_brief_and_follow_up_prefer_scoped_symbols(self) -> None:
        """Display exact in-scope evidence, an oversized span, and no repeated work record."""
        assigned = self.root / "assigned.py"
        assigned.write_text("LIMIT = 7\ndef approach():\n    return LIMIT\n\n"
                            "def long_owner():\n" + "    total = 7\n" * 40 + "    return total\n",
                            encoding="utf-8")
        (self.root / "other.py").write_text("def approach():\n    return 'unassigned sentinel'\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        board = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        task = board.put("task", "Change the assigned rule", files=[assigned.name],
                         agent_id="agent:worker", session_id="session:worker")["node"]
        request = QueryContextRequest.from_raw(text="approach LIMIT long_owner", scopes=["code"], task_id=task["id"])
        service = self.graph.query_context_service
        result = service.execute(request, brief=True)
        rendered = service.render(result, brief=True)
        self.assertEqual(rendered.count("def approach():"), 1)
        self.assertIn("LIMIT = 7", rendered)
        self.assertIn("long_owner (indexed range)", rendered)
        self.assertNotIn("unassigned sentinel", rendered)
        self.assertNotIn("other.py", rendered)
        follow_up = service.execute(request, brief=True, follow_up=True)
        text = service.render(follow_up, brief=True)
        self.assertIn("def approach():", text)
        self.assertIn("long_owner (indexed range)", text)
        self.assertNotIn("Engineering context", text)
        self.assertNotIn("Change the assigned rule", text)

        assigned.write_text(assigned.read_text(encoding="utf-8").replace("LIMIT = 7", "LIMIT = 8"),
                            encoding="utf-8")
        stale = service.execute(request, brief=True, follow_up=True)
        self.assertEqual(stale.freshness.status, "stale")
        self.assertTrue(all("text" not in row for row in stale.payload["snippets"]))
        self.assertNotIn("LIMIT = 8", service.render(stale, brief=True))

    def test_assigned_brief_keeps_named_declaration_with_verified_method(self) -> None:
        worker = self.root / "policy.py"
        worker.write_text('class InvoicePolicy:\n    mode = 1\n\n'
                          '    def register_policy(self, rule):\n'
                          '        """Register an invoice validator for this policy."""\n'
                          '        return rule\n', encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        board = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        task = board.put("task", "Update the invoice policy", agent_id="agent:owner", session_id="session:work",
                         files=[worker.name], next_action="Update InvoicePolicy's mode and validator.")["node"]
        query = "invoice validator policy registration onboarding configuration"
        request = QueryContextRequest.from_raw(text=query, scopes=["code"], task_id=task["id"])
        service = self.graph.query_context_service
        result = service.execute(request, brief=True)
        snippets = result.payload["snippets"]
        self.assertEqual([row["line_start"] for row in snippets], [4, 1])
        self.assertEqual(snippets[1]["text"], "class InvoicePolicy:\n    mode = 1\n")
        self.assertEqual(snippets[0]["sha256"], snippets[1]["sha256"])
        self.assertEqual(service.render(result, brief=True).count("class InvoicePolicy:"), 1)
        other = board.put("task", "Update the other policy", agent_id="agent:owner", session_id="session:work",
                          files=[worker.name], next_action="Update OtherPolicy's mode and validator.")["node"]
        self.assertEqual(len(service.execute(replace(request, task_id=other["id"]), brief=True).payload["snippets"]), 1)

    def test_assigned_brief_completes_small_pinned_source_without_repeating_it(self) -> None:
        worker = self.root / "routing.py"
        rows = ["def parse_route(path):", '    """Parse a routing path."""'] + ["    path = path.strip()"] * 58
        worker.write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        digest = hashlib.sha256(worker.read_bytes()).hexdigest()
        board = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        task = board.put("task", "Update routing", agent_id="agent:owner", session_id="session:work",
                         files=[worker.name], next_action=f"`{worker.name}#L10-L48@{digest}`")["node"]
        query = "parse_route"
        request = QueryContextRequest.from_raw(text=query, scopes=["code"], task_id=task["id"])
        service = self.graph.query_context_service
        result = service.execute(request, brief=True)
        self.assertEqual(result.payload["task_edit_spans"]["spans"][0]["line_start"], 10)
        whole = result.payload["task_source_file"]
        self.assertEqual(whole["sha256"], digest)
        self.assertEqual(whole["text"].splitlines(), rows)
        unrelated = {"path": "tax_rules.py", "line_start": 1, "line_end": 24, "symbols": ["calculate_invoice_total"]}
        caller = {"path": "source.py", "label": "keep_caller"}
        related_edge = {"caller": caller, "target": {"path": worker.name, "label": "parse_route"}, "edge_type": "CALLS"}
        unrelated_edge = {"caller": {**caller, "label": "omit_caller"},
                          "target": {"path": unrelated["path"], "label": "calculate"}, "edge_type": "CALLS"}
        display = replace(result, payload={**result.payload, "working_set": [unrelated, *result.payload["working_set"]],
                                           "impact": {"callers": [related_edge, unrelated_edge]}})
        rendered = service.render(display, brief=True)
        self.assertIn("Assigned source file", rendered)
        self.assertNotIn("Assigned edit spans", rendered)
        self.assertNotIn("tax_rules.py [1-24]", rendered)
        self.assertIn("keep_caller", rendered)
        self.assertNotIn("omit_caller", rendered)
        self.assertEqual(display.payload["working_set"][0], unrelated)
        self.assertEqual(rendered.count("path = path.strip()"), 58)
        worker.write_text(worker.read_text(encoding="utf-8").replace("strip()", "lower()"), encoding="utf-8")
        self.assertNotIn("task_source_file", service.execute(request, brief=True).payload)

    def test_brief_completes_the_indexed_owner_without_expanding_the_saved_pin(self) -> None:
        """Show a complete routine when the short tail fits and the pin remains current."""
        rows = ["def fold_discounts(items):", "    total = 0",
                *("    total += sum(items)" for _ in range(35)), "    return total"]
        worker = self.root / "discounts.py"
        worker.write_text("\n".join([*rows, "", "UNRELATED = 42", *("# padding" for _ in range(75))]) + "\n",
                          encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        board = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        digest = hashlib.sha256(worker.read_bytes()).hexdigest()
        task = board.put("task", "Update fold_discounts", files=[worker.name],
                         next_action=f"`{worker.name}#L1-L30@{digest}`",
                         agent_id="agent:owner", session_id="session:work")["node"]
        request = QueryContextRequest.from_raw(text="fold_discounts", scopes=["code"], task_id=task["id"])
        service = self.graph.query_context_service
        result = service.execute(request, brief=True)
        pin, = result.payload["task_edit_spans"]["spans"]
        self.assertEqual(pin["line_end"], 30)
        owner = result.payload["task_owner_source"]
        self.assertEqual((owner["line_end"], owner["sha256"], owner["text"]), (38, digest, "\n".join(rows)))
        rendered = service.render(result, brief=True)
        self.assertEqual(rendered.count("def fold_discounts(items):"), 1)
        self.assertIn("    return total", rendered)
        self.assertNotIn("UNRELATED = 42", rendered)
        worker.write_text(worker.read_text(encoding="utf-8").replace("return total", "return total * 2"), encoding="utf-8")
        self.assertNotIn("task_owner_source", service.execute(request, brief=True).payload)

    def test_brief_recovers_local_declarations_linked_to_the_pinned_methods(self) -> None:
        """Attach a resolved type and a named sibling without reopening the source."""
        worker = self.root / "channel.py"
        rows = ["class ChannelState:", "    closed = False", "", "class Channel:",
                "    def begin(self):", "        state_type = ChannelState", "        return state_type()", "",
                "    def stop(self):", "        self.state.closed = True", "",
                "    def unrelated(self):", "        return 77", *("# padding" for _ in range(100))]
        worker.write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        board = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        digest = hashlib.sha256(worker.read_bytes()).hexdigest()
        task = board.put("task", "Add a Channel query and preserve stop", files=[worker.name],
                         next_action=f"`{worker.name}#L4-L8@{digest}`",
                         agent_id="agent:owner", session_id="session:work")["node"]
        request = QueryContextRequest.from_raw(text="Channel begin", scopes=["code"], task_id=task["id"])
        service = self.graph.query_context_service
        result = service.execute(request, brief=True)
        local = result.payload["task_local_source"]
        self.assertEqual([(row["line_start"], row["line_end"], row["sha256"]) for row in local],
                         [(1, 2, digest), (9, 10, digest)])
        rendered = service.render(result, brief=True)
        self.assertIn("Related source in assigned files", rendered)
        self.assertEqual(rendered.count("def stop(self):"), 1)
        self.assertNotIn("return 77", rendered)
        self.assertNotIn("task_local_source", service.execute(request).payload)

    def test_brief_includes_only_an_unambiguous_passage_in_assigned_documentation(self) -> None:
        """Return complete current prose and its example for a referenced API in a named file."""
        guide = self.root / "cli.md"
        paragraph = ("Use the index with `tool calculate_invoice_total`:\n\n"
                     "```sh\ntool calculate_invoice_total 42\n```\n\nThe input is a price.\n")
        guide.write_text("# Commands\n\n" + paragraph + "\n## Next\n\nAn unrelated command.\n", encoding="utf-8")
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        board = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        task = board.put("task", "Update tax_rules.calculate_invoice_total and document the result.",
                         agent_id="agent:owner", session_id="session:work", files=[self.path.name, guide.name])["node"]
        request = QueryContextRequest.from_raw(text="calculate_invoice_total", scopes=["code"], task_id=task["id"])
        service = self.graph.query_context_service
        result = service.execute(request, brief=True)
        row = result.payload["task_documentation_source"]
        self.assertEqual((row["path"], row["text"]), (guide.name, paragraph.rstrip()))
        self.assertEqual(row["sha256"], hashlib.sha256(guide.read_bytes()).hexdigest())
        self.assertEqual(service.render(result, brief=True).count("The input is a price."), 1)
        self.assertNotIn("task_documentation_source", service.execute(request).payload)
        guide.write_text("# Commands\n\n" + paragraph + "\nAlternate `calculate_invoice_total`.\n", encoding="utf-8")
        self.assertNotIn("task_documentation_source", service.execute(request, brief=True).payload)
        self.assertFalse(self.graph.compile_project(self.root).run.errors)
        self.assertNotIn("task_documentation_source", service.execute(request, brief=True).payload)

    def test_brief_attaches_scoped_work_and_omits_incidental_checkpoint_history(self) -> None:
        """Keep source-relevant decisions while leaving generic history available."""
        work = CoordinationStore(self.db.with_name("agent-dashboard.reql"))
        decision = work.put("decision", "Keep invoice rounding",
                            rationale="Callers depend on the result",
                            files=["tax_rules.py"], agent_id="agent:author", session_id="test")["node"]
        checkpoint = work.put("checkpoint", "Ran the invoice packaging check",
                              files=["packaging.py"], agent_id="agent:author", session_id="test")["node"]
        request = QueryContextRequest.from_raw(text="calculate invoice total", scopes=["code"])
        normal = self.graph.query_context_result(request)
        brief = self.graph.query_context_service.execute(request, brief=True)
        self.assertIn(checkpoint["id"], {row["id"] for row in normal.payload["engineering_context"]["records"]})
        self.assertEqual({row["id"] for row in brief.payload["engineering_context"]["records"]}, {decision["id"]})
        self.assertTrue(any(row.get("text") == self.body.rstrip("\n") for row in brief.payload["snippets"]))

    def test_brief_compound_query_can_embed_a_clear_leading_owner(self) -> None:
        """Use the separation between owners when query terms dilute the absolute score."""
        request = QueryContextRequest.from_raw(text="invoice pricing validation rule facts", scopes=["code"])
        original = self.graph.query_context_service.execute(request, brief=True)
        owners = original.payload["owner_candidates"]
        self.assertTrue(owners)
        other = dict(owners[0], id="test:linked-helper", score=owners[0]["score"] - 0.03,
                     reason="linked owner symbol")
        with patch.object(self.graph.retrieval, "_code_owner_candidates", return_value=[owners[0], other, *owners[1:]]):
            brief = self.graph.query_context_service.execute(request, brief=True)
        self.assertTrue(any(item.get("text") == self.body.rstrip("\n") for item in brief.payload["snippets"]))
        self.assertIn("Current source", self.graph.query_context_service.render(brief, brief=True))

    def test_brief_keeps_locations_for_ambiguous_long_and_stale_symbols(self) -> None:
        """Show useful locations without embedding an ambiguous or partial body."""
        request = QueryContextRequest.from_raw(text=self.node.properties["name"], scopes=["code"])
        conflict = self.root / "alternate.py"
        conflict.write_text(self.body, encoding="utf-8")
        self.graph.compile_project(self.root)
        result = self.graph.query_context_service.execute(request, brief=True)
        self.assertFalse(result.payload["snippets"])
        paths = {row["path"] for row in result.payload["working_set"]}
        self.assertTrue({"alternate.py", "tax_rules.py"} <= paths)

        conflict.unlink()
        long_body = self.body.replace("    return total", "    total += 1\n" * 40 + "    return total")
        self.path.write_text(long_body, encoding="utf-8")
        self.graph.compile_project(self.root)
        result = self.graph.query_context_service.execute(request, brief=True)
        self.assertFalse(result.payload["snippets"])
        self.assertIn("tax_rules.py", self.graph.query_context_service.render(result, brief=True))

        self.path.write_text(self.body, encoding="utf-8")
        self.graph.compile_project(self.root)
        self.path.write_text("# changed\n" + self.body, encoding="utf-8")
        result = self.graph.query_context_service.execute(request, brief=True)
        text = self.graph.query_context_service.render(result, brief=True)
        self.assertEqual(result.freshness.status, "stale")
        self.assertIn("Graph freshness: stale", text)
        self.assertNotIn("return total", text)

    def test_changed_deleted_and_refreshed_source(self) -> None:
        self.path.write_text(self.body.replace("sum(prices)", "sum(prices) * 2"), encoding="utf-8")
        stale = self._payload(include_source=True)
        self.assertEqual(stale["freshness"]["status"], "stale")
        self.assertTrue(stale["payload"]["snippets"])
        self.assertTrue(all(item["source_status"] == "stale" and "text" not in item
                            for item in stale["payload"]["snippets"]))
        compiled = self.graph.compile_project(self.root)
        self.assertFalse(compiled.run.errors)
        refreshed = self._payload(include_source=True)
        self.assertTrue(any("sum(prices) * 2" in item.get("text", "") for item in refreshed["payload"]["snippets"]))
        self.path.unlink()
        rows = self._payload(include_source=True)["payload"]["snippets"]
        self.assertTrue(all(item["source_status"] == "stale" and "text" not in item for item in rows))

    def test_partial_failed_run_then_reverted_scan_cannot_validate_wrong_spans(self) -> None:
        other = self.root / "other.py"
        other.write_text("def another_rule():\n    return 1\n", encoding="utf-8")
        self.graph.compile_project(self.root)
        other.write_text("def another_rule():\n    return 2\n", encoding="utf-8")
        self.path.write_text("\n\n" + self.body, encoding="utf-8")
        compile_artifact = ArtifactCompiler.compile_artifact

        def partial_compile(compiler, store, artifact):
            if artifact.relative_path == "other.py":
                raise RuntimeError("other parser failed")
            return compile_artifact(compiler, store, artifact)

        with patch.object(ArtifactCompiler, "compile_artifact", partial_compile):
            failed = self.graph.compile_project(self.root)
        self.assertTrue(failed.run.errors)
        self.path.write_text(self.body, encoding="utf-8")
        with patch.object(ArtifactCompiler, "compile_artifact", side_effect=RuntimeError("parser failed")):
            failed = self.graph.compile_project(self.root)
        self.assertTrue(failed.run.errors)
        rows = self._payload(include_source=True)["payload"]["snippets"]
        self.assertTrue(rows)
        self.assertTrue(all(item["source_status"] != "verified" and "text" not in item for item in rows))

    def test_bom_crlf_and_non_utf8_source_are_handled_explicitly(self) -> None:
        self.path.write_bytes(b"\xef\xbb\xbf" + self.body.replace("\n", "\r\n").encode("utf-8"))
        self.graph.compile_project(self.root)
        rows = self._payload(include_source=True)["payload"]["snippets"]
        self.assertTrue(any(item.get("text") == self.body.rstrip("\n") for item in rows))
        self.path.write_bytes(self.body.encode("utf-8") + b"# non-UTF8 comment: \xff\n")
        self.graph.compile_project(self.root)
        rows = self._payload(include_source=True)["payload"]["snippets"]
        self.assertTrue(rows)
        self.assertTrue(all(item["source_status"] == "unavailable" and "text" not in item for item in rows))

    def test_byte_limit_and_read_errors_never_return_graph_text(self) -> None:
        with patch("memory.services.retrieval.context.source.MAX_SOURCE_BYTES", 32):
            rows = capture_source_excerpts(self.graph.store, [self._read()])
        self.assertEqual(rows[0]["source_status"], "unavailable")
        self.assertNotIn("text", rows[0])
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            rows = capture_source_excerpts(self.graph.store, [self._read()])
        self.assertEqual(rows[0]["source_status"], "unavailable")
        self.assertIn("PermissionError", rows[0]["reason"])
        self.assertNotIn("text", rows[0])

    def test_file_region_and_total_budgets_apply_across_the_entire_response(self) -> None:
        for index in range(3):
            (self.root / f"zone_{index}.py").write_text(
                f"def zone_{index}():\n    return {index}\n", encoding="utf-8")
        self.graph.compile_project(self.root)
        other_nodes = [node for node in self.graph.store.all_nodes()
                       if node.type == "Function" and node.id != self.node.id]
        reads = [self._read(line, line) for line in range(1, 24, 2)]
        regions = capture_source_excerpts(self.graph.store, reads)
        self.assertEqual(len(regions), 6)
        reads = [self._read()] + [dict(path=node.properties["relative_path"], line_start=1,
                                      line_end=2, node_id=node.id) for node in other_nodes]
        rows = capture_source_excerpts(self.graph.store, reads)
        self.assertEqual(len({row["artifact_id"] for row in rows}), 3)
        self.assertLessEqual(sum(len(row.get("text", "").splitlines()) for row in rows), 120)
        self.assertLessEqual(sum(len(row.get("text", "")) for row in rows), 6000)

    def test_outside_paths_and_resolved_links_are_rejected_before_open(self) -> None:
        artifact = self.graph.store.get_node(self.artifact_id)
        revision = RevisionRepository(self.graph.store).latest(self.project_id)
        for unsafe in ("../outside.py", "C:/outside.py", "/outside.py", "//server/share/outside.py"):
            with self.subTest(path=unsafe):
                props = {**artifact.properties, "relative_path": unsafe}
                self.graph.store.update_node_fields(artifact.id, properties=props)
                revision.manifest[unsafe] = revision.manifest["tax_rules.py"]
                with patch.object(RevisionRepository, "latest", return_value=revision), \
                        patch.object(Path, "open", side_effect=AssertionError("outside file was opened")):
                    rows = capture_source_excerpts(self.graph.store, [self._read()])
                self.assertEqual(rows[0]["source_status"], "unavailable")
                self.assertIn("outside", rows[0]["reason"])
        self.graph.store.update_node_fields(artifact.id, properties=artifact.properties)
        original_resolve = Path.resolve

        def linked_resolve(path, *args, **kwargs):
            return self.root.parent / "outside.py" if path == self.path else original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", linked_resolve), \
                patch.object(Path, "open", side_effect=AssertionError("resolved link was opened")):
            rows = capture_source_excerpts(self.graph.store, [self._read()])
        self.assertEqual(rows[0]["source_status"], "unavailable")

    def test_python_cli_and_mcp_provide_the_same_verified_source(self) -> None:
        expected = self._payload(include_source=True)["payload"]["snippets"]
        self.graph.close()
        mcp = mcp_query_context(storage_path=str(self.db), query="calculate_invoice_total", code=True, include_source=True)
        output = io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            with contextlib.redirect_stdout(output):
                code = main(["query_context", "--query", "calculate_invoice_total", "--code", "--source", "--json"])
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, output.getvalue())
        cli = json.loads(output.getvalue())
        self.assertEqual(mcp["payload"]["snippets"], expected)
        self.assertEqual(cli["payload"]["snippets"], expected)

    def test_invalid_source_requests_are_rejected_at_shared_boundaries(self) -> None:
        for scopes in (None, ["docs"], ["code", "docs"]):
            with self.subTest(scopes=scopes), self.assertRaises(ValueError):
                QueryContextRequest.from_raw(text="invoice", scopes=scopes, include_source=True)
        with self.assertRaises(TypeError):
            QueryContextRequest.from_raw(text="invoice", scopes=["code"], include_source="yes")
        with self.assertRaises(MCPToolError):
            mcp_query_context(storage_path=str(self.db), query="invoice", include_source=True)
        schema = next(item for item in list_tools() if item["name"] == "query_context")
        self.assertEqual(schema["inputSchema"]["properties"]["include_source"]["default"], False)


if __name__ == "__main__":
    unittest.main()
