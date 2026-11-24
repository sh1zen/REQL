"""Configuration domain objects for REQL."""
from __future__ import annotations

from dataclasses import MISSING, Field, dataclass, field, fields, is_dataclass
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Protocol, get_args, get_origin, get_type_hints

from ..domain import query_context as context_contract
from .policies import LexicalConfig, QueryConfig, RetrievalConfig, TestingConfig
from .path_rules import normalize_scan_exclude_pattern, resolve_scan_exclude_pattern


def normalize_scan_path_pattern(pattern: str) -> str:
    """Return the canonical matching key for a scan include pattern.

    Exclusions use :func:`resolve_scan_exclude_pattern` so their ``./`` anchor
    remains significant.
    """

    normalized = pattern.strip().replace("\\", "/")
    previous = None
    while normalized != previous:
        previous = normalized
        normalized = normalized.lstrip("/")
        normalized = normalized.removeprefix("./")
    return normalized.rstrip("/")


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    id: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id}


@dataclass(frozen=True, slots=True)
class ScanConfig:
    max_file_size_mb: float
    ignore_defaults: bool
    include: list[str]
    exclude: list[str]

    @property
    def max_file_size_bytes(self) -> int | None:
        return max(1, int(self.max_file_size_mb * 1024 * 1024)) if self.max_file_size_mb else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_file_size_mb": self.max_file_size_mb,
            "ignore_defaults": self.ignore_defaults,
            "include": list(self.include),
            "exclude": list(self.exclude),
        }


@dataclass(frozen=True, slots=True)
class CompileConfig:
    ingest_documents: bool
    documents: dict[str, bool]
    document_formats: dict[str, dict[str, list[str]]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ingest_documents": self.ingest_documents,
            "documents": dict(self.documents),
            "document_formats": {
                format_name: {key: list(values) for key, values in definition.items()}
                for format_name, definition in self.document_formats.items()
            },
        }


@dataclass(frozen=True, slots=True)
class CacheConfig:
    enabled: bool

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled}


@dataclass(frozen=True, slots=True)
class ActivationPolicy:
    """Validated settings for activation."""

    blocked_edge_types: tuple[str, ...]
    negative_edge_types: tuple[str, ...]
    min_edge_weight: float
    latent_factor: float
    negative_factor: float


@dataclass(frozen=True, slots=True)
class SaliencePolicy:
    """Validated settings for salience."""

    half_life_days: float
    type_priors: Mapping[str, float]
    default_prior: float
    novelty_weight: float
    recency_weight: float
    centrality_weight: float
    code_priority_weight: float
    evidence_weight: float
    confidence_weight: float
    degree_saturation: float
    priority_saturation: float
    evidence_saturation: float
    inactive_factor: float
    signal_weight: float
    volatility_weight: float


@dataclass(frozen=True, slots=True)
class HubPolicy:
    """Validated settings for hub."""

    min_score: float
    max_generic_penalty: float
    excluded_node_types: tuple[str, ...]
    warning_generic_penalty: float
    warning_centrality: float
    degree_weight: float
    weighted_degree_weight: float
    activation_weight: float
    retrieval_weight: float
    salience_weight: float
    community_weight: float
    specificity_weight: float
    confidence_weight: float
    generic_penalty_weight: float


@dataclass(frozen=True, slots=True)
class BridgePolicy:
    """Validated settings for bridge."""

    min_score: float
    max_generic_penalty: float
    min_communities: int
    excluded_node_types: tuple[str, ...]
    ignored_edge_types: tuple[str, ...]
    community_saturation: float
    incident_saturation: float
    novelty_community_saturation: float
    centrality_community_weight: float
    centrality_incident_weight: float
    novelty_distance_weight: float
    novelty_community_weight: float
    utility_salience_weight: float
    utility_specificity_weight: float
    utility_evidence_weight: float
    randomness_min_evidence: int
    randomness_min_strength: float
    randomness_min_specificity: float
    randomness_min_distance: float
    randomness_count_penalty: float
    randomness_strength_penalty: float
    randomness_specificity_penalty: float
    randomness_distance_penalty: float
    distance_weight: float
    novelty_weight: float
    utility_weight: float
    evidence_weight: float
    centrality_weight: float
    specificity_weight: float
    randomness_weight: float
    generic_penalty_weight: float


