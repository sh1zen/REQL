"""Derive a business-oriented repository view from deterministic code facts."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePosixPath
import re
from typing import Any, Iterable, Sequence

from ..artifacts.context_scope import is_test_context_path
from ..code_analysis.entrypoints import entrypoint_reason
from ..config import AnalysisConfig, default_config
from ..config.models import ArchitecturePolicy, WorkflowPolicy
from ..domain.ids import stable_id
from ..domain.models import MemoryNode
from ..storage.graph_store import GraphStore
from .models import (
    ArchitectureLayer,
    BusinessCapability,
    BusinessWorkflow,
    ChangeGuide,
    CodeEvidence,
    RepositoryExplanation,
    WorkflowParticipant,
)


ACTIVE_STATUSES = {"active"}
CAPABILITY_NODE_TYPES = {"Module", "Class", "Interface", "Function", "Endpoint", "Schema"}
OWNER_NODE_TYPES = {"Class", "Interface", "Function", "Method", "Endpoint", "Schema"}
FLOW_NODE_TYPES = OWNER_NODE_TYPES | {"Module"}
WORKFLOW_PARTICIPANT_TYPES = {"Class", "Interface", "Function", "Method", "Endpoint"}
WORKFLOW_IMPLEMENTATION_EDGE_TYPES = {
    "CALLS",
    "HANDLES_ROUTE",
    "INSTANTIATES",
    "WRAPS",
}
WORKFLOW_OUTPUT_EDGE_TYPES = {"EMITS", "WRITES"}
DEPENDENCY_EDGE_TYPES = {
    "CALLS",
    "DEPENDS_ON",
    "HANDLES_ROUTE",
    "IMPLEMENTS",
    "IMPORTS",
    "IMPORTS_FROM",
    "INSTANTIATES",
    "USES",
    "WRAPS",
}
LAYER_ORDER = ("interface", "application", "domain", "core", "infrastructure")
LAYER_PURPOSES = {
    "interface": "exposes commands, APIs, routes, protocols, or user-facing adapters",
    "application": "coordinates use cases and repository workflows",
    "domain": "defines business entities, policies, schemas, and rules",
    "core": "implements the repository's central deterministic behavior",
    "infrastructure": "provides storage, configuration, persistence, caching, and external adapters",
}
WORD_RE = re.compile(r"[A-Za-z0-9]+")
CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(slots=True)
class _CapabilityGroup:
    key: str
    nodes: dict[str, MemoryNode] = field(default_factory=dict)
    paths: Counter[str] = field(default_factory=Counter)
    score: float = 0.0
    focus_score: float = 0.0


@dataclass(slots=True)
class _WorkflowCandidate:
    anchor: MemoryNode
    members: dict[str, MemoryNode]
    relations: dict[str, set[str]]
    predecessors: dict[str, set[str]]
    documentation: list[CodeEvidence]
    focus_score: int
    score: float


class RepositoryExplanationService:
    """Compute a read-only business projection over a compiled project graph."""

    def __init__(self, store: GraphStore, *, config: AnalysisConfig | None = None) -> None:
        self.store = store
        self.config = config if config is not None else default_config().analysis

    def explain(
        self,
        project: MemoryNode,
        *,
        focus: str | None = None,
        max_capabilities: int = 12,
        max_workflows: int = 8,
    ) -> RepositoryExplanation:
        if project.type != "Project":
            raise ValueError("Repository explanation requires a Project node")
        if max_capabilities < 1:
            raise ValueError("max_capabilities must be at least 1")
        if max_workflows < 0:
            raise ValueError("max_workflows must not be negative")

        project_id = project.id
        artifacts = self._project_nodes(project_id, "SourceArtifact")
        code_artifacts = [node for node in artifacts if str(node.properties.get("artifact_type") or "") == "code"]
        code_artifact_ids = {node.id for node in code_artifacts}
        documentation_nodes = [
            node
            for node in self._project_nodes(project_id, "SourceFragment")
            if node.properties.get("artifact_id") not in code_artifact_ids
            and not is_test_context_path(_node_path(node))
        ]
        test_paths = sorted(
            {
                path
                for node in code_artifacts
                if (path := _node_path(node)) and is_test_context_path(path)
            }
        )
        modules = [
            node
            for node in self._project_nodes(project_id, "Module")
            if (path := _node_path(node)) and not is_test_context_path(path)
        ]
        capability_nodes = self._capability_nodes(project_id, modules)
        paths = [_node_path(node) for node in modules]
        dominant_root = _dominant_source_root([path for path in paths if path], self.config.architecture)
        groups, node_groups = self._group_nodes(capability_nodes, dominant_root)

        top_nodes = self.store.top_nodes_by_degree(
            limit=None,
            node_types=FLOW_NODE_TYPES,
            statuses=ACTIVE_STATUSES,
            project_id=project_id,
            include_global_project=False,
            clone=False,
        )
        degree_by_id = {node.id: degree for node, degree, _ in top_nodes}
        for node, degree, weighted_degree in top_nodes:
            path = _node_path(node)
            if not path or is_test_context_path(path):
                continue
            key = _capability_key(path, dominant_root, self.config.architecture.source_roots)
            if not key:
                continue
            group = groups.setdefault(key, _CapabilityGroup(key))
            group.nodes[node.id] = node
            group.paths[path] += 1
            group.score += 0.25 + float(degree) + float(weighted_degree)
            node_groups[node.id] = key

        focus_matches = self._focus_matches(project_id, focus)
        for node, score in focus_matches:
            key = _capability_key(_node_path(node), dominant_root, self.config.architecture.source_roots)
            if key and key in groups:
                groups[key].focus_score += score
                groups[key].score += score * 8.0

        dependencies = self._capability_dependencies(
            node_groups,
            dominant_root=dominant_root,
            known_groups=set(groups),
        )
        capabilities = self._build_capabilities(
            project_id,
            groups,
            dependencies,
            degree_by_id,
            test_paths,
            max_capabilities=max_capabilities,
        )
        selected_ids = {capability.id for capability in capabilities}
        layers = self._build_layers(capabilities)
        workflows = self._build_workflows(
            project_id,
            top_nodes,
            node_groups=node_groups,
            documentation_nodes=documentation_nodes,
            focus=focus,
            max_workflows=max_workflows,
        )
        change_guide = self._build_change_guide(
            focus,
            focus_matches,
            capabilities,
            dominant_root,
            selected_ids,
        )
        languages = Counter(
            str(node.properties.get("language") or "unknown")
            for node in code_artifacts
        )
        artifact_types = Counter(str(node.properties.get("artifact_type") or "unknown") for node in artifacts)
        project_name = str(project.properties.get("name") or project.label or project.id)
        summary = (
            f"{project_name} contains {len(capabilities)} inferred business capabilities "
            f"across {len(layers)} architectural layers. The explanation is derived from "
            f"{len(modules)} code modules and {len(capability_nodes)} high-signal code symbols."
        )
        return RepositoryExplanation(
            project={
                "id": project.id,
                "name": project_name,
                "root_path": project.properties.get("root_path") or project.canonical_key or project.text,
            },
            summary=summary,
            basis={
                "mode": "deterministic-code-graph",
                "persisted": False,
                "llm_required": False,
                "workflow_projection": "semantic-multi-evidence",
                "focus": focus,
                "artifacts": len(artifacts),
                "code_modules": len(modules),
                "high_signal_symbols": len(capability_nodes),
                "artifact_types": dict(sorted(artifact_types.items())),
                "languages": dict(sorted(languages.items())),
            },
            layers=layers,
            capabilities=capabilities,
            workflows=workflows,
            change_guide=change_guide,
        )

    def _project_nodes(self, project_id: str, node_type: str) -> list[MemoryNode]:
        return self.store.find_nodes_by_property(
            "project_id",
            project_id,
            type_=node_type,
            status="active",
            limit=None,
            clone=False,
        )

    def _capability_nodes(self, project_id: str, modules: Sequence[MemoryNode]) -> list[MemoryNode]:
        nodes: dict[str, MemoryNode] = {node.id: node for node in modules}
        for node_type in CAPABILITY_NODE_TYPES - {"Module"}:
            for node in self._project_nodes(project_id, node_type):
                path = _node_path(node)
                if path and not is_test_context_path(path):
                    nodes[node.id] = node
        return list(nodes.values())

    def _group_nodes(
        self,
        nodes: Sequence[MemoryNode],
        dominant_root: str | None,
    ) -> tuple[dict[str, _CapabilityGroup], dict[str, str]]:
        groups: dict[str, _CapabilityGroup] = {}
        node_groups: dict[str, str] = {}
        for node in nodes:
            path = _node_path(node)
            key = _capability_key(path, dominant_root, self.config.architecture.source_roots)
            if not key:
                continue
            group = groups.setdefault(key, _CapabilityGroup(key))
            group.nodes[node.id] = node
            group.paths[path] += 1
            group.score += 1.0 if node.type == "Module" else 0.4
            node_groups[node.id] = key
        return groups, node_groups

    def _focus_matches(self, project_id: str, focus: str | None) -> list[tuple[MemoryNode, float]]:
        if not focus or not focus.strip():
            return []
        results = self.store.lexical_search(
            focus,
            top_k=None,
            node_types=CAPABILITY_NODE_TYPES | {"Method", "Test", "SourceArtifact"},
            include_archived=False,
        )
        matches = []
        for node, score in results:
            if node.properties.get("project_id") != project_id:
                continue
            if not _node_path(node):
                continue
            matches.append((node, float(score)))
        return matches

    def _capability_dependencies(
        self,
        node_groups: dict[str, str],
        *,
        dominant_root: str | None,
        known_groups: set[str],
    ) -> Counter[tuple[str, str]]:
        node_ids = list(node_groups)
        if not node_ids:
            return Counter()
        edges = self.store.incident_edges(
            node_ids,
            edge_types=DEPENDENCY_EDGE_TYPES,
            limit=None,
            clone=False,
        )
        dependencies: Counter[tuple[str, str]] = Counter()
        for edge in edges:
            source_group = node_groups.get(edge.from_id)
            target_group = node_groups.get(edge.to_id)
            if source_group and not target_group:
                target = self.store.get_node(edge.to_id, clone=False)
                if target is not None:
                    resolved_path = str(target.properties.get("resolved_relative_path") or "")
                    resolved_group = _capability_key(resolved_path, dominant_root, self.config.architecture.source_roots)
                    if resolved_group in known_groups:
                        target_group = resolved_group
            if source_group and target_group and source_group != target_group:
                dependencies[(source_group, target_group)] += 1
        return dependencies

    def _build_capabilities(
        self,
        project_id: str,
        groups: dict[str, _CapabilityGroup],
        dependencies: Counter[tuple[str, str]],
        degree_by_id: dict[str, int],
        test_paths: Sequence[str],
        *,
        max_capabilities: int,
    ) -> list[BusinessCapability]:
        ranked_groups = sorted(
            groups.values(),
            key=lambda item: (-item.focus_score, -item.score, item.key),
        )[:max_capabilities]
        names = {group.key: _capability_name(group, degree_by_id, self.config.architecture) for group in ranked_groups}
        capabilities = []
        for group in ranked_groups:
            ranked_owners = sorted(
                (
                    node
                    for node in group.nodes.values()
                    if node.type in OWNER_NODE_TYPES and _is_public_owner(node)
                ),
                key=lambda node: _owner_rank(node, degree_by_id, self.config.architecture),
            )
            if not ranked_owners:
                ranked_owners = sorted(
                    (node for node in group.nodes.values() if node.type in OWNER_NODE_TYPES),
                    key=lambda node: _owner_rank(node, degree_by_id, self.config.architecture),
                )
            owners = _dedupe_evidence(
                _evidence(node, reason="high-signal owner in this capability")
                for node in ranked_owners
            )[:4]
            responsibilities = _responsibilities(ranked_owners, fallback=group.key)
            layer = _infer_layer(group, self.config)
            dependency_names = [
                names[target]
                for (source, target), _count in sorted(
                    dependencies.items(),
                    key=lambda item: (-item[1], item[0][1]),
                )
                if source == group.key and target in names
            ][:5]
            primary_paths = [
                path
                for path, _count in sorted(
                    group.paths.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ][:5]
            associated_tests = _associated_tests(group.key, responsibilities, test_paths, self.config.architecture)
            purpose = _capability_purpose(names[group.key], responsibilities, dependency_names)
            capabilities.append(
                BusinessCapability(
                    id=stable_id("business-capability", project_id, group.key),
                    name=names[group.key],
                    layer=layer,
                    purpose=purpose,
                    responsibilities=responsibilities,
                    primary_paths=primary_paths,
                    dependencies=list(dict.fromkeys(dependency_names)),
                    owners=owners,
                    tests=associated_tests,
                    score=group.score,
                )
            )
        return capabilities

    def _build_layers(self, capabilities: Sequence[BusinessCapability]) -> list[ArchitectureLayer]:
        layers = []
        for layer_name in LAYER_ORDER:
            members = [item for item in capabilities if item.layer == layer_name]
            if not members:
                continue
            layers.append(
                ArchitectureLayer(
                    name=layer_name,
                    purpose=LAYER_PURPOSES[layer_name],
                    capability_ids=[item.id for item in members],
                    paths=sorted({path for item in members for path in item.primary_paths}),
                )
            )
        return layers

    def _build_workflows(
        self,
        project_id: str,
        top_nodes: Sequence[tuple[MemoryNode, int, float]],
        *,
        node_groups: dict[str, str],
        documentation_nodes: Sequence[MemoryNode],
        focus: str | None,
        max_workflows: int,
    ) -> list[BusinessWorkflow]:
        if max_workflows == 0:
            return []
        focus_tokens = _tokens(focus or "")
        documentation_tokens: dict[str, set[str]] = {}
        candidates: list[_WorkflowCandidate] = []
        for node, degree, weighted_degree in top_nodes:
            trigger_reason = _workflow_trigger_reason(node, self.config.workflow)
            if trigger_reason is None:
                continue
            members, relations, predecessors = self._workflow_implementation_nodes(
                node,
                project_id,
            )
            if len(members) < 2 or not relations:
                continue
            concept_tokens = _workflow_concept_tokens(node, self.config.workflow)
            documentation = _workflow_documentation_evidence(
                documentation_nodes,
                concept_tokens,
                tokens_by_id=documentation_tokens,
            )
            documentation.extend(
                _workflow_docstring_evidence(
                    members.values(),
                    concept_tokens,
                )
            )
            documentation = _dedupe_evidence(documentation)
            all_text = " ".join(
                f"{_node_label(member)} {_node_path(member)}"
                for member in members.values()
            )
            focus_score = len(focus_tokens & _tokens(all_text))
            relation_kinds = {kind for kinds in relations.values() for kind in kinds}
            paths = {_node_path(member) for member in members.values() if _node_path(member)}
            groups = {node_groups[node_id] for node_id in members if node_id in node_groups}
            score = (
                focus_score * 20.0
                + len(members) * 3.0
                + len(relation_kinds) * 2.0
                + len(documentation) * 2.0
                + min(3, max(0, len(paths) - 1))
                + min(3, max(0, len(groups) - 1))
                + min(20.0, float(degree) + float(weighted_degree))
            )
            candidates.append(
                _WorkflowCandidate(
                    anchor=node,
                    members=members,
                    relations=relations,
                    predecessors=predecessors,
                    documentation=documentation,
                    focus_score=focus_score,
                    score=score,
                )
            )

        candidates.sort(
            key=lambda item: (
                -item.focus_score,
                -item.score,
                -len(item.members),
                _node_label(item.anchor).casefold(),
                item.anchor.id,
            )
        )
        selected: list[_WorkflowCandidate] = []
        for candidate in candidates:
            if any(_workflow_candidates_overlap(candidate, existing, self.config.workflow) for existing in selected):
                continue
            selected.append(candidate)
            if len(selected) >= max_workflows:
                break

        workflows = []
        for candidate in selected:
            anchor = candidate.anchor
            trigger_reason = _workflow_trigger_reason(anchor, self.config.workflow) or "explicit workflow trigger"
            ordered_members = [anchor] + sorted(
                (node for node_id, node in candidate.members.items() if node_id != anchor.id),
                key=lambda node: (
                    _workflow_role(node, self.config.workflow, is_trigger=False),
                    _node_label(node).casefold(),
                    node.id,
                ),
            )
            participants = []
            for member in ordered_members:
                is_trigger = member.id == anchor.id
                if is_trigger:
                    reason = trigger_reason
                else:
                    kinds = ", ".join(sorted(candidate.relations.get(member.id, set())))
                    sources = sorted(
                        {
                            _humanize_symbol(_node_label(candidate.members[source_id]))
                            for source_id in candidate.predecessors.get(member.id, set())
                            if source_id in candidate.members
                        }
                    )
                    source_suffix = f" from {', '.join(sources)}" if sources else ""
                    reason = f"implemented_by supported by {kinds}{source_suffix}"
                    anchor_group = node_groups.get(anchor.id)
                    member_group = node_groups.get(member.id)
                    if anchor_group and member_group and anchor_group != member_group:
                        reason += f"; crosses capability boundary {anchor_group} -> {member_group}"
                participants.append(
                    WorkflowParticipant(
                        relation="implemented_by",
                        role=_workflow_role(member, self.config.workflow, is_trigger=is_trigger),
                        target=_evidence(member, reason=reason),
                    )
                )

            name = _humanize_symbol(_node_label(anchor))
            inputs = _workflow_inputs(anchor)
            outputs = self._workflow_outputs(anchor)
            invariants = self._workflow_invariants(ordered_members)
            evidence = _dedupe_evidence(
                candidate.documentation
                + [participant.target for participant in participants]
            )
            member_ids = sorted(candidate.members)
            workflows.append(
                BusinessWorkflow(
                    id=stable_id("business-workflow", project_id, anchor.id, *member_ids),
                    name=name,
                    intent=_workflow_intent(anchor, ordered_members, self.config.architecture),
                    trigger=name,
                    inputs=inputs,
                    outputs=outputs,
                    invariants=invariants,
                    participants=participants,
                    evidence=evidence,
                )
            )
        return workflows

    def _workflow_implementation_nodes(
        self,
        anchor: MemoryNode,
        project_id: str,
    ) -> tuple[dict[str, MemoryNode], dict[str, set[str]], dict[str, set[str]]]:
        members = {anchor.id: anchor}
        relations: dict[str, set[str]] = defaultdict(set)
        predecessors: dict[str, set[str]] = defaultdict(set)
        queue = deque([anchor])
        while queue:
            current = queue.popleft()
            edges = self.store.get_edges(
                from_id=current.id,
                type_=sorted(WORKFLOW_IMPLEMENTATION_EDGE_TYPES),
                limit=None,
                clone=False,
            )
            for edge in sorted(edges, key=lambda item: (item.type, item.to_id, item.id)):
                if edge.properties.get("status") == "archived":
                    continue
                target = self.store.get_node(edge.to_id, clone=False)
                if not _is_workflow_participant(target, project_id, self.config.workflow):
                    continue
                assert target is not None
                if target.id not in members:
                    queue.append(target)
                    members[target.id] = target
                relations[target.id].add(edge.type)
                predecessors[target.id].add(current.id)
        self._add_import_participants(
            anchor,
            project_id,
            members,
            relations,
            predecessors,
        )
        return members, dict(relations), dict(predecessors)

    def _add_import_participants(
        self,
        anchor: MemoryNode,
        project_id: str,
        members: dict[str, MemoryNode],
        relations: dict[str, set[str]],
        predecessors: dict[str, set[str]],
    ) -> None:
        """Follow corroborated import chains from the workflow participants."""
        queue = deque(members.values())
        artifacts: dict[str, tuple[list[MemoryNode], dict[str, list[str]]]] = {}
        targets_by_property: dict[tuple[str, str], list[MemoryNode]] = {}
        while queue:
            source = queue.popleft()
            artifact_id = str(source.properties.get("artifact_id") or "")
            if not artifact_id:
                continue
            if artifact_id not in artifacts:
                imports = self.store.find_nodes_by_property(
                    "artifact_id", artifact_id, type_="Import", status="active", limit=None, clone=False,
                )
                fragments: dict[str, list[str]] = defaultdict(list)
                if imports:
                    for fragment in self.store.find_nodes_by_property(
                        "artifact_id", artifact_id, type_="SourceFragment", status="active", limit=None, clone=False,
                    ):
                        symbol_id = fragment.properties.get("symbol_id")
                        if isinstance(symbol_id, str):
                            fragments[symbol_id].append(str(fragment.text or ""))
                artifacts[artifact_id] = imports, fragments
            imports, fragments = artifacts[artifact_id]
            if not imports:
                continue
            source_text = _implementation_body_text(source, "\n".join(fragments.get(source.id, ())))
            if not source_text:
                continue
            for import_node in imports:
                resolved_path = str(import_node.properties.get("resolved_relative_path") or "")
                imported_name = str(import_node.properties.get("name") or "").rsplit(".", 1)[-1]
                module_hint = str(import_node.properties.get("module") or "")
                if not imported_name:
                    continue
                reference_name = str(import_node.properties.get("alias") or imported_name)
                if not _source_references_name(source_text, reference_name):
                    continue
                key = ("relative_path", resolved_path) if resolved_path else ("name", imported_name)
                if key not in targets_by_property:
                    targets_by_property[key] = self.store.find_nodes_by_property(
                        *key, status="active", limit=None, clone=False,
                    )
                targets = targets_by_property[key]
                for target in targets:
                    if not _is_workflow_participant(target, project_id, self.config.workflow):
                        continue
                    assert target is not None
                    target_name = _node_label(target).rsplit(".", 1)[-1]
                    if target_name.casefold() != imported_name.casefold():
                        continue
                    if module_hint and not _module_hint_matches_path(module_hint, _node_path(target)):
                        continue
                    if not _import_matches_workflow(anchor, target, self.config):
                        continue
                    if target.id not in members:
                        members[target.id] = target
                        if _workflow_role(target, self.config.workflow, is_trigger=False) in {
                            "orchestration", "infrastructure", "policy",
                        }:
                            queue.append(target)
                    relations[target.id].add("IMPORTS_FROM")
                    predecessors[target.id].add(source.id)

    def _workflow_outputs(self, anchor: MemoryNode) -> list[str]:
        outputs = []
        return_type = str(anchor.properties.get("returns") or "").strip()
        if return_type:
            outputs.append(return_type)
        edges = self.store.get_edges(
            from_id=anchor.id,
            type_=sorted(WORKFLOW_OUTPUT_EDGE_TYPES),
            limit=None,
            clone=False,
        )
        for edge in edges:
            target = self.store.get_node(edge.to_id, clone=False)
            if target is None or is_test_context_path(_node_path(target)):
                continue
            outputs.append(f"{_humanize_symbol(edge.type)} {_humanize_symbol(_node_label(target))}")
        return list(dict.fromkeys(output for output in outputs if output))

    def _workflow_invariants(self, members: Sequence[MemoryNode]) -> list[str]:
        """Summarize indexed failure conditions from the workflow participants."""
        conditions: dict[str, None] = {}
        for member in members:
            edges = self.store.get_edges(from_id=member.id, type_="RAISES", limit=None, clone=False)
            for edge in edges:
                if edge.properties.get("status") == "archived":
                    continue
                name = edge.properties.get("name") or edge.properties.get("evidence")
                if isinstance(name, str) and name:
                    conditions.setdefault(f"{_node_label(member)} raises {name}", None)
                if len(conditions) >= 5:
                    return list(conditions)
        return list(conditions)

    def _build_change_guide(
        self,
        focus: str | None,
        focus_matches: Sequence[tuple[MemoryNode, float]],
        capabilities: Sequence[BusinessCapability],
        dominant_root: str | None,
        selected_ids: set[str],
    ) -> ChangeGuide:
        capabilities_by_key = {
            _capability_key(path, dominant_root, self.config.architecture.source_roots): capability
            for capability in capabilities
            for path in capability.primary_paths
        }
        capability_ids = []
        start_here = []
        if focus:
            for node, score in focus_matches:
                if is_test_context_path(_node_path(node)):
                    continue
                key = _capability_key(_node_path(node), dominant_root, self.config.architecture.source_roots)
                capability = capabilities_by_key.get(key)
                if capability and capability.id in selected_ids and capability.id not in capability_ids:
                    capability_ids.append(capability.id)
                start_here.append(_evidence(node, reason=f"lexical match for '{focus}'", score=score))
                if len(start_here) >= 8:
                    break
            rationale = (
                f"Start with the code facts ranked for '{focus}', then follow the owning capability "
                "and its declared verification paths before editing."
            )
        else:
            for capability in capabilities[:3]:
                capability_ids.append(capability.id)
                start_here.extend(capability.owners[:1])
            rationale = (
                "No focus was supplied, so the guide starts from the highest-signal capability owners. "
                "Pass a focus phrase to rank change-specific files and symbols."
            )
        verify_with = list(
            dict.fromkeys(
                test
                for capability in capabilities
                if not capability_ids or capability.id in capability_ids
                for test in capability.tests
            )
        )[:12]
        return ChangeGuide(
            focus=focus,
            rationale=rationale,
            capability_ids=capability_ids,
            start_here=_dedupe_evidence(start_here)[:8],
            verify_with=verify_with,
        )


def _node_path(node: MemoryNode) -> str:
    value = (
        node.properties.get("relative_path")
        or node.properties.get("source_file")
        or node.properties.get("path")
        or ""
    )
    return str(value).replace("\\", "/").lstrip("./")


def _node_label(node: MemoryNode) -> str:
    value = node.properties.get("name") or node.properties.get("qualified_name") or node.label or node.text or node.id
    return str(value).splitlines()[0]


def _path_parts(path: str, source_roots: tuple[str, ...]) -> list[str]:
    if not path:
        return []
    parts = [part for part in PurePosixPath(path).parts if part not in {"", "."}]
    while parts and parts[0].casefold() in source_roots:
        parts.pop(0)
    if not parts:
        return []
    filename = parts[-1]
    stem = filename
    while "." in stem:
        next_stem = stem.rsplit(".", 1)[0]
        if not next_stem:
            break
        stem = next_stem
    if stem.casefold() in {"__init__", "index", "mod"} and len(parts) > 1:
        parts.pop()
    else:
        parts[-1] = stem
    return [part for part in parts if part]


def _dominant_source_root(paths: Sequence[str], policy: ArchitecturePolicy) -> str | None:
    roots = Counter(parts[0].casefold() for path in paths if (parts := _path_parts(path, policy.source_roots)))
    if not roots:
        return None
    root, count = roots.most_common(1)[0]
    required = max(policy.dominant_root_min_paths, int(len(paths) * policy.dominant_root_ratio))
    return root if count >= required else None


@lru_cache(maxsize=4096)
def _capability_key(path: str, dominant_root: str | None, source_roots: tuple[str, ...]) -> str | None:
    """Reuse immutable path classification for symbols sharing a source file."""
    parts = _path_parts(path, source_roots)
    if not parts:
        return None
    folded = [part.casefold() for part in parts]
    if is_test_context_path(path):
        return None
    if dominant_root and folded[0] == dominant_root and len(parts) > 1:
        return folded[1]
    return folded[0]


def _is_public_owner(node: MemoryNode) -> bool:
    name = _node_label(node).rsplit(".", 1)[-1]
    if node.type == "Endpoint":
        return True
    return not name.startswith("_")


def _capability_name(group: _CapabilityGroup, degree_by_id: dict[str, int], policy: ArchitecturePolicy) -> str:
    fallback = _humanize_symbol(group.key)
    if group.key not in policy.generic_capability_names:
        return fallback
    owners = sorted(
        (node for node in group.nodes.values() if node.type in OWNER_NODE_TYPES and _is_public_owner(node)),
        key=lambda node: _owner_rank(node, degree_by_id, policy),
    )
    concepts = []
    for node in owners:
        concept = _owner_concept(_node_label(node), policy.symbol_suffixes)
        if concept and concept.casefold() not in {item.casefold() for item in concepts}:
            concepts.append(concept)
        if len(concepts) >= 3:
            break
    return " / ".join(concepts) if concepts else fallback


def _owner_concept(value: str, suffixes: tuple[str, ...]) -> str:
    short = value.rsplit(".", 1)[-1]
    for suffix in suffixes:
        if short.endswith(suffix) and len(short) > len(suffix):
            short = short[: -len(suffix)]
            break
    return _humanize_symbol(short)


def _responsibilities(nodes: Sequence[MemoryNode], *, fallback: str) -> list[str]:
    values = []
    for node in nodes:
        value = _humanize_symbol(_node_label(node).rsplit(".", 1)[-1])
        if value and value.casefold() not in {item.casefold() for item in values}:
            values.append(value)
        if len(values) >= 5:
            break
    return values or [_humanize_symbol(fallback)]


def _infer_layer(group: _CapabilityGroup, config: AnalysisConfig) -> str:
    """Use indexed roles and explicit project rules to assign a layer."""
    policy = config.architecture
    parent_paths = " ".join(str(PurePosixPath(path).parent) for path in group.paths)
    tokens = _tokens(f"{group.key} {parent_paths}")
    evidence = set()
    if any(node.type == "Endpoint" or node.properties.get("is_entrypoint") is True for node in group.nodes.values()):
        evidence.add("interface")
    if any(node.type == "Schema" for node in group.nodes.values()):
        evidence.add("domain")
    layer_tokens = {
        "interface": config.workflow.interface_path_tokens,
        "application": policy.application_tokens,
        "domain": policy.domain_tokens,
        "infrastructure": policy.infrastructure_tokens,
        "core": policy.core_tokens,
    }
    for layer in policy.layer_precedence:
        if layer in evidence or not tokens.isdisjoint(layer_tokens[layer]):
            return layer
    return policy.fallback_layer


def _associated_tests(
    key: str, responsibilities: Sequence[str], test_paths: Sequence[str], policy: ArchitecturePolicy,
) -> list[str]:
    match_tokens = _tokens(f"{key} {' '.join(responsibilities)}") - set(policy.generic_capability_names)
    if not match_tokens:
        return []
    scored = []
    for path in test_paths:
        overlap = len(match_tokens & _tokens(path))
        if overlap:
            scored.append((overlap, path))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _score, path in scored[:6]]


def _capability_purpose(name: str, responsibilities: Sequence[str], dependencies: Sequence[str]) -> str:
    details = ", ".join(responsibilities[:4]) if responsibilities else name.casefold()
    purpose = f"Owns {name.casefold()} behavior through {details}."
    if dependencies:
        purpose += f" Collaborates with {', '.join(dependencies[:3])}."
    return purpose


def _workflow_trigger_reason(node: MemoryNode, policy: WorkflowPolicy) -> str | None:
    """Classify a stored symbol as a workflow trigger."""
    return entrypoint_reason(node.type, node.properties, node.label or node.text or node.id, policy=policy)


def _is_workflow_participant(node: MemoryNode | None, project_id: str, policy: WorkflowPolicy) -> bool:
    if node is None or node.type not in WORKFLOW_PARTICIPANT_TYPES:
        return False
    if node.status not in ACTIVE_STATUSES or node.properties.get("project_id") != project_id:
        return False
    if is_test_context_path(_node_path(node)) or not _is_public_owner(node):
        return False
    short_name = _node_label(node).rsplit(".", 1)[-1]
    return short_name.casefold() not in policy.excluded_names


def _import_matches_workflow(anchor: MemoryNode, target: MemoryNode, config: AnalysisConfig) -> bool:
    """Require extra evidence for references to imports outside the call graph."""
    anchor_tokens = _workflow_concept_tokens(anchor, config.workflow)
    target_tokens = _tokens(_node_label(target).rsplit(".", 1)[-1])
    if anchor_tokens & target_tokens:
        return True
    short_name = _node_label(target).rsplit(".", 1)[-1]
    return target.type in {"Class", "Interface"} and short_name.endswith(
        config.architecture.symbol_suffixes + config.workflow.additional_semantic_suffixes
    )


def _module_hint_matches_path(module_hint: str, path: str) -> bool:
    normalized_hint = module_hint.replace(".", "/").strip("/").casefold()
    normalized_path = path.replace("\\", "/").strip("/").casefold()
    suffix = PurePosixPath(normalized_path).suffix
    if suffix:
        normalized_path = normalized_path[: -len(suffix)]
    normalized_path = normalized_path.removesuffix("/__init__")
    return bool(
        normalized_hint
        and (
            normalized_path == normalized_hint
            or normalized_path.endswith(f"/{normalized_hint}")
        )
    )


def _source_references_name(source_text: str, name: str) -> bool:
    if not source_text or not name:
        return False
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
        source_text,
    ) is not None


def _implementation_body_text(node: MemoryNode, source_text: str) -> str:
    if node.type not in {"Class", "Function", "Method"}:
        return source_text
    lines = source_text.splitlines()
    if not lines:
        return ""
    signature_started = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not signature_started and stripped.startswith(
            ("def ", "async def ", "class ", "@")
        ):
            signature_started = True
        if signature_started and stripped.endswith(":"):
            return "\n".join(lines[index + 1 :])
    return source_text


def _workflow_concept_tokens(node: MemoryNode, policy: WorkflowPolicy) -> set[str]:
    tokens = _tokens(_node_label(node).rsplit(".", 1)[-1]) - set(policy.generic_tokens)
    if tokens:
        return tokens
    return _tokens(f"{_node_label(node)} {_node_path(node)}") - set(policy.generic_tokens)


def _workflow_documentation_evidence(
    documentation_nodes: Sequence[MemoryNode],
    concept_tokens: set[str],
    *,
    tokens_by_id: dict[str, set[str]],
) -> list[CodeEvidence]:
    """Reuse document tokens across workflow candidates within one explanation."""
    if not concept_tokens:
        return []
    required_overlap = 1 if len(concept_tokens) == 1 else 2
    ranked = []
    for node in documentation_nodes:
        tokens = tokens_by_id.get(node.id)
        if tokens is None:
            searchable = " ".join(
                (
                    _node_label(node),
                    str(node.text or ""),
                    str(node.properties.get("section_path") or ""),
                    _node_path(node),
                )
            )
            tokens = tokens_by_id[node.id] = _tokens(searchable)
        overlap = len(concept_tokens & tokens)
        if overlap < required_overlap:
            continue
        score = overlap / max(1, len(concept_tokens))
        ranked.append(
            (
                -score,
                _node_path(node),
                node.id,
                _evidence(
                    node,
                    reason="documentation corroborates workflow intent",
                    score=score,
                ),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return _dedupe_evidence(item[3] for item in ranked)[:3]


def _workflow_docstring_evidence(
    participants: Iterable[MemoryNode],
    concept_tokens: set[str],
) -> list[CodeEvidence]:
    evidence = []
    for node in participants:
        text = str(node.text or "").strip()
        if not text:
            continue
        searchable_tokens = _tokens(f"{_node_label(node)} {text}")
        if concept_tokens and not concept_tokens & searchable_tokens:
            continue
        evidence.append(
            _evidence(
                node,
                reason="docstring corroborates workflow intent",
            )
        )
    return _dedupe_evidence(evidence)


def _workflow_candidates_overlap(
    left: _WorkflowCandidate,
    right: _WorkflowCandidate,
    policy: WorkflowPolicy,
) -> bool:
    left_ids = set(left.members)
    right_ids = set(right.members)
    shared = len(left_ids & right_ids)
    if not shared:
        return False
    containment = shared / min(len(left_ids), len(right_ids))
    if containment >= 0.75:
        return True
    left_tokens = _workflow_concept_tokens(left.anchor, policy)
    right_tokens = _workflow_concept_tokens(right.anchor, policy)
    token_union = left_tokens | right_tokens
    token_overlap = len(left_tokens & right_tokens) / max(1, len(token_union))
    return token_overlap >= 0.5 and shared / max(len(left_ids), len(right_ids)) >= 0.5


def _workflow_role(node: MemoryNode, policy: WorkflowPolicy, *, is_trigger: bool) -> str:
    if is_trigger:
        return "trigger"
    concept = _node_label(node).rsplit(".", 1)[-1].casefold()
    if node.type == "Endpoint":
        return "interface"
    if concept.endswith(policy.infrastructure_suffixes):
        return "infrastructure"
    if concept.endswith(policy.orchestration_suffixes):
        return "orchestration"
    if concept.endswith(policy.policy_suffixes):
        return "policy"
    return "participant"


def _workflow_inputs(anchor: MemoryNode) -> list[str]:
    values = anchor.properties.get("args") or []
    if isinstance(values, str):
        values = [values]
    inputs = []
    for value in values:
        normalized = str(value).strip()
        name = normalized.split(":", 1)[0].split("=", 1)[0].strip().lstrip("*")
        if normalized and name not in {"self", "cls"}:
            inputs.append(normalized)
    return list(dict.fromkeys(inputs))


def _workflow_intent(
    anchor: MemoryNode, participants: Sequence[MemoryNode], policy: ArchitecturePolicy,
) -> str:
    for sentence in _sentences(str(anchor.text or "")):
        if len(_tokens(sentence)) >= 4:
            return sentence
    name = _humanize_symbol(_node_label(anchor))
    collaborators = []
    for participant in participants:
        if participant.id == anchor.id:
            continue
        concept = _owner_concept(_node_label(participant), policy.symbol_suffixes)
        if concept and concept.casefold() not in {item.casefold() for item in collaborators}:
            collaborators.append(concept)
        if len(collaborators) >= 4:
            break
    if collaborators:
        return f"Implements {name.casefold()} through {', '.join(collaborators)}."
    return f"Implements {name.casefold()} from an explicit repository boundary."


def _sentences(text: str) -> list[str]:
    values = []
    for item in re.split(r"(?<=[.!?])\s+|[\r\n]+", text):
        sentence = " ".join(item.split()).strip()
        if 4 <= len(sentence) <= 280:
            values.append(sentence)
    return values


def _evidence(
    node: MemoryNode,
    *,
    reason: str,
    score: float | None = None,
) -> CodeEvidence:
    line_start = _int_or_none(node.properties.get("line_start") or node.properties.get("start_line"))
    line_end = _int_or_none(node.properties.get("line_end") or node.properties.get("end_line"))
    return CodeEvidence(
        node_id=node.id,
        node_type=node.type,
        label=_node_label(node),
        path=_node_path(node),
        line_start=line_start,
        line_end=line_end,
        reason=reason,
        score=score,
    )


def _dedupe_evidence(items: Iterable[CodeEvidence]) -> list[CodeEvidence]:
    result = []
    seen: set[tuple[str, str, int | None]] = set()
    for item in items:
        key = (item.label, item.path, item.line_start)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _owner_rank(node: MemoryNode, degree_by_id: dict[str, int], policy: ArchitecturePolicy) -> tuple[bool, int, str, str]:
    short_name = _node_label(node).rsplit(".", 1)[-1].casefold()
    return (
        short_name in policy.generic_owner_names,
        -degree_by_id.get(node.id, 0),
        _node_label(node).casefold(),
        node.id,
    )


def _tokens(value: str) -> set[str]:
    expanded = CAMEL_RE.sub(" ", value.replace("_", " ").replace("-", " "))
    return {match.group(0).casefold() for match in WORD_RE.finditer(expanded)}


def _humanize_symbol(value: str) -> str:
    short = value.rsplit(".", 1)[-1]
    expanded = CAMEL_RE.sub(" ", short.replace("_", " ").replace("-", " "))
    words = [match.group(0) for match in WORD_RE.finditer(expanded)]
    if not words:
        return short
    return " ".join(words).strip().title()


def _int_or_none(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
