"""Immutable configuration values used by extraction, retrieval and testing."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class LexicalNormalizationPolicy:
    """Validated lexical normalization settings."""

    fold_accents: bool
    split_identifiers: bool
    expand_plurals: bool
    min_token_chars: int
    min_token_signal: float
    phrase_min_signal: float
    phrase_frequency: float
    phrase_specificity: float
    sentence_clause_chars: int


@dataclass(frozen=True, slots=True)
class SeedExtractionPolicy:
    """Validated seed extraction settings."""

    topic_candidates: int
    topic_limit: int
    signature_topics: int
    topic_min_chars: int
    topic_min_signal: float
    entity_limit: int
    entity_confidence: float
    quoted_entities: bool
    capitalized_entities: bool
    structured_entities: bool
    quality_base: float
    quality_per_topic: float
    quality_per_entity: float


@dataclass(frozen=True, slots=True)
class DocumentExtractionPolicy:
    """Validated document extraction settings."""

    max_terms: int
    max_relations: int
    max_events_per_term: int
    frequency_weight: float
    specificity_weight: float
    distribution_weight: float
    phrase_boost: float
    ubiquitous_factor: float
    ubiquitous_min_fragments: int
    bigram_min_signal: float
    trigram_min_signal: float
    bigram_frequency: float
    trigram_frequency: float
    token_min_signal: float
    phrase_min_signal: float
    event_frequency_boost: float
    relation_frequency_weight: float
    relation_term_weight: float
    relation_min_rank: float


@dataclass(frozen=True, slots=True)
class LexicalIndexPolicy:
    """Validated lexical index settings."""

    metadata_weight: float
    token_base_weight: float
    token_signal_weight: float
    token_rarity_weight: float
    salience_bonus: float
    phrase_query_weight: float
    idf_base_weight: float
    idf_weight: float
    compound_coverage: float
    normalized_score_weight: float
    matched_term_bonus: float


@dataclass(frozen=True, slots=True)
class SearchPolicy:
    """Validated search settings."""

    exact_path_score: float
    exact_symbol_score: float
    field_owner_score: float
    field_owner_match_floor: float
    topic_seed_base: float
    topic_seed_weight: float
    entity_seed_base: float
    entity_seed_weight: float
    exact_symbol_match_floor: float
    identifier_min_chars: int
    informative_min_chars: int
    strong_identifier_signal: float
    phrase_min_chars: int
    phrase_word_signal: float
    phrase_full_coverage: float
    phrase_high_coverage: float
    phrase_medium_coverage: float
    high_phrase_min_coverage: float
    medium_phrase_min_coverage: float
    high_token_coverage: float
    medium_token_coverage: float
    identifier_phrase_coverage_floor: float
    text_phrase_coverage_floor: float
    prefix_coverage_floor: float
    strong_coverage_floor: float
    source_min_coverage: float
    weak_overlap_count: int
    weak_overlap_ratio: float
    exact_topic: float
    exact_text: float
    exact_identifier: float
    identifier_phrase: float
    identifier_full_with_phrase: float
    identifier_full: float
    text_phrase: float
    text_prefix: float
    text_full_with_phrase: float
    text_full: float
    phrase_high: float
    phrase_medium: float
    strong_overlap: float
    coverage_high: float
    coverage_medium: float
    coverage_partial_limit: float
    coverage_partial_weight: float
    coverage_phrase_weight: float
    source_coverage_weight: float
    type_bonuses: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class RankingPolicy:
    """Validated ranking settings."""

    min_edge_weight: float
    context_min_edge_weight: float
    unresolved_edge_factor: float
    step_previous_weight: float
    step_coverage_weight: float
    step_edge_weight: float
    path_edge_weight: float
    path_coverage_weight: float
    direct_weight: float
    coverage_weight: float
    path_weight: float
    seed_weight: float
    depth_penalty_max: float
    depth_penalty_per_hop: float
    linked_source_penalty: float
    unlinked_source_penalty: float
    weak_path_min_coverage: float
    weak_match_min_score: float
    weak_query_min_terms: int
    exact_symbol_bonus: float
    exact_owner_bonus: float
    resolved_edge_bonus: float


@dataclass(frozen=True, slots=True)
class SourceEvidencePolicy:
    """Validated source evidence settings."""

    brief_max_source_chars: int = field(metadata={"project_override": True})
    setup_headings: tuple[str, ...] = field(metadata={"project_override": True})
    setup_preferred_paths: tuple[str, ...] = field(metadata={"project_override": True})


@dataclass(frozen=True, slots=True)
class ContextProjectionPolicy:
    """Validated context projection settings."""

    min_confidence: float


@dataclass(frozen=True, slots=True)
class ExploreEvidencePolicy:
    """Validated explore evidence settings."""

    owner_edges: tuple[str, ...]
    caller_edges: tuple[str, ...]
    public_surface_edges: tuple[str, ...]
    serialization_edges: tuple[str, ...]
    owner_nodes: tuple[str, ...]
    public_surface_nodes: tuple[str, ...]
    docs_edges: tuple[str, ...]
    docs_nodes: tuple[str, ...]
    duplicate_min_tags: int
    duplicate_min_features: int
    duplicate_similarity: float


@dataclass(frozen=True, slots=True)
class PathQueryPolicy:
    """Validated path query settings."""

    edge_weight: float
    salience_weight: float


@dataclass(frozen=True, slots=True)
class QueryDefaultsPolicy:
    """Validated query defaults settings."""

    activate_min: float
    explain_hub_columns: tuple[str, ...]
    communities_columns: tuple[str, ...]
    hubs_columns: tuple[str, ...]
    stats_columns: tuple[str, ...]
    cache_columns: tuple[str, ...]
    node_columns: tuple[str, ...]
    edge_columns: tuple[str, ...]
    search_columns: tuple[str, ...]
    retrieve_columns: tuple[str, ...]
    match_columns: tuple[str, ...]
    activate_columns: tuple[str, ...]
    path_columns: tuple[str, ...]
    explain_node_columns: tuple[str, ...]
    explain_search_columns: tuple[str, ...]
    verify_finding_columns: tuple[str, ...]
    projects_columns: tuple[str, ...]
    artifacts_columns: tuple[str, ...]
    fragments_columns: tuple[str, ...]
    symbols_columns: tuple[str, ...]
    findings_columns: tuple[str, ...]
    deltas_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TestDiscoveryPolicy:
    """Regular expressions for project test directories."""

    directories: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TestSuggestionPolicy:
    """Evidence precedence for test associations of equal freshness."""

    evidence_order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TestingConfig:
    """Effective test discovery and suggestion policies."""

    discovery: TestDiscoveryPolicy
    suggestions: TestSuggestionPolicy


@dataclass(frozen=True, slots=True)
class LexicalConfig:
    """Effective lexical policies for a project."""

    normalization: LexicalNormalizationPolicy
    seeds: SeedExtractionPolicy
    documents: DocumentExtractionPolicy
    index: LexicalIndexPolicy


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """Effective retrieval policies for a project."""

    search: SearchPolicy
    ranking: RankingPolicy
    source: SourceEvidencePolicy
    context: ContextProjectionPolicy
    explore: ExploreEvidencePolicy


@dataclass(frozen=True, slots=True)
class QueryConfig:
    """Effective query policies for a project."""

    path: PathQueryPolicy
    defaults: QueryDefaultsPolicy