@dataclass(frozen=True, slots=True)
class CentralityPolicy:
    """Validated settings for centrality."""

    activation_saturation: float
    community_saturation: float
    excluded_node_types: tuple[str, ...]
    ignored_edge_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SpecificityPolicy:
    """Validated settings for specificity."""

    generic_types: tuple[str, ...]
    specific_types: tuple[str, ...]
    generic_artifact_types: tuple[str, ...]
    specific_artifact_types: tuple[str, ...]
    base_score: float
    concise_min_tokens: int
    concise_max_tokens: int
    overlong_min_tokens: int
    single_min_chars: int
    weak_max_chars: int
    topical_min_chars: int
    concise_bonus: float
    overlong_penalty: float
    single_bonus: float
    weak_token_weight: float
    weak_ratio_threshold: float
    topical_bonus: float
    specific_type_bonus: float
    generic_type_penalty: float
    external_symbol_penalty: float
    entropy_threshold: float
    entropy_penalty: float
    focused_bonus: float
    high_degree_threshold: int
    low_focus_threshold: float
    max_degree_penalty: float
    degree_saturation: float
    generic_artifact_penalty: float
    specific_artifact_bonus: float
    max_usage_bonus: float
    usage_saturation: float


@dataclass(frozen=True, slots=True)
class CommunityPolicy:
    """Validated settings for community."""

    min_size: int
    excluded_node_types: tuple[str, ...]
    ignored_edge_types: tuple[str, ...]
    bridge_weight: float
    bridge_confidence: float


@dataclass(frozen=True, slots=True)
class ArchitecturePolicy:
    """Validated settings for architecture."""

    source_roots: tuple[str, ...] = field(metadata={"project_override": True})
    generic_capability_names: tuple[str, ...] = field(metadata={"project_override": True})
    generic_owner_names: tuple[str, ...] = field(metadata={"project_override": True})
    generic_module_names: tuple[str, ...] = field(metadata={"project_override": True})
    symbol_suffixes: tuple[str, ...] = field(metadata={"project_override": True})
    layer_precedence: tuple[str, ...] = field(metadata={"project_override": True})
    fallback_layer: str = field(metadata={"project_override": True})
    application_tokens: tuple[str, ...] = field(metadata={"project_override": True})
    domain_tokens: tuple[str, ...] = field(metadata={"project_override": True})
    infrastructure_tokens: tuple[str, ...] = field(metadata={"project_override": True})
    core_tokens: tuple[str, ...] = field(metadata={"project_override": True})
    dominant_root_min_paths: int = field(metadata={"project_override": True})
    dominant_root_ratio: float = field(metadata={"project_override": True})


@dataclass(frozen=True, slots=True)
class WorkflowPolicy:
    """Validated settings for workflow."""

    entry_names: tuple[str, ...] = field(metadata={"project_override": True})
    entry_roles: tuple[str, ...] = field(metadata={"project_override": True})
    interface_path_tokens: tuple[str, ...] = field(metadata={"project_override": True})
    excluded_names: tuple[str, ...] = field(metadata={"project_override": True})
    infer_roots: bool = field(metadata={"project_override": True})
    generic_tokens: tuple[str, ...] = field(metadata={"project_override": True})
    additional_semantic_suffixes: tuple[str, ...] = field(metadata={"project_override": True})
    infrastructure_suffixes: tuple[str, ...] = field(metadata={"project_override": True})
    orchestration_suffixes: tuple[str, ...] = field(metadata={"project_override": True})
    policy_suffixes: tuple[str, ...] = field(metadata={"project_override": True})


@dataclass(frozen=True, slots=True)
class MarkdownReportPolicy:
    """Set Markdown presentation limits."""

    section_limit: int
    delta_limit: int
    cache_limit: int
    analysis_limit: int


@dataclass(frozen=True, slots=True)
class HTMLReportPolicy:
    """Set HTML search, graph detail, and payload limits."""

    graph_search_limit: int
    graph_detail_edge_limit: int
    graph_menu_edge_limit: int
    graph_label_chars: int
    graph_text_chars: int
    graph_property_chars: int
    graph_property_items: int
    pipeline_search_limit: int


