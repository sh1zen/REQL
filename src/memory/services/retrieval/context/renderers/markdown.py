"""Markdown rendering for query, exploration, and context payloads."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import PurePosixPath
import re
from typing import Any, Sequence

from .....artifacts.context_scope import is_documentation_context, is_test_context_path
from .....domain.models import MemoryEdge, MemoryNode
from ...common import SOURCE_NODE_TYPES
from ..source import uncovered_source_rows


class MarkdownContextRendererMixin:
    def _render_query_explore_payload(self, payload: dict[str, Any]) -> str:
        lines = ["# REQL Query Explore", "", f"Query: {payload.get('query', '')}", ""]
        seed_nodes = list(payload.get("seed_nodes") or [])
        if seed_nodes:
            lines.append("## Seed Nodes")
            for node in seed_nodes[:8]:
                lines.append(f"- `{node['id']}` [{node['type']}] {node.get('label') or node.get('text') or ''} {node.get('location') or ''}".rstrip())
            lines.append("")
        sections = payload.get("sections") or {}
        titles = {
            "owners": "Owners",
            "callers": "Callers",
            "public_surface": "Public Surface",
            "serialization_paths": "Serialization Paths",
            "docs_mentions": "Docs Mentions",
            "structural_duplicates": "Structural Duplicates",
            "code": "Code",
        }
        for key, title in titles.items():
            if key not in sections:
                continue
            value = sections[key]
            lines.append(f"## {title}")
            if key == "code":
                for row in value.get("working_set", []):
                    span = self._format_line_span(row.get("line_start"), row.get("line_end"))
                    lines.append(f"- working_set `{row['path']}` [{row['role']}] score={float(row['score']):.2f}{span}")
                for row in value.get("targeted_reads", []):
                    location = self._format_path_span(row.get("path"), row.get("line_start"), row.get("line_end"))
                    lines.append(f"- read `{location}` {row.get('reason')}: {row.get('label')}")
            elif value:
                lines.extend(self._render_query_explore_row(row) for row in value)
            else:
                lines.append("- No matches in this view.")
            lines.append("")
        lines.extend(self._render_counts(payload))
        return "\n".join(lines).strip()

    def _render_query_explore_row(self, row: dict[str, Any]) -> str:
        if isinstance(row.get("source"), dict) and isinstance(row.get("duplicate"), dict):
            source = row["source"]
            duplicate = row["duplicate"]
            shared = ", ".join(row.get("shared_patterns") or [])
            shared_suffix = f"; shared={shared}" if shared else ""
            return (
                f"- `{source.get('location') or source.get('id')}` <-> "
                f"`{duplicate.get('location') or duplicate.get('id')}`; "
                f"similarity={float(row.get('similarity', 0.0)):.2f}; {row.get('reason')}{shared_suffix}"
            )
        node = row.get("owner") or row.get("caller") or row.get("surface") or row.get("node") or row.get("mention") or row.get("target")
        if not isinstance(node, dict):
            return f"- {row.get('reason', 'match')}"
        location = f" @ {node['location']}" if node.get("location") else ""
        edge = row.get("edge")
        relation = f"; edge={edge.get('type')}:{edge.get('id')}" if isinstance(edge, dict) else ""
        return f"- `{node['id']}` [{node['type']}] {node.get('label') or node.get('text') or ''}{location}; {row.get('reason', 'match')}{relation}"

    def _query_explore_node_payload(self, node: MemoryNode, ranked_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "id": node.id,
            "type": node.type,
            "label": self._compact_text(self._node_label(node), max_chars=140),
            "text": self._compact_text(node.text or "", max_chars=260),
            "location": self._location_summary(node),
            "properties": self._query_explore_properties(node),
        }
        if ranked_payload:
            payload["score"] = ranked_payload.get("score")
            payload["reasons"] = ranked_payload.get("reasons")
        return payload

    def _query_explore_edge_payload(self, edge: MemoryEdge, nodes: OrderedDict[str, MemoryNode]) -> dict[str, Any]:
        payload = self._edge_context_payload(edge, nodes)
        payload["location"] = self._location_summary(edge)
        return payload

    @staticmethod
    def _query_explore_properties(node: MemoryNode) -> dict[str, Any]:
        keys = {
            "alias",
            "artifact_id",
            "kind",
            "line_end",
            "line_start",
            "module",
            "name",
            "path",
            "qualified_name",
            "relative_path",
            "source_file",
            "source_path",
            "symbol_name",
            "target",
            "is_re_export",
            "unresolved_call_count",
        }
        return {key: value for key, value in node.properties.items() if key in keys}

    def _is_public_surface_node(self, node: MemoryNode) -> bool:
        if node.type not in self.policy.explore.public_surface_nodes:
            return False
        if node.type == "Import":
            return bool(node.properties.get("is_re_export"))
        if node.type in {"Function", "Method"}:
            name = str(node.properties.get("name") or node.label or "")
            return bool(name and not name.startswith("_"))
        return True

    def _is_docs_mention_node(self, node: MemoryNode) -> bool:
        if node.type not in self.policy.explore.docs_nodes:
            return False
        return is_documentation_context(node.type, node.properties, self._node_relative_path(node) or "")


    def _render_query_context_payload(
        self, payload: dict[str, Any], *, brief: bool = False, displayed_source: list[dict[str, Any]] | None = None,
    ) -> str:
        if payload.get("kind") == "code":
            return self._render_code_context_payload(payload, brief=brief, displayed_source=displayed_source)
        return self._render_general_context_payload(payload)

    def _render_code_context_payload(
        self, payload: dict[str, Any], *, brief: bool = False, displayed_source: list[dict[str, Any]] | None = None,
    ) -> str:
        if str(payload.get("query_mode") or "informative") == "cleanup":
            return self._render_cleanup_context_payload(payload)

        lines = self._render_context_header(payload, title="# REQL Context")
        emitted: list[dict[str, Any]] = []
        located: set[tuple[str, int, int]] = set()

        def source_lines(rows: Sequence[dict[str, Any]]) -> list[str]:
            """Use only complete, displayed bodies to cover later evidence."""
            visible = []
            for row in rows:
                if not uncovered_source_rows([row], emitted):
                    continue
                location = (row["path"], row["line_start"], row["line_end"])
                if location in located and not row.get("text"):
                    continue
                visible.append(row)
                located.add(location)
                if row.get("source_status") == "verified" and "text" in row and not row.get("truncated"):
                    emitted.append(row)
                    if displayed_source is not None:
                        displayed_source.append(row)
            return self._render_snippet_lines(visible, brief=True)

        exact_ranges = payload.get("task_focus_ranges", ()) if brief else ()
        exact_paths = payload.get("exact_paths", ()) if brief else ()
        task_paths = set(payload.get("task_source_paths", ())) if brief else set()
        outlines: Sequence[dict[str, Any]] = ()
        if brief:
            outline = payload.get("task_documentation_outline")
            outlines = (outline,) if outline else payload.get("task_documentation_outlines", ())
        if self._query_context_confidence_is_insufficient(payload) and not (exact_ranges or exact_paths):
            self._append_section(lines, "Assigned document sections", self._document_outline_lines(outlines))
            return "\n".join(lines).strip()
        working_set = list(payload.get("working_set") or [])
        owner_candidates = list(payload.get("owner_candidates") or [])
        targeted_reads = list(payload.get("targeted_reads") or [])
        read_plan = list(payload.get("read_plan") or [])
        if exact_paths:
            self._append_section(lines, "Requested files", [f"- `{path}`" for path in exact_paths])
        edit_spans = payload.get("task_edit_spans") if brief else None
        assigned_test = payload.get("task_test_source") if brief else None
        assigned_file = payload.get("task_source_file") if brief else None
        assigned_owner = payload.get("task_owner_source") if brief else None
        documentation = payload.get("task_documentation_source") if brief else None
        complete_assignments = [row for row in (assigned_file, assigned_test, assigned_owner) if row]
        assigned_sources = [*(edit_spans["spans"] if edit_spans else []), *complete_assignments,
                            *([documentation] if documentation else [])]
        assigned_tests = {item["path"] for item in assigned_sources
                          if item.get("source_status") == "verified" and is_test_context_path(item["path"])}
        tests: list[dict[str, Any]] = []
        seen_test_paths: set[str] = set()
        for item in list(payload.get("test_targets") or []):
            path = str(item.get("path") or "")
            if item.get("kind") != "test" or not path or path in seen_test_paths:
                continue
            if (assigned_tests or (exact_ranges and path not in task_paths)) and item.get("evidence_kind") not in {"static", "observed"}:
                continue
            seen_test_paths.add(path)
            tests.append(item)

        snippets = payload.get("snippets") or []
        complete = [row for row in (*snippets, *assigned_sources)
                    if row.get("source_status") == "verified" and "text" in row and not row.get("truncated")]
        verified_paths = {row["path"] for row in complete}
        task_covered = bool(task_paths) and task_paths <= verified_paths
        if assigned_file:
            self._append_section(lines, "Assigned source file", source_lines([assigned_file]))
        if assigned_test:
            self._append_section(lines, "Assigned test file", source_lines([assigned_test]))
        if edit_spans:
            span_rows = list(edit_spans["spans"])
            if assigned_owner:
                span_rows.insert(0, assigned_owner)
            span_lines = source_lines(span_rows)
            self._append_section(lines, "Assigned edit spans", span_lines)
        if documentation:
            self._append_section(lines, "Assigned documentation", source_lines([documentation]))
        self._append_section(lines, "Assigned document sections", self._document_outline_lines(outlines))
        if brief and payload.get("source_mode") == "verified":
            self._append_section(lines, "Current source", source_lines(snippets))
            if linked_section := payload.get("linked_section"):
                span = self._format_path_bracket_span(
                    linked_section["path"], linked_section["line_start"], linked_section["line_end"],
                )
                section_lines = source_lines([linked_section]) if "text" in linked_section else [f"- `{span}` (verified section)"]
                self._append_section(lines, "Linked guidance", section_lines)
        if exact_ranges:
            missing = [
                row for row in exact_ranges
                if (row["path"], row["line_start"], row["line_end"]) not in located and not any(
                    seen["path"] == row["path"] and seen["line_start"] <= row["line_start"]
                    and row["line_end"] <= seen["line_end"]
                    for seen in (*emitted, *[s for s in snippets if s.get("source_status") == "stale"])
                )
            ]
            locations = [
                f"- `{self._format_path_bracket_span(row['path'], row['line_start'], row['line_end'])}`"
                f"; {row['label']} (indexed range)"
                for row in missing
            ]
            self._append_section(lines, "Assigned locations", locations)
        if brief and (local_source := payload.get("task_local_source")):
            self._append_section(lines, "Related source in assigned files", source_lines(local_source))
        if brief and (contracts := payload.get("task_contract_source")):
            self._append_section(lines, "Referenced declarations", source_lines(contracts))
        if brief:
            complete = emitted
            verified_paths = {row["path"] for row in emitted}
            task_covered = bool(task_paths) and task_paths <= verified_paths
        source_rows = working_set
        if brief and complete:
            focus = {row.get("path") for row in complete} | {row.get("path") for row in owner_candidates}
            source_rows = [
                row for index, row in enumerate(source_rows)
                if row.get("path") in focus or (index < 3 and row.get("reason") != "graph match")
            ]
        if task_covered or exact_ranges:
            focused_paths = {row["path"] for row in exact_ranges}
            source_rows = [row for row in source_rows if row.get("path") in task_paths
                           and row.get("path") not in focused_paths]
        rendered_paths: set[str] = set()

        def best_span(path: str, row: dict[str, Any]) -> tuple[Any, Any]:
            if row.get("line_start") is not None or row.get("line_end") is not None:
                return row.get("line_start"), row.get("line_end")
            for item in targeted_reads:
                if item.get("path") == path and (item.get("line_start") is not None or item.get("line_end") is not None):
                    return item.get("line_start"), item.get("line_end")
            for item in owner_candidates:
                if item.get("path") == path and (item.get("line_start") is not None or item.get("line_end") is not None):
                    return item.get("line_start"), item.get("line_end")
            return None, None

        data_trace_lines: list[str] = []
        for item in list(payload.get("data_trace") or []):
            location = self._format_path_bracket_span(item.get("path"), item.get("line_start"), item.get("line_end"))
            relation = f"; via={item.get('relation')}" if item.get("relation") else ""
            data_trace_lines.append(f"- {item.get('stage')}: `{location}`; {item.get('label')}{relation}")
        if data_trace_lines:
            self._append_section(lines, "End-to-end data trace", data_trace_lines)

        file_lines: list[str] = []
        if not brief:
            verified_paths = {row.get("path") for row in snippets if row.get("source_status") == "verified"}
        lines = [line for line in lines if not line.startswith(("Query:", "Mode:", "Scope:"))]
        for row in source_rows:
            path = str(row.get("path") or "")
            if not path:
                continue
            line_start, line_end = best_span(path, row)
            location = self._format_path_bracket_span(path, line_start, line_end)
            symbols: list[str] = []
            for symbol in list(row.get("symbols") or []):
                if symbol and symbol not in symbols:
                    symbols.append(str(symbol))
            for item in owner_candidates:
                symbol = item.get("name") or item.get("label")
                if item.get("path") == path and symbol and symbol not in symbols:
                    symbols.append(str(symbol))
            owner_note = f"; owners={', '.join(self._source_symbol_name(symbol, path) for symbol in symbols[:4])}" if symbols else "; owners=none identified"
            covered = any(row.get("path") == path and line_start is not None and line_end is not None
                          and row["line_start"] <= line_start and row["line_end"] >= line_end for row in complete)
            if brief and not covered:
                path_source = [item for item in complete if item.get("path") == path]
                # A coarse file location adds nothing once its selected owner spans are shown.
                covered = bool(path_source) and all(
                    read.get("snippet_embedded") for read in read_plan
                    if read.get("path") == path and read.get("reason") == "owner symbol"
                )
            if (brief and (covered or (path, line_start, line_end) in located)) or (not brief and path in verified_paths):
                continue
            rendered_paths.add(path)
            file_lines.append(f"- `{location}`{owner_note if symbols or not brief else ''}")
        if not file_lines and not verified_paths and not exact_ranges and not exact_paths and not located:
            file_lines.append("- No source files matched this query.")
        self._append_section(lines, "Files", file_lines)

        # Reuse the projection's bounded evidence instead of requiring another
        # retrieval just to discover the base implementation or affected callers.
        impact = payload.get("impact") or {}
        boundary_items = list(impact.get("implementation_links") or []) + list(impact.get("callers") or [])
        boundary_items.extend(item for item in impact.get("public_surface", []) if item.get("edge_id"))
        if brief:
            # Keep one row per semantic edge even when it is also a public entry point.
            unique: dict[str, dict[str, Any]] = {}
            exact_ids = {row["node_id"] for row in exact_ranges}
            for item in boundary_items:
                if exact_ids and not any(
                    (item.get(side) or {}).get("id") in exact_ids
                    for side in ("source", "caller", "target", "surface")
                ):
                    continue
                if task_covered:
                    source = (item.get("source") or item.get("caller") or {}).get("path")
                    target = (item.get("target") or {}).get("path")
                    if source and target and source not in task_paths and target not in task_paths:
                        continue
                unique.setdefault(item.get("edge_id") or self._render_change_chain_item(item), item)
            boundary_items = list(unique.values())
        shown_boundaries = boundary_items[:6]
        boundary_lines = [
            f"- {line}" for line in (
                self._brief_code_boundaries(shown_boundaries) if brief else
                [self._render_change_chain_item(item) for item in shown_boundaries]
            )
        ]
        if len(boundary_items) > 6:
            boundary_lines.append(f"- {len(boundary_items) - 6} more relation(s) in structured impact.")
        boundary_lines.extend(f"- {note}" for note in list(impact.get("notes") or [])[:1])
        if boundary_lines:
            self._append_section(lines, "Change boundaries", boundary_lines)

        test_lines: list[str] = []
        for item in tests:
            path = str(item.get("path") or "")
            if path in rendered_paths and path not in verified_paths and (not brief or not item.get("evidence_kind")):
                continue
            location = self._format_path_bracket_span(path, item.get("line_start"), item.get("line_end"))
            symbols = [str(symbol) for symbol in list(item.get("symbols") or []) if symbol]
            owner_note = f"; owners={', '.join(self._source_symbol_name(symbol, path) for symbol in symbols[:3])}" if symbols else ""
            evidence = item.get("evidence_kind")
            validity = "current" if item.get("verified_current") else "unverified"
            suffix = f" [{evidence}, {validity}]" if evidence else ""
            test_lines.append(f"- `{location}`{suffix}{owner_note}")
        if tests and not test_lines and not brief:
            test_lines.append("- Tests included in Files above.")
        self._append_section(lines, "Associated tests", test_lines)
        if payload.get("source_mode") == "verified":
            if not brief:
                self._append_section(lines, "Current source", self._render_snippet_lines(snippets))
        else:
            self._append_section(lines, "Source previews", self._render_snippet_lines(snippets))
        return "\n".join(lines).strip()

    def _document_outline_lines(self, outlines: Sequence[dict[str, Any]]) -> list[str]:
        """Show assigned document paths with verified section ranges."""
        lines = []
        for outline in outlines:
            if lines:
                lines.append("")
            lines.append(f"`{outline['path']}` (verified). Read the relevant section by line range.")
            lines.extend(f"- {row['line_start']}-{row['line_end']} {row['title']}" for row in outline["headings"])
            if outline["omitted"]:
                lines.append(f"- {outline['omitted']} more sections.")
        return lines

    def _render_general_context_payload(self, payload: dict[str, Any]) -> str:
        if str(payload.get("query_mode") or "informative") == "cleanup":
            return self._render_cleanup_context_payload(payload)

        lines = self._render_context_header(payload, title="# REQL Context")
        if self._query_context_confidence_is_insufficient(payload):
            return "\n".join(lines).strip()
        results = list(payload["results"])
        result_lines: list[str] = []
        for item in results:
            result_lines.extend(self._render_general_result_lines(item))
        if not results:
            result_lines.append("- No ranked nodes matched this query.")
        self._append_section(lines, "Results", result_lines)
        return "\n".join(lines).strip()

    def _render_cleanup_context_payload(self, payload: dict[str, Any]) -> str:
        lines = self._render_context_header(payload, title="# REQL Cleanup Context")
        if self._query_context_confidence_is_insufficient(payload):
            return "\n".join(lines).strip()
        cleanup_filter = payload.get("cleanup_filter") or {}
        if cleanup_filter:
            mode = cleanup_filter.get("mode") or "safe_remove"
            excluded = cleanup_filter.get("excluded_risky_candidates", 0)
            self._append_section(lines, "Cleanup filter", [f"- mode={mode}; shown={cleanup_filter.get('shown_candidates', 0)}; excluded_risky={excluded}; validate/risky findings excluded"])
        cleanup = list(payload.get("cleanup_candidates") or [])
        result_lines: list[str] = []
        if not cleanup:
            result_lines.append("- No cleanup candidates matched this query.")
        for item in cleanup:
            location = f" @ {item['location']}" if item.get("location") else ""
            name = item.get("symbol_name") or item.get("label") or item.get("id")
            finding_type = f"; finding={item.get('finding_type')}" if item.get("finding_type") else ""
            priority = f"; priority={item.get('cleanup_priority')}" if item.get("cleanup_priority") else ""
            safety = f"; safety={item.get('removal_safety')}" if item.get("removal_safety") else ""
            reason = f"; reason={item.get('removal_reason')}" if item.get("removal_reason") else ""
            validation = f"; validate={item.get('validation_reason')}" if item.get("validation_reason") else ""
            result_lines.append(f"- cleanup `{item['id']}` {name}{location}{finding_type}{priority}{safety}{reason}{validation}")
        self._append_section(lines, "Cleanup candidates", result_lines)
        read_lines: list[str] = []
        for item in list(payload.get("targeted_reads") or [])[:12]:
            location = self._format_path_bracket_span(item.get("path"), item.get("line_start"), item.get("line_end"))
            kind = item.get("read_kind") or "read"
            sufficiency = item.get("sufficiency") or {}
            status = sufficiency.get("status")
            status_text = f"; {status}: {sufficiency.get('reason')}" if status else ""
            read_lines.append(f"- {kind} `{location}` from `{item.get('node_id')}` [{item.get('type')}] {item.get('reason')}{status_text}")
        self._append_section(lines, "Targeted reads", read_lines)
        self._append_section(lines, "Change chain", self._render_change_chain_lines(payload.get("change_chain") or []))
        self._append_section(lines, "Snippets", self._render_snippet_lines(payload.get("snippets") or []))
        self._append_section(lines, "Research queries", self._render_research_refs(payload))
        self._append_section(lines, "Summary", self._render_compact_counts(payload))
        return "\n".join(lines).strip()

    def _render_change_chain_lines(self, change_chain: Sequence[dict[str, Any]], *, limit: int = 5) -> list[str]:
        lines: list[str] = []
        for step in list(change_chain)[:limit]:
            phase = step.get("phase") or "step"
            description = step.get("description") or ""
            lines.append(f"- {phase}: {description}")
            for item in list(step.get("items") or [])[:3]:
                lines.append(f"  - {self._render_change_chain_item(item)}")
        return lines

    def _brief_code_boundaries(self, items: Sequence[dict[str, Any]]) -> list[str]:
        """Share the path and target for located calls within the same file."""
        lines: list[str] = []
        groups: dict[tuple[str, str, str], tuple[int, list[str]]] = {}
        for item in items:
            caller, target = item.get("caller") or {}, item.get("target") or {}
            path = caller.get("path")
            caller_location, target_location = caller.get("location") or "", target.get("location") or ""
            prefix = f"{path}:"
            if (item.get("edge_type") != "CALLS" or not path or target.get("path") != path
                    or not caller_location.startswith(prefix) or not target_location.startswith(prefix)):
                lines.append(self._render_change_chain_item(item))
                continue
            caller_name = self._source_symbol_name(caller.get("label") or caller.get("id"), path)
            target_name = self._source_symbol_name(target.get("label") or target.get("id"), path)
            destination = f"`{target_name}` [{target_location.removeprefix(prefix)}]"
            key = (path, target.get("id") or target_name, destination)
            if key not in groups:
                groups[key] = (len(lines), [])
                lines.append("")
            _, sources = groups[key]
            sources.append(f"`{caller_name}` [{caller_location.removeprefix(prefix)}]")
        for (path, _, destination), (index, sources) in groups.items():
            lines[index] = f"CALLS: `{path}` {', '.join(sources)} -> {destination}"
        return lines

    def _render_change_chain_item(self, item: dict[str, Any]) -> str:
        """Render existing source and relationship evidence without opaque duplicate ids."""
        if item.get("source_span"):
            return f"`{item.get('source_span')}` via `{item.get('node_id')}`; {item.get('reason') or 'graph match'}"
        if item.get("command"):
            return f"`{item.get('command')}`; {item.get('reason') or item.get('kind') or 'verify'}"
        if item.get("source") and item.get("target"):
            source, target = item["source"], item["target"]
            return (f"{item.get('edge_type')}: `{self._source_symbol_name(source.get('label') or source.get('id'), source.get('path'))}` @ {source.get('location') or source.get('path') or 'unknown'}"
                    f" -> `{self._source_symbol_name(target.get('label') or target.get('id'), target.get('path'))}` @ {target.get('location') or target.get('path') or 'unknown'}")
        if item.get("surface"):
            surface = item.get("surface") or {}
            return f"{item.get('edge_type') or item.get('impact_kind', 'impact')}: `{self._source_symbol_name(surface.get('label') or surface.get('id'), surface.get('path'))}` @ {surface.get('location') or surface.get('path') or 'unknown'}"
        if item.get("caller"):
            caller = item.get("caller") or {}
            target = item.get("target") or {}
            return (f"{item.get('edge_type') or 'caller'}: `{self._source_symbol_name(caller.get('label') or caller.get('id'), caller.get('path'))}` @ {caller.get('location') or caller.get('path') or 'unknown'}"
                    f" -> `{target.get('label') or target.get('id')}`")
        if item.get("impact_kind") == "docs":
            return f"docs `{item.get('location') or item.get('path')}`; {item.get('reason') or 'documentation mention'}"
        if item.get("impact_kind") == "note":
            return str(item.get("reason") or "")
        if item.get("location") or item.get("path"):
            name = item.get("name") or item.get("label") or item.get("id") or item.get("node_id")
            location = item.get("location") or self._format_path_bracket_span(item.get("path"), item.get("line_start"), item.get("line_end"))
            related = item.get("related") or []
            related_text = f"; related={', '.join(str(value) for value in related[:2])}" if related else ""
            return f"`{item.get('id') or item.get('node_id')}` {name} @ {location}{related_text}"
        return self._compact_text(str(item), max_chars=220)

    @staticmethod
    def _source_symbol_name(name: object, path: str | None) -> str:
        """Omit only the exact file module prefix when its source path is displayed."""
        name = str(name)
        if not path:
            return name
        relative = PurePosixPath(path.replace("\\", "/"))
        if not relative.name:
            return name
        module = relative.with_suffix("").as_posix().replace("/", ".")
        if name == module:
            return "(module)"
        return name.removeprefix(module + ".")

    def _render_snippet_lines(self, snippets: Sequence[dict[str, Any]], *, brief: bool = False) -> list[str]:
        snippet_lines: list[str] = []
        for item in snippets:
            location = self._format_path_bracket_span(item.get("path"), item.get("line_start"), item.get("line_end"))
            if "source_status" in item:
                status = item["source_status"]
                suffix = "" if status == "verified" else f" ({status})"
                if brief and status == "verified":
                    if item.get("body_omitted"):
                        snippet_lines.append(f"- `{location}` (verified range)")
                        continue
                    if item.get("whole_file"):
                        suffix = " (whole file, verified)"
                    else:
                        suffix = " (verified excerpt)" if item.get("truncated") else " (complete, verified)"
                    if item.get("encoding") and item.get("line_endings"):
                        suffix += f" [{item['encoding']}, line endings: {item['line_endings']}]"
                snippet_lines.append(f"- `{location}`{suffix}")
                if status == "verified":
                    text = str(item.get("text") or "")
                    if brief:
                        # Keep indentation copyable, even when a document embeds its own fences.
                        longest = max((len(match[1]) for match in re.finditer(r"(?m)^ {0,3}(`{3,})[ \t]*$", text)),
                                      default=2)
                        fence = "`" * (longest + 1)
                        snippet_lines.extend(("", fence, *text.split("\n"), fence, ""))
                    else:
                        snippet_lines.extend(("", *(f"      {line}" for line in text.splitlines()), ""))
                    if item.get("truncated"):
                        snippet_lines.append(f"  Truncated; read from line {item['next_line']} for the remaining span.")
                else:
                    snippet_lines.append(f"  {item.get('reason')}")
                continue
            origin = "unverified disk preview" if item.get("source") == "disk" else "indexed graph preview"
            snippet_lines.append(f"- `{location}` ({item.get('type')}; {origin})")
            text = str(item.get("text") or "")
            if text:
                snippet_lines.extend(f"  {line}" for line in text.splitlines())
            if item.get("truncated"):
                next_line = f"; next line {item['next_line']}" if item.get("next_line") else ""
                snippet_lines.append(f"  Preview truncated{next_line}.")
        return snippet_lines

    @staticmethod
    def _append_section(lines: list[str], title: str, body: list[str]) -> None:
        if not body:
            return
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"## {title}")
        lines.extend(body)

    def _render_context_header(self, payload: dict[str, Any], *, title: str) -> list[str]:
        lines = [title, f"Query: {payload.get('query', '')}", f"Mode: {payload.get('query_mode', 'informative')}"]
        scopes = list(payload.get("scopes") or [])
        if scopes:
            lines.append(f"Scope: {', '.join(scopes)}")
        confidence = payload.get("confidence") or {}
        if confidence.get("status") == "insufficient":
            max_score = float(confidence.get("max_score", 0.0) or 0.0)
            threshold = float(confidence.get("threshold", self.policy.context.min_confidence))
            detail = f"max score {max_score:.3f} < {threshold:.3f}" if max_score < threshold else "no ranked results"
            lines.append(f"Confidence: insufficient ({detail}); targeted rg fallback allowed.")
        return lines

    @staticmethod
    def _query_context_confidence_is_insufficient(payload: dict[str, Any]) -> bool:
        return (payload.get("confidence") or {}).get("status") == "insufficient"

    def _render_research_refs(self, payload: dict[str, Any]) -> list[str]:
        query = self._reql_string(str(payload.get("query") or ""))
        ids: list[str] = []
        for group in ("results", "owner_candidates", "cleanup_candidates"):
            for item in list(payload.get(group) or []):
                node_id = item.get("id") or item.get("node_id")
                if node_id and node_id not in ids:
                    ids.append(str(node_id))
                if len(ids) >= 3:
                    break
            if len(ids) >= 3:
                break
        lines = [
            f"- research raw rows: `reql query 'RETRIEVE {query} LIMIT 8 RETURN id,type,text,score,source_for,relation,direction,relative_path,line_start,line_end'`",
            f"- research graph: `reql query_graph --query {query} --max-depth 3 --json`",
        ]
        if ids:
            id_list = ", ".join(self._reql_string(node_id) for node_id in ids)
            lines.append(f"- research compare: `reql query 'FIND nodes WHERE id IN [{id_list}] RETURN id,type,label,text,relative_path,line_start,line_end'`")
        return lines

    @staticmethod
    def _render_compact_counts(payload: dict[str, Any]) -> list[str]:
        counts = payload.get("counts") or {}
        rendered = ", ".join(f"{key}={value}" for key, value in counts.items())
        lines = [f"Counts: {rendered}" if rendered else "Counts: none"]
        if payload.get("trace_id"):
            lines.append(f"Trace: {payload['trace_id']}")
        return lines

    def _render_general_result_lines(self, item: dict[str, Any]) -> list[str]:
        label = self._compact_text(str(item.get("label") or item.get("text") or item.get("id") or ""), max_chars=140)
        prefix = "- source" if item.get("kind") == "source" else f"- ({float(item.get('score', 0.0)):.2f})"
        line = f"{prefix} `{item.get('id')}` [{item.get('type')}] {label}"
        if item.get("location"):
            line += f" @ {item['location']}"
        source_locations = [str(value) for value in item.get("source_locations", []) if value]
        if source_locations:
            line += f"; source={', '.join(source_locations[:3])}"
        source_ids = [str(value) for value in item.get("source_ids", []) if value]
        if source_ids:
            rendered_ids = ", ".join(f"`{source_id}`" for source_id in source_ids[:3])
            line += f"; source_ids={rendered_ids}"
        lines = [line]
        text = self._compact_text(str(item.get("text") or ""), max_chars=260)
        if text and text != label:
            lines.append(f"  text: {text}")
        return lines

    @staticmethod
    def _render_counts(payload: dict[str, Any]) -> list[str]:
        lines = ["## Counts"]
        counts = payload.get("counts") or {}
        lines.extend(f"- {key}: {value}" for key, value in counts.items())
        if payload.get("trace_id"):
            lines.append(f"- trace_id: {payload['trace_id']}")
        return lines

    @staticmethod
    def _format_line_span(line_start: Any, line_end: Any) -> str:
        if line_start is None and line_end is None:
            return ""
        if line_end is None or line_end == line_start:
            return f" lines={line_start}"
        return f" lines={line_start}-{line_end}"

    def _format_path_span(self, path: Any, line_start: Any, line_end: Any) -> str:
        if not path:
            return ""
        if line_start is None and line_end is None:
            return str(path)
        if line_end is None or line_end == line_start:
            return f"{path}:{line_start}"
        return f"{path}:{line_start}-{line_end}"

    def _format_path_bracket_span(self, path: Any, line_start: Any, line_end: Any) -> str:
        if not path:
            return ""
        if line_start is None and line_end is None:
            return str(path)
        if line_end is None or line_end == line_start:
            return f"{path} [{line_start}]"
        return f"{path} [{line_start}-{line_end}]"
