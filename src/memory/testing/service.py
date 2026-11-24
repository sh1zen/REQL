"""Join resolved AST links, imported observations, and query-time candidates."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import re
from typing import Any, Iterable

from ..artifacts.fingerprint import normalize_relative_lookup_path
from ..artifacts.testing_conventions import candidate_test_names, candidate_test_stems, project_test_paths, test_path_stem
from ..artifacts.scanner import fingerprint_file
from ..artifacts.revision import RevisionRepository
from ..config import StorageConfig, default_config
from ..config.policies import TestSuggestionPolicy
from ..domain.ids import stable_hash
from ..domain.models import MemoryNode
from ..storage.graph_store import GraphStore
from .coverage_import import local_path, observations, read_coverage_json
from .evidence_store import TestEvidenceStore
from .models import TestAssociation
from .static_links import SYMBOL_TYPES, TEST_NODE_TYPES, is_test_case


class TestIntelligenceService:
    """Return stable, bounded test associations without executing tests."""

    def __init__(self, graph: GraphStore, *, policy: StorageConfig | None = None,
                 suggestions: TestSuggestionPolicy | None = None) -> None:
        self.graph = graph
        suggestions = suggestions or default_config().testing.suggestions
        self.evidence_order = {kind: index for index, kind in enumerate(suggestions.evidence_order)}
        storage = getattr(graph, "path", None)
        self.evidence = TestEvidenceStore(Path(storage), policy=policy) if storage is not None else None

    def find(self, *, symbol_id: str | None = None, file: str | None = None,
             include_candidates: bool = False, limit: int = 20) -> list[TestAssociation]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Test association limit must be between 1 and 100")
        if type(include_candidates) is not bool:
            raise ValueError("include_candidates must be a boolean")
        if (symbol_id is None) == (file is None):
            raise ValueError("Select exactly one symbol or project-relative file")
        if symbol_id is not None:
            if not isinstance(symbol_id, str) or not symbol_id.strip():
                raise ValueError("symbol_id must be a non-empty string")
            node = self.graph.get_node(symbol_id, clone=False)
            targets = [node] if node and node.type in SYMBOL_TYPES else []
        else:
            if not isinstance(file, str):
                raise ValueError("file must be a project-relative string")
            path = normalize_relative_lookup_path(file)
            if not path or "\0" in path:
                raise ValueError("file must be a non-empty project-relative path")
            targets = [
                node for node in self.graph.find_nodes_by_property("relative_path", path, limit=None, clone=False)
                if node.type in SYMBOL_TYPES
            ]
        return self.associations(targets, include_candidates=include_candidates, limit=limit)

    def associations(self, targets: Iterable[MemoryNode], *, include_candidates: bool = False,
                     limit: int = 20) -> list[TestAssociation]:
        selected = {node.id: node for node in targets if node.type in SYMBOL_TYPES}
        if not selected:
            return []
        hashes = _SourceHashes(self.graph)
        out: list[TestAssociation] = []
        if self.evidence:
            with self.evidence.open(read_only=True) as evidence:
                if evidence is not None:
                    for target_id in selected:
                        rows = evidence.find_nodes_by_property("name", target_id, type_="TestObservation",
                                                               limit=None, clone=False)
                        out.extend(self._observed(row.properties, hashes) for row in rows)
        known_by_target: dict[str, set[str]] = {}
        if include_candidates:
            for row in out:
                known_by_target.setdefault(row.target_symbol_id, set()).add(row.test_id)
        test_paths: dict[str, dict[str, list[str]]] = {}
        for target in selected.values():
            if target.status != "active":
                continue
            static_tests = set()
            for edge in self.graph.get_edges(to_id=target.id, type_="TESTS", limit=None, clone=False):
                if edge.properties.get("evidence_kind") != "static" or edge.properties.get("status") != "active":
                    continue
                test = self.graph.get_node(edge.from_id, clone=False)
                if test is None or test.status != "active":
                    continue
                dependencies = self.graph.get_nodes(edge.properties.get("resolution_dependencies", []), clone=False)
                resolved_current = len(dependencies) == len(edge.properties.get("resolution_dependencies", [])) and all(
                    hashes.current(node) for node in dependencies
                )
                static_tests.add(test.id)
                out.append(TestAssociation(
                    test.id, target.id, "static", str(edge.properties["evidence_ref"]), hashes.revision_id(target),
                    hashes.compiled(target), bool(resolved_current and hashes.current(test) and hashes.current(target)),
                    _path(test), _path(target),
                ))
            if include_candidates:
                out.extend(self._candidates(target, known_by_target.get(target.id, set()) | static_tests, test_paths))
        out.sort(key=lambda row: (not row.verified_current, self.evidence_order[row.evidence_kind],
                                  row.test_path or "", row.test_id, row.target_symbol_id, row.evidence_ref or ""))
        return out[:limit]

    def _candidates(self, target: MemoryNode, known: set[str],
                    test_paths: dict[str, dict[str, list[str]]]) -> Iterable[TestAssociation]:
        """Look up exact naming matches without a preliminary lexical cutoff."""
        name = str(target.properties.get("name") or "")
        path = _path(target)
        project_id = target.properties.get("project_id")
        if not name or not path or not isinstance(project_id, str):
            return
        if project_id not in test_paths:
            paths_by_stem: dict[str, list[str]] = {}
            for test_path in project_test_paths(self.graph, project_id):
                paths_by_stem.setdefault(test_path_stem(test_path), []).append(test_path)
            test_paths[project_id] = paths_by_stem
        known.add(target.id)
        lookups = [("name", test_name) for test_name in candidate_test_names(name)]
        lookups.extend(("relative_path", test_path) for stem in candidate_test_stems(path)
                       for test_path in test_paths[project_id].get(stem, ()))
        for field, value in lookups:
            for node in self.graph.find_nodes_by_property(field, value, status="active", limit=None, clone=False):
                if node.id in known or node.properties.get("project_id") != project_id or not is_test_case(node):
                    continue
                known.add(node.id)
                yield TestAssociation(node.id, target.id, "candidate", "naming convention", None, None, False,
                                      _path(node), path)

    def import_coverage(self, input_path: str | Path, *, project_path: str | Path = ".") -> dict[str, Any]:
        """Import coverage.py JSON with optional capture-time SHA-256 metadata."""
        if self.evidence is None:
            raise ValueError("Test import requires persistent graph storage")
        root = Path(project_path).expanduser().resolve()
        project = self.graph.get_node_by_key("Project", root.as_posix())
        if project is None:
            raise ValueError("Compile the project before importing coverage")
        report, digest = read_coverage_json(root, input_path)
        metadata = report.get("reql", {})
        if not isinstance(metadata, dict) or not isinstance(metadata.get("source_hashes", {}), dict):
            raise ValueError("reql.source_hashes must be an object of project paths and SHA-256 digests")
        captured = {}
        for path, value in metadata.get("source_hashes", {}).items():
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
                raise ValueError("Coverage source hashes must be SHA-256 hex digests")
            captured[local_path(root, path).relative_to(root).as_posix()] = value.lower()

        symbols_by_path: dict[str, list[MemoryNode]] = {}
        hashes = _SourceHashes(self.graph)

        def symbols(path: str) -> list[MemoryNode]:
            if path not in symbols_by_path:
                symbols_by_path[path] = [
                    node for node in self.graph.find_nodes_by_property("relative_path", path, status="active", limit=None, clone=False)
                    if node.properties.get("project_id") == project.id and node.type in SYMBOL_TYPES | TEST_NODE_TYPES
                ]
            return symbols_by_path[path]

        rows: dict[str, dict[str, Any]] = {}
        skipped = 0
        for observed in observations(root, report):
            tests = [
                node for node in symbols(observed.test_path) if is_test_case(node)
                and _coverage_name_matches(node, observed.test_name)
            ]
            if len(tests) != 1:
                skipped += 1
                continue
            test = tests[0]
            possible = [
                node for node in symbols(observed.source_path)
                if node.type in {"Function", "Method", "Class"}
                and (start := node.properties.get("start_line")) is not None
                and (end := node.properties.get("end_line")) is not None
                and start < observed.line <= end
            ]
            # Prefer the innermost span, and skip overlapping ambiguous spans.
            possible.sort(key=lambda node: node.properties["end_line"] - node.properties["start_line"])
            if not possible or (len(possible) > 1 and _span(possible[0]) == _span(possible[1])):
                skipped += 1
                continue
            target = possible[0]
            if is_test_case(target):
                continue
            test_hash, target_hash = captured.get(observed.test_path), captured.get(observed.source_path)
            if ((test_hash and test_hash != hashes.compiled(test)) or (target_hash and target_hash != hashes.compiled(target))
                    or not hashes.current(test) or not hashes.current(target)):
                skipped += 1
                continue
            attested = bool(test_hash and target_hash)
            row = {
                "test_id": test.id, "target_symbol_id": target.id,
                "test_path": observed.test_path, "target_path": observed.source_path,
                "source_revision": hashes.revision_id(target) if attested else None,
                "source_hash": target_hash if attested else None,
                "test_hash": test_hash if attested else None,
                "evidence_ref": f"coverage-json:{digest}:{observed.context}",
                "evidence_kind": "observed",
            }
            key = stable_hash((digest, observed.context, test.id, target.id), length=64)
            rows[key] = row
        return {"schema_version": 1, "identified": len(rows), "imported": self.evidence.record(rows), "skipped_lines": skipped}

    def _observed(self, data: dict[str, Any], hashes: _SourceHashes) -> TestAssociation:
        test = self.graph.get_node(data["test_id"], clone=False)
        target = self.graph.get_node(data["target_symbol_id"], clone=False)
        fields = {key: value for key, value in data.items() if key not in {"name", "test_hash"}}
        fields["verified_current"] = bool(
            test and is_test_case(test) and target and data["source_hash"] and data["test_hash"]
            and hashes.current(test) == data["test_hash"] and hashes.current(target) == data["source_hash"]
        )
        return TestAssociation(**fields)

    def status(self) -> dict[str, int]:
        hashes = _SourceHashes(self.graph)
        counts = {"schema_version": 1, "observed": 0, "verified_current": 0, "stale_or_unverified": 0}
        if self.evidence:
            with self.evidence.open(read_only=True) as evidence:
                if evidence is not None:
                    for row in evidence.find_nodes_by_types(("TestObservation",), clone=False):
                        association = self._observed(row.properties, hashes)
                        counts["observed"] += 1
                        counts["verified_current" if association.verified_current else "stale_or_unverified"] += 1
        return counts

    def enrich_projection(self, rows: list[dict[str, Any]], targets: Iterable[MemoryNode],
                          *, limit: int) -> list[dict[str, Any]]:
        """Attach only the strongest evidence for each existing test path."""
        selected = {row["path"]: row for row in rows if row["kind"] == "test"}
        for item in selected.values():
            item.update(evidence_kind="candidate", evidence_ref="retrieval match", verified_current=False,
                        reason="test candidate")
        seen = set()
        for association in self.associations(targets, limit=40):
            path = association.test_path
            if not path or path in seen:
                continue
            test = self.graph.get_node(association.test_id, clone=False)
            if test is None or test.status != "active":
                continue
            seen.add(path)
            row = selected.setdefault(path, {"path": path, "kind": "test", "score": 1.0})
            row.update(
                asdict(association),
                line_start=test.properties.get("start_line"),
                line_end=test.properties.get("end_line"),
                symbols=[test.properties.get("qualified_name") or test.label],
                reason=f"{association.evidence_kind} test evidence",
            )
        ordered = sorted(selected.values(), key=lambda row: (not row["verified_current"],
                         self.evidence_order[row["evidence_kind"]], -row["score"], row["path"]))
        return ordered[:limit] or rows[:limit]


def _path(node: MemoryNode) -> str | None:
    value = node.properties.get("relative_path")
    return value if isinstance(value, str) else None


def _coverage_name_matches(node: MemoryNode, name: str) -> bool:
    """Match a fully qualified identity or a unique file-local test identity."""
    qualified = str(node.properties.get("qualified_name") or "")
    if qualified == name:
        return True
    if node.type == "Method" and "." not in name:
        return False
    return qualified.endswith("." + name) or (node.type == "Test" and node.properties.get("name") == name)


def _span(node: MemoryNode) -> tuple[int, int]:
    return node.properties["start_line"], node.properties["end_line"]


class _SourceHashes:
    """Cache committed provenance and confined on-disk hashes within a request."""

    def __init__(self, graph: GraphStore) -> None:
        self.graph = graph
        self.revisions = {}
        self.artifacts = {}
        self.verified = {}

    def _revision(self, node: MemoryNode):
        project_id = str(node.properties.get("project_id") or "")
        if project_id not in self.revisions:
            self.revisions[project_id] = RevisionRepository(self.graph).latest(project_id) if project_id else None
        return self.revisions[project_id]

    def revision_id(self, node: MemoryNode) -> str | None:
        revision = self._revision(node)
        return revision.id if revision else None

    def compiled(self, node: MemoryNode) -> str | None:
        artifact_id = str(node.properties.get("artifact_id") or "")
        if not artifact_id or node.status != "active":
            return None
        if artifact_id not in self.artifacts:
            self.artifacts[artifact_id] = self.graph.get_node(artifact_id, clone=False)
        artifact = self.artifacts[artifact_id]
        revision = self._revision(node)
        if artifact is None or artifact.status != "active" or revision is None:
            return None
        entry = revision.manifest.get(_path(node) or "", {})
        digest = artifact.properties.get("compiled_sha256")
        if entry.get("artifact_id") == artifact_id and entry.get("sha256") == digest == artifact.properties.get("sha256"):
            return digest
        return None

    def current(self, node: MemoryNode) -> str | None:
        digest = self.compiled(node)
        if digest is None:
            return None
        key = str(node.properties["artifact_id"])
        if key not in self.verified:
            project = self.graph.get_node(str(node.properties["project_id"]), clone=False)
            relative = _path(node)
            self.verified[key] = None
            if project is None or not relative:
                return None
            root = Path(project.properties["root_path"]).resolve()
            try:
                path = local_path(root, relative)
                if path.is_file():
                    _, current_digest, _ = fingerprint_file(path)
                    if current_digest == digest:
                        self.verified[key] = digest
            except (OSError, ValueError):
                return None
        return self.verified[key]