def _builtin_policy(section: str, name: str | None = None) -> Any:
    """Support direct construction of the original public section models."""
    from .loader import default_config

    policy = getattr(default_config(), section)
    return getattr(policy, name) if name is not None else policy


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    enable_hubs: bool = field(metadata={"project_override": True})
    enable_communities: bool = field(metadata={"project_override": True})
    activation: ActivationPolicy = field(default_factory=lambda: _builtin_policy("analysis", "activation"))
    salience: SaliencePolicy = field(default_factory=lambda: _builtin_policy("analysis", "salience"))
    hubs: HubPolicy = field(default_factory=lambda: _builtin_policy("analysis", "hubs"))
    bridges: BridgePolicy = field(default_factory=lambda: _builtin_policy("analysis", "bridges"))
    centrality: CentralityPolicy = field(default_factory=lambda: _builtin_policy("analysis", "centrality"))
    specificity: SpecificityPolicy = field(default_factory=lambda: _builtin_policy("analysis", "specificity"))
    communities: CommunityPolicy = field(default_factory=lambda: _builtin_policy("analysis", "communities"))
    architecture: ArchitecturePolicy = field(default_factory=lambda: _builtin_policy("analysis", "architecture"))
    workflow: WorkflowPolicy = field(default_factory=lambda: _builtin_policy("analysis", "workflow"))

    def to_dict(self) -> dict[str, Any]:
        return _policy_mapping(self)


@dataclass(frozen=True, slots=True)
class ReportingConfig:
    output_dir: str
    markdown: MarkdownReportPolicy = field(default_factory=lambda: _builtin_policy("reporting", "markdown"))
    html: HTMLReportPolicy = field(default_factory=lambda: _builtin_policy("reporting", "html"))

    def to_dict(self) -> dict[str, Any]:
        return _policy_mapping(self)


class _PolicyModel(Protocol):
    """Dataclass fields needed to serialize a configuration policy."""

    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]


def _policy_mapping(policy: _PolicyModel) -> dict[str, Any]:
    """Serialize nested policies with JSON-compatible lists and mappings."""
    result = {}
    for option in fields(policy):
        value = getattr(policy, option.name)
        if is_dataclass(value):
            value = _policy_mapping(value)
        elif isinstance(value, tuple):
            value = list(value)
        elif isinstance(value, Mapping):
            value = {key: list(item) if isinstance(item, tuple) else item for key, item in value.items()}
        result[option.name] = value
    return result


@dataclass(frozen=True, slots=True)
class DiagnosticsConfig:
    enabled: bool
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "path": self.path}


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    """Count-based retention for project commits and completed agent sessions."""

    commits: int
    agent_sessions: int

    def to_dict(self) -> dict[str, Any]:
        return {"commits": self.commits, "agent_sessions": self.agent_sessions}


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """Page layout, cache and checkpoint policy for the block adapter."""

    block_size_bytes: int
    page_cache_blocks: int
    dense_node_threshold: int
    checkpoint_wal_bytes: int


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """Default workspace identity and store paths."""

    default_agent_id: str
    private_store_name: str
    dashboard_store_name: str
    agents_dir: str

    def dashboard_path(self, storage: str | Path) -> Path:
        """Locate the shared dashboard next to the canonical graph."""
        path = Path(storage)
        if path.name.casefold() == self.dashboard_store_name.casefold():
            raise ValueError("Project and public dashboard stores must use distinct paths")
        return path.with_name(self.dashboard_store_name)


