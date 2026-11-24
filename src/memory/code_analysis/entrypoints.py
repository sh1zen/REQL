"""Entrypoint conventions shared by repository projections."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
import re

from ..artifacts.context_scope import is_test_context
from ..config import default_config
from ..config.models import WorkflowPolicy


WORKFLOW_NODE_TYPES = {"Class", "Endpoint", "Function", "Interface", "Method"}
TRIGGER_NODE_TYPES = {"Endpoint", "Function", "Method"}
WORKFLOW_EDGE_TYPES = {"CALLS", "HANDLES_ROUTE", "IMPORTS_FROM", "INSTANTIATES", "WRAPS"}
INFERRED_TRIGGER_REASON = "inferred call-graph root"
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[A-Za-z0-9]+")


def entrypoint_reason(
    node_type: str, properties: Mapping[str, object], label: str, *, policy: WorkflowPolicy | None = None,
) -> str | None:
    """Return the explicit evidence or convention that identifies a project entrypoint."""
    policy = policy if policy is not None else default_config().analysis.workflow
    path = str(properties.get("relative_path") or properties.get("source_file") or properties.get("path") or "")
    if node_type not in TRIGGER_NODE_TYPES or is_test_context(node_type, properties, path):
        return None
    if node_type == "Endpoint":
        return "explicit endpoint trigger"
    short_name = _short_name(str(properties.get("name") or properties.get("qualified_name") or label))
    if short_name in policy.excluded_names:
        return None
    if properties.get("is_entrypoint") is True:
        return "explicit entrypoint role"
    for key in ("roles", "semantic_roles"):
        value = properties.get(key)
        roles = (value,) if isinstance(value, str) else value
        if isinstance(roles, (list, tuple, set)) and any(_has_entry_role(str(role), policy) for role in roles):
            return "explicit entrypoint role"
    if short_name.startswith("_") and short_name not in policy.entry_names:
        return None
    if not set(policy.interface_path_tokens).isdisjoint(_word_tokens(path)):
        return "interface boundary convention"
    if short_name in policy.entry_names:
        return "process entrypoint convention"
    return None


def is_workflow_root_candidate(node_type: str, label: str, policy: WorkflowPolicy) -> bool:
    """Limit inferred triggers to eligible public executable symbols."""
    name = _short_name(label)
    return node_type in TRIGGER_NODE_TYPES and (
        node_type == "Endpoint" or (not name.startswith("_") and name not in policy.excluded_names)
    )


def _short_name(value: str) -> str:
    return value.partition("\n")[0].rsplit(".", 1)[-1].casefold()


def _has_entry_role(role: str, policy: WorkflowPolicy) -> bool:
    """Recognize role words and the compound public API role."""
    return not set(policy.entry_roles).isdisjoint(_word_tokens(role) + role.casefold().replace("_", "-").split())


def _word_tokens(value: str) -> list[str]:
    """Split path and role names on punctuation and camel-case boundaries."""
    return _WORD.findall(_CAMEL_BOUNDARY.sub(" ", value).casefold())


def workflow_edge_ids(
    from_id: str, to_id: str, edge_type: str, target_type: str,
) -> tuple[str, str] | None:
    """Orient stored workflow relationships from the trigger toward its implementation."""
    if edge_type not in WORKFLOW_EDGE_TYPES:
        return None
    if edge_type == "HANDLES_ROUTE" and target_type == "Endpoint":
        return to_id, from_id
    return from_id, to_id


def select_workflow_entries(
    outgoing: Mapping[str, Sequence[str]],
    reasons: Mapping[str, str],
    candidate_keys: Mapping[str, tuple[str, int, str, str]],
    endpoint_handlers: set[str],
    *,
    infer_roots: bool,
) -> tuple[list[str], bool]:
    """Select explicit triggers or cover the directed graph with executable roots."""
    explicit = [node_id for node_id in reasons if outgoing.get(node_id) and node_id not in endpoint_handlers]
    if explicit:
        return sorted(explicit, key=candidate_keys.__getitem__), False
    if not infer_roots:
        return [], False

    links = [(source, target) for source, targets in outgoing.items() for target in targets]
    components = strongly_connected_components(outgoing, links)
    component_by_node = {node_id: index for index, members in enumerate(components) for node_id in members}
    successors: dict[int, set[int]] = defaultdict(set)
    for source, target in links:
        successors[component_by_node[source]].add(component_by_node[target])

    covered: set[int] = set()
    roots: list[str] = []
    for index, members in enumerate(components):
        if index not in covered:
            candidates = [node_id for node_id in members if node_id in candidate_keys and outgoing.get(node_id)]
            if not candidates:
                continue
            roots.append(min(candidates, key=lambda node_id: (-len(outgoing[node_id]), *candidate_keys[node_id])))
        covered.update(successors[index])
    return sorted(roots, key=candidate_keys.__getitem__), bool(roots)


def strongly_connected_components(
    node_ids: Iterable[str],
    edges: Iterable[tuple[str, str]],
) -> list[set[str]]:
    """Return components in source-to-target order without recursive traversal."""
    forward: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)
    nodes = set(node_ids)
    for source, target in edges:
        nodes.update((source, target))
        forward[source].add(target)
        reverse[target].add(source)

    visited: set[str] = set()
    finished: list[str] = []
    for root in sorted(nodes):
        stack = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                finished.append(node)
            elif node not in visited:
                visited.add(node)
                stack.append((node, True))
                stack.extend((target, False) for target in sorted(forward[node], reverse=True))

    assigned: set[str] = set()
    components: list[set[str]] = []
    for root in reversed(finished):
        if root in assigned:
            continue
        component = {root}
        assigned.add(root)
        pending = [root]
        while pending:
            for source in reverse[pending.pop()] - assigned:
                assigned.add(source)
                component.add(source)
                pending.append(source)
        components.append(component)
    return components
