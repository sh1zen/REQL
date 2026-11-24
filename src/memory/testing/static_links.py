"""Derive factual TESTS edges from the compiler's resolved AST relations."""
from __future__ import annotations

from ..artifacts.compiler import _typed_edge
from ..artifacts.context_scope import matches_test_case_name
from ..artifacts.models import ArtifactCompilationResult
from ..domain.models import MemoryNode
from ..storage.graph_store import GraphStore

RESOLVER = "test_static_links"
SYMBOL_TYPES = {"Function", "Method", "Class", "Interface", "Variable"}
TEST_NODE_TYPES = {"Function", "Method", "Test"}


def is_test_case(node: MemoryNode) -> bool:
    """Use the compiled case label, or recognize the case in a legacy index."""
    if (node.properties.get("context_scope") != "test" or node.type not in TEST_NODE_TYPES
            or node.properties.get("ambiguous_declaration")):
        return False
    if node.type == "Test":
        return True
    if "test_case" in node.properties:
        return node.properties["test_case"] is True
    return bool((node.properties.get("metadata") or {}).get("test_case")
                or matches_test_case_name(str(node.properties.get("name") or "")))


def refresh_static_links(store: GraphStore, project_id: str) -> ArtifactCompilationResult:
    """Reconcile active test edges after the project's code relations settle."""
    result = ArtifactCompilationResult(artifact_id="*")
    prior = {edge.id: edge for edge in store.find_edges_by_property("project_id", project_id, type_="TESTS", limit=None, clone=False)
             if edge.properties.get("resolver") == RESOLVER}
    tests = [
        node for kind in sorted(TEST_NODE_TYPES)
        for node in store.find_nodes_by_property("project_id", project_id, type_=kind, status="active", limit=None, clone=False)
        if is_test_case(node) and node.type != "Test"
    ]
    desired = {}
    for test in tests:
        for edge in store.get_edges(from_id=test.id, type_=("CALLS", "INSTANTIATES", "REFERENCES", "READS", "RETURNS"),
                                    limit=None, clone=False):
            if not edge.properties.get("resolver") or edge.properties.get("status") == "archived":
                continue
            target = store.get_node(edge.to_id, clone=False)
            if target is None or target.status != "active" or target.type not in SYMBOL_TYPES or is_test_case(target):
                continue
            link = _typed_edge(
                test.id, target.id, "TESTS",
                {"project_id": project_id, "artifact_id": test.properties["artifact_id"],
                 "resolver": RESOLVER, "evidence_kind": "static", "evidence_ref": edge.id, "status": "active",
                 "resolution_dependencies": edge.properties.get("resolution_dependencies", [])},
                source_file=test.properties.get("relative_path"), line_start=edge.properties.get("line_start"),
                extractor=RESOLVER, evidence=edge.properties.get("evidence"),
            )
            previous = desired.get(link.id)
            if previous is None or edge.id < previous.properties["evidence_ref"]:
                desired[link.id] = link
    for edge_id, link in desired.items():
        old = prior.get(edge_id)
        if old is not None and all(old.properties.get(key) == link.properties.get(key)
                                   for key in ("evidence_ref", "status", "line_start", "evidence", "resolution_dependencies")):
            continue
        persisted, created = store.upsert_edge(link, return_clone=False)
        result.record_edge(persisted.id, created=created)
    for edge_id in prior.keys() - desired.keys():
        old = prior[edge_id]
        if old.properties.get("status") != "archived":
            store.update_edge_fields(old.id, weight=0.0, properties={**old.properties, "status": "archived"})
            result.archived_edges.add(edge_id)
    return result