@dataclass(frozen=True, slots=True)
class MCPConfig:
    """Listener settings and resource limits for MCP tools."""

    host: str
    port: int
    max_top_k: int
    max_depth: int
    max_items: int
    max_query_rows: int
    max_nodes: int
    max_edges: int
    max_text_chars: int

    def to_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class REQLConfig:
    project: ProjectConfig
    scan: ScanConfig
    compile: CompileConfig
    cache: CacheConfig
    analysis: AnalysisConfig
    reporting: ReportingConfig
    diagnostics: DiagnosticsConfig
    retention: RetentionConfig
    mcp: MCPConfig
    storage: StorageConfig
    workspace: WorkspaceConfig
    lexical: LexicalConfig = field(default_factory=lambda: _builtin_policy("lexical"))
    retrieval: RetrievalConfig = field(default_factory=lambda: _builtin_policy("retrieval"))
    query: QueryConfig = field(default_factory=lambda: _builtin_policy("query"))
    testing: TestingConfig = field(default_factory=lambda: _builtin_policy("testing"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "lexical": _policy_mapping(self.lexical),
            "retrieval": _policy_mapping(self.retrieval),
            "query": _policy_mapping(self.query),
            "testing": _policy_mapping(self.testing),
            "project": self.project.to_dict(),
            "scan": self.scan.to_dict(),
            "compile": self.compile.to_dict(),
            "cache": self.cache.to_dict(),
            "analysis": self.analysis.to_dict(),
            "reporting": self.reporting.to_dict(),
            "diagnostics": self.diagnostics.to_dict(),
            "retention": self.retention.to_dict(),
            "mcp": self.mcp.to_dict(),
            "storage": {field.name: getattr(self.storage, field.name) for field in fields(StorageConfig)},
            "workspace": {field.name: getattr(self.workspace, field.name) for field in fields(WorkspaceConfig)},
        }


SECTION_TYPES = {
    "lexical": LexicalConfig,
    "retrieval": RetrievalConfig,
    "query": QueryConfig,
    "testing": TestingConfig,
    "project": ProjectConfig,
    "scan": ScanConfig,
    "compile": CompileConfig,
    "cache": CacheConfig,
    "analysis": AnalysisConfig,
    "reporting": ReportingConfig,
    "diagnostics": DiagnosticsConfig,
    "retention": RetentionConfig,
    "mcp": MCPConfig,
    "storage": StorageConfig,
    "workspace": WorkspaceConfig,
}

def merge_config(config: REQLConfig, overrides: Mapping[str, Any], *, project_only: bool = False) -> REQLConfig:
    """Merge config values, using the bundled user defaults for project overrides."""
    project_defaults = None
    if project_only:
        from .loader import _load_defaults

        _, project_defaults = _load_defaults()
    return _merge_config(config, overrides, project_defaults)


def _merge_config(
    config: REQLConfig, overrides: Mapping[str, Any], project_defaults: Mapping[str, Any] | None,
) -> REQLConfig:
    """Normalize and apply overrides with the project policy from this config load."""
    nested: dict[str, dict[str, Any]] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        if "." in key:
            section, option_path = key.split(".", 1)
            target = nested.setdefault(section, {})
            option_parts = option_path.split(".")
            for part in option_parts[:-1]:
                child = target.get(part, {})
                if not isinstance(child, Mapping):
                    raise ValueError(f"Config override conflicts with a scalar option: {key}")
                # Copy only the traversed branch; nested overrides belong to callers.
                target[part] = dict(child)
                target = target[part]
            target[option_parts[-1]] = value
        elif isinstance(value, Mapping):
            target = nested.setdefault(key, {})
            if key in {"analysis", "reporting", "lexical", "retrieval", "query", "testing"}:
                _merge_policy_mapping(target, value)
            else:
                target.update(dict(value))
        else:
            raise ValueError(f"Config override must be dotted or nested: {key}")

    current = config.to_dict()
    for section, values in nested.items():
        section_type = SECTION_TYPES.get(section)
        if section_type is None:
            raise ValueError(f"Unknown config section: {section}")
        allowed = None
        if project_defaults is not None:
            allowed = project_defaults.get(section, {})
            _validate_project_fields(section_type, values, allowed, section)
        scan_ignore_defaults = False
        if section == "scan":
            scan_ignore_defaults = bool(values.get("ignore_defaults", current[section].get("ignore_defaults", False)))
            if scan_ignore_defaults:
                for option in ("include", "exclude"):
                    if allowed is None or option in allowed:
                        current[section][option] = []
        for option, value in values.items():
            if option not in current[section]:
                raise ValueError(f"Unknown config option: {section}.{option}")
            current_value = current[section][option]
            if section in {"analysis", "reporting", "lexical", "retrieval", "query", "testing"} and isinstance(current_value, dict) and isinstance(value, Mapping):
                _merge_policy_mapping(current_value, value)
            elif section == "compile" and option == "documents" and isinstance(value, Mapping):
                merged_mapping = dict(current_value) if isinstance(current_value, Mapping) else {}
                merged_mapping.update(_coerce_document_toggles(value))
                current[section][option] = merged_mapping
            elif section == "compile" and option == "document_formats" and isinstance(value, Mapping):
                merged_mapping = dict(current_value) if isinstance(current_value, Mapping) else {}
                merged_mapping.update(_coerce_document_formats(value))
                current[section][option] = merged_mapping
            elif isinstance(current_value, list) and isinstance(value, list):
                if section == "scan" and option in {"include", "exclude"} and scan_ignore_defaults:
                    current[section][option] = list(value)
                elif section == "scan" and option == "exclude":
                    current[section][option] = _join_scan_exclude_lists(current_value, value)
                else:
                    current[section][option] = _join_config_lists(current_value, value)
            else:
                current[section][option] = value
    return config_from_mapping(current)


def _validate_project_fields(
    section_type: type[Any], overrides: Mapping[str, Any], allowed: Mapping[str, Any], prefix: str,
) -> None:
    """Validate option membership while leaving types and unknown keys to the schema."""
    hints = get_type_hints(section_type)
    project_fields = {item.name for item in fields(section_type) if item.metadata.get("project_override")}
    for key, value in overrides.items():
        option_type = hints.get(key)
        if option_type is None:
            continue
        option = f"{prefix}.{key}"
        if is_dataclass(option_type) and isinstance(value, Mapping):
            _validate_project_fields(option_type, value, allowed.get(key, {}), option)
        elif key not in allowed and key not in project_fields:
            raise ValueError(
                f"Config option {option} is set in core.yaml. "
                "Use options in user.yaml or settings declared for the project."
            )


def _merge_policy_mapping(target: dict[str, Any], changes: Mapping[str, Any]) -> None:
    """Merge partial policies by field, replacing ordered lists as supplied."""
    for key, value in changes.items():
        if isinstance(value, Mapping):
            current = target.get(key)
            child = dict(current) if isinstance(current, Mapping) else {}
            _merge_policy_mapping(child, value)
            target[key] = child
        else:
            target[key] = value


def _join_config_lists(current: list[Any], additions: list[Any]) -> list[Any]:
    joined = list(current)
    for item in additions:
        if item not in joined:
            joined.append(item)
    return joined


def _join_scan_exclude_lists(current: list[Any], additions: list[Any]) -> list[Any]:
    joined: list[Any] = []
    seen: set[str] = set()
    for item in [*current, *additions]:
        key = normalize_scan_exclude_pattern(item)
        if key not in seen:
            joined.append(item)
            seen.add(key)
    return joined


def config_from_mapping(data: Mapping[str, Any], *, require_complete: bool = False) -> REQLConfig:
    """Build a validated config, requiring explicit policy values for bundled defaults."""

    allowed_sections = set(SECTION_TYPES)
    unknown_sections = set(data) - allowed_sections
    if unknown_sections:
        raise ValueError(f"Unknown config section(s): {', '.join(sorted(unknown_sections))}")
    if require_complete:
        missing_sections = allowed_sections - data.keys()
        if missing_sections:
            raise ValueError(f"Missing built-in config section(s): {', '.join(sorted(missing_sections))}")

    cfg = REQLConfig(
        lexical=_section(LexicalConfig, data["lexical"], "lexical") if "lexical" in data else _builtin_policy("lexical"),
        retrieval=_section(RetrievalConfig, data["retrieval"], "retrieval") if "retrieval" in data else _builtin_policy("retrieval"),
        query=_section(QueryConfig, data["query"], "query") if "query" in data else _builtin_policy("query"),
        testing=_section(TestingConfig, data["testing"], "testing") if "testing" in data else _builtin_policy("testing"),
        project=_section(ProjectConfig, data.get("project", {}), "project"),
        scan=_section(ScanConfig, data.get("scan", {}), "scan"),
        compile=_section(CompileConfig, data.get("compile", {}), "compile"),
        cache=_section(CacheConfig, data.get("cache", {}), "cache"),
        analysis=_section(AnalysisConfig, data.get("analysis", {}), "analysis", require_complete=require_complete),
        reporting=_section(ReportingConfig, data.get("reporting", {}), "reporting", require_complete=require_complete),
        diagnostics=_section(DiagnosticsConfig, data.get("diagnostics", {}), "diagnostics"),
        retention=_section(RetentionConfig, data.get("retention", {}), "retention"),
        mcp=_section(MCPConfig, data.get("mcp", {}), "mcp"),
        storage=_section(StorageConfig, data.get("storage", {}), "storage"),
        workspace=_section(WorkspaceConfig, data.get("workspace", {}), "workspace"),
    )
    _validate(cfg)
    return cfg


def _section(cls: type[Any], raw: object, section_name: str, *, require_complete: bool = False) -> Any:
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"Config section [{section_name}] must be a table")
    hints = get_type_hints(cls)
    option_names = {field.name for field in fields(cls)}
    raw_values = dict(raw)
    unknown = set(raw_values) - option_names
    if unknown:
        raise ValueError(f"Unknown config option(s) in [{section_name}]: {', '.join(sorted(unknown))}")
    missing = {
        field.name for field in fields(cls)
        if require_complete or (field.default is MISSING and field.default_factory is MISSING)
    } - raw_values.keys()
    if missing:
        raise ValueError(f"Missing config option(s) in [{section_name}]: {', '.join(sorted(missing))}")
    values = {
        key: _coerce_value(section_name, key, value, hints[key])
        for key, value in raw_values.items()
    }
    return cls(**values)

def _coerce_value(section: str, key: str, value: Any, expected_type: Any) -> Any:
    path = f"{section}.{key}"
    if is_dataclass(expected_type):
        if not isinstance(value, Mapping):
            raise ValueError(f"Config option {path} must be a mapping")
        return _section(expected_type, value, path)
    if get_origin(expected_type) is tuple and get_args(expected_type) == (str, Ellipsis):
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) and item and item == item.strip() for item in value
        ):
            raise ValueError(f"Config option {path} must be a list of non-empty strings")
        return tuple(value)
    if expected_type == Mapping[str, tuple[str, ...]]:
        if not isinstance(value, Mapping) or not all(isinstance(name, str) and name.strip() for name in value):
            raise ValueError(f"Config option {path} must map names to lists of strings")
        return MappingProxyType({name: _coerce_value(path, name, items, tuple[str, ...]) for name, items in value.items()})
    if expected_type == Mapping[str, float]:
        if not isinstance(value, Mapping) or not all(isinstance(name, str) and name.strip() for name in value):
            raise ValueError(f"Config option {path} must map node types to scores")
        priors = {name: _coerce_value(path, name, score, float) for name, score in value.items()}
        for name, score in priors.items():
            if not 0 <= score <= 1:
                raise ValueError(f"Config option {path}.{name} must be between zero and one")
        return MappingProxyType(priors)
    if expected_type is bool:
        if not isinstance(value, bool):
            raise ValueError(f"Config option {section}.{key} must be a boolean")
        return value
    if expected_type is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"Config option {section}.{key} must be a number")
        try:
            return float(value)
        except OverflowError as exc:
            raise ValueError(f"Config option {section}.{key} must be a finite number") from exc
    if expected_type is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"Config option {section}.{key} must be an integer")
        return value
    if expected_type is str:
        if not isinstance(value, str):
            raise ValueError(f"Config option {section}.{key} must be a string")
        return value
    if get_origin(expected_type) is list and get_args(expected_type) == (str,):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Config option {section}.{key} must be a list of strings")
        return list(value)
    if section == "compile" and key == "documents":
        return _coerce_document_toggles(value)
    if section == "compile" and key == "document_formats":
        return _coerce_document_formats(value)
    return value


def _coerce_document_toggles(value: Any) -> dict[str, bool]:
    if not isinstance(value, Mapping):
        raise ValueError("Config option compile.documents must be a format-to-boolean mapping")
    toggles: dict[str, bool] = {}
    for raw_format, enabled in value.items():
        format_name = str(raw_format).strip().casefold()
        if not format_name:
            raise ValueError("Config option compile.documents format names must not be empty")
        if not isinstance(enabled, bool):
            raise ValueError(f"Config option compile.documents.{format_name} must be a boolean")
        toggles[format_name] = enabled
    return toggles


def _coerce_document_formats(value: Any) -> dict[str, dict[str, list[str]]]:
    if not isinstance(value, Mapping):
        raise ValueError("Config option compile.document_formats must be a mapping")
    formats: dict[str, dict[str, list[str]]] = {}
    for raw_format, item in value.items():
        format_name = str(raw_format).strip().casefold()
        if not format_name:
            raise ValueError("Config option compile.document_formats format names must not be empty")
        if not isinstance(item, Mapping):
            raise ValueError(f"Config option compile.document_formats.{format_name} must be an object")
        unknown = set(item) - {"extensions", "filenames"}
        if unknown:
            raise ValueError(
                f"Unknown compile.document_formats.{format_name} option(s): {', '.join(sorted(unknown))}"
            )
        extensions = item.get("extensions", [])
        filenames = item.get("filenames", [])
        if not isinstance(extensions, list) or not all(isinstance(value, str) for value in extensions):
            raise ValueError(f"Config option compile.document_formats.{format_name}.extensions must be a list of strings")
        if not isinstance(filenames, list) or not all(isinstance(value, str) for value in filenames):
            raise ValueError(f"Config option compile.document_formats.{format_name}.filenames must be a list of strings")
        normalized_extensions = []
        for extension in extensions:
            normalized = extension.strip().casefold()
            if not normalized.startswith("."):
                raise ValueError(
                    f"Config option compile.document_formats.{format_name}.extensions values must start with '.'"
                )
            normalized_extensions.append(normalized)
        normalized_filenames = [filename.strip() for filename in filenames if filename.strip()]
        if not normalized_extensions and not normalized_filenames:
            raise ValueError(
                f"Config option compile.document_formats.{format_name} must define extensions or filenames"
            )
        formats[format_name] = {
            "extensions": normalized_extensions,
            "filenames": normalized_filenames,
        }
    return formats


def _validate_testing(config: TestingConfig) -> None:
    """Validate directory expressions and the complete evidence order."""
    for pattern in config.discovery.directories:
        try:
            re.compile(pattern, re.IGNORECASE)
        except (re.error, OverflowError, RecursionError) as exc:
            raise ValueError(f"Invalid regex in testing.discovery.directories: {pattern!r}: {exc}") from exc
    order = config.suggestions.evidence_order
    if len(order) != 3 or set(order) != {"observed", "static", "candidate"}:
        raise ValueError("Config option testing.suggestions.evidence_order must list each evidence kind once")


def _validate(config: REQLConfig) -> None:
    _validate_testing(config.testing)
    if not config.project.id.strip():
        raise ValueError("Config option project.id must not be empty")
    if not math.isfinite(config.scan.max_file_size_mb * (1024 * 1024)) or config.scan.max_file_size_mb < 0:
        raise ValueError("Config option scan.max_file_size_mb must be finite and non-negative")
    for pattern in config.scan.exclude:
        resolve_scan_exclude_pattern(pattern)
    unknown_document_formats = set(config.compile.documents) - set(config.compile.document_formats)
    if unknown_document_formats:
        raise ValueError(
            "Unknown compile.documents format(s): " + ", ".join(sorted(unknown_document_formats))
        )
    if config.diagnostics.enabled and not config.diagnostics.path.strip():
        raise ValueError("Config option diagnostics.path must not be empty when diagnostics.enabled is true")
    if config.retention.commits < 1:
        raise ValueError("Config option retention.commits must be greater than zero")
    if config.retention.agent_sessions < 0:
        raise ValueError("Config option retention.agent_sessions must be zero or greater")
    _validate_mcp(config.mcp)
    _validate_operational_policy(config)
    _validate_graph_policies(config)
    _validate_search_policies(config)


def _validate_search_policies(config: REQLConfig) -> None:
    """Validate bounded work, scoring and cross-field constraints at load time."""
    for section in ("lexical", "retrieval", "query"):
        for name, settings in _policy_mapping(getattr(config, section)).items():
            for key, value in settings.items():
                path = f"{section}.{name}.{key}"
                minimum = 0 if key in {
                    "entity_limit",
                    "max_terms", "max_relations", "max_events_per_term", "weak_overlap_count",
                    "brief_max_source_chars",
                } else 1
                if key == "min_token_chars":
                    minimum = 2
                if type(value) is int and value < minimum:
                    raise ValueError(f"Config option {path} must be at least {minimum}")
                if isinstance(value, float) and (not math.isfinite(value) or value < 0):
                    raise ValueError(f"Config option {path} must be finite and non-negative")
                if key.endswith("_columns") and (not value or len(value) != len(set(value))):
                    raise ValueError(f"Config option {path} must contain unique columns")
    for path, value in (
        ("lexical.seeds.entity_confidence", config.lexical.seeds.entity_confidence),
        ("retrieval.search.exact_path_score", config.retrieval.search.exact_path_score),
        ("retrieval.search.exact_symbol_score", config.retrieval.search.exact_symbol_score),
        ("retrieval.context.min_confidence", config.retrieval.context.min_confidence),
        ("retrieval.explore.duplicate_similarity", config.retrieval.explore.duplicate_similarity),
    ):
        if value > 1:
            raise ValueError(f"Config option {path} must be between zero and one")
    source = config.retrieval.source
    for path in source.setup_preferred_paths:
        relative = PurePosixPath(path)
        if (not relative.parts or relative.is_absolute() or PureWindowsPath(path).drive
                or ".." in relative.parts or "\\" in path or path != relative.as_posix()):
            raise ValueError("Config option retrieval.source.setup_preferred_paths must use project-relative POSIX paths")


def _validate_operational_policy(config: REQLConfig) -> None:
    """Validate configurable resource policies without exposing disk codec identifiers."""
    storage, workspace = config.storage, config.workspace
    if not 4096 <= storage.block_size_bytes <= 0xFFFFFFFF:
        raise ValueError("Config option storage.block_size_bytes must fit the block header and be at least 4096")
    for key, minimum in {"page_cache_blocks": 1, "dense_node_threshold": 1, "checkpoint_wal_bytes": 0}.items():
        if getattr(storage, key) < minimum:
            raise ValueError(f"Config option storage.{key} must be at least {minimum}")
    if not workspace.default_agent_id.strip() or workspace.default_agent_id != workspace.default_agent_id.strip():
        raise ValueError("Config option workspace.default_agent_id must be a non-empty, trimmed identifier")
    names = (workspace.private_store_name, workspace.dashboard_store_name, workspace.agents_dir)
    for key, value in zip(("private_store_name", "dashboard_store_name", "agents_dir"), names):
        if (value in {"", ".", ".."} or value.rstrip(". ") != value
                or any(char in value for char in '<>:"/\\|?*')
                or any(ord(char) < 32 for char in value)
                or re.fullmatch(r"(?:con|prn|aux|nul|conin\$|conout\$|(?:com|lpt)[1-9\u00b9\u00b2\u00b3])",
                                value.partition(".")[0].rstrip().casefold())):
            raise ValueError(f"Config option workspace.{key} must be a single filesystem name")
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("Workspace store and directory names must be distinct")


def _validate_graph_policies(config: REQLConfig) -> None:
    """Check scoring ranges, budgets, and cross-field relationships."""
    positive = {
        "min_communities", "min_size", "half_life_days", "graph_label_chars",
        "graph_text_chars", "graph_property_chars", "analysis_limit",
    }
    for section, container in (("analysis", config.analysis), ("reporting", config.reporting)):
        for option in fields(container):
            policy = getattr(container, option.name)
            if not is_dataclass(policy):
                continue
            for item in fields(policy):
                value = getattr(policy, item.name)
                key = f"{section}.{option.name}.{item.name}"
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    minimum = 1 if item.name in positive else 0
                    if item.name in {"min_communities", "graph_label_chars", "graph_text_chars", "graph_property_chars"}:
                        minimum = 2 if item.name == "min_communities" else 4
                    if value < minimum:
                        raise ValueError(f"Config option {key} must be at least {minimum}")
                elif isinstance(value, float):
                    if item.name in positive or item.name.endswith("_saturation"):
                        valid = math.isfinite(value) and value > 0
                    elif item.name == "entropy_threshold":
                        valid = math.isfinite(value) and value >= 0
                    else:
                        valid = 0 <= value <= 1
                    if not valid:
                        raise ValueError(f"Config option {key} is outside its valid numeric range")

    architecture = config.analysis.architecture
    layers = {"interface", "application", "domain", "infrastructure", "core"}
    if (
        set(architecture.layer_precedence) - layers
        or len(set(architecture.layer_precedence)) != len(architecture.layer_precedence)
        or architecture.fallback_layer not in layers
    ):
        raise ValueError("Config options analysis.architecture.layer_precedence and fallback_layer must use known layers without duplicates")
    specificity = config.analysis.specificity
    if not specificity.concise_min_tokens <= specificity.concise_max_tokens < specificity.overlong_min_tokens:
        raise ValueError("Config option analysis.specificity concise and overlong token ranges must be ordered")


def _validate_mcp(config: MCPConfig) -> None:
    """Reject invalid listener settings and limits outside the retrieval contract."""
    if not config.host or any(char.isspace() or ord(char) < 32 for char in config.host):
        raise ValueError("Config option mcp.host must be a non-empty hostname or IP address")
    for key, minimum, maximum in (
        ("port", 0, 65535),
        ("max_top_k", context_contract.MIN_TOP_K, context_contract.MAX_TOP_K),
        ("max_depth", context_contract.MIN_DEPTH, context_contract.MAX_DEPTH),
        ("max_items", context_contract.MIN_ITEMS, context_contract.MAX_ITEMS),
    ):
        if not minimum <= getattr(config, key) <= maximum:
            raise ValueError(f"Config option mcp.{key} must be between {minimum} and {maximum}")
    for key, minimum in (("max_query_rows", 0), ("max_nodes", 1), ("max_edges", 0), ("max_text_chars", 80)):
        if getattr(config, key) < minimum:
            raise ValueError(f"Config option mcp.{key} must be at least {minimum}")
