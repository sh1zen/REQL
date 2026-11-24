"""Deterministic query/source extraction for the code graph.

The extractor is intentionally small and dependency-free. It provides lexical
topics and named-entity seeds for retrieval without producing conversation
memory candidates such as claims, preferences, observations, or corrections.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config.loader import default_config
from ..config.policies import LexicalNormalizationPolicy, SeedExtractionPolicy
from .normalization import canonicalize, extract_capitalized_entities, keyword_scores, normalize_text, split_sentences


@dataclass(slots=True)
class ExtractedEntityMention:
    text: str
    canonical_key: str
    entity_type: str
    confidence: float
    resolved_key: str
    source_sentence: str
    extractor: str
    start_char: int | None = None
    end_char: int | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResolvedEntity:
    canonical_key: str
    canonical_name: str
    entity_type: str
    confidence: float
    aliases: list[str]
    mention_count: int
    provenance: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ExtractedRelation:
    subject: str
    predicate: str
    object_value: str
    subject_key: str
    object_key: str
    edge_type: str
    confidence: float
    source_sentence: str
    raw_predicate: str
    normalized_predicate: str
    language: str
    extractor: str = "deterministic"
    dynamic_relation: bool = False
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractionResult:
    text: str
    semantic_hash: str
    topics: list[tuple[str, float]]
    entities: list[tuple[str, str, float]]
    candidates: list[Any] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)
    quality: float = 0.0
    relations: list[ExtractedRelation] = field(default_factory=list)
    entity_mentions: list[ExtractedEntityMention] = field(default_factory=list)
    resolved_entities: list[ResolvedEntity] = field(default_factory=list)
    language: str | None = None
    signature: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


_DEFAULT_LEXICAL = default_config().lexical


class DeterministicExtractor:
    """Rule-based extractor used for query seed discovery."""

    def __init__(self, *, policy: SeedExtractionPolicy = _DEFAULT_LEXICAL.seeds,
                 normalization: LexicalNormalizationPolicy = _DEFAULT_LEXICAL.normalization) -> None:
        self.policy = policy
        self.normalization = normalization

    def extract(self, text: str) -> ExtractionResult:
        sentences = split_sentences(text, policy=self.normalization)
        text = normalize_text(text)
        semantic_hash = canonicalize(text, fold_accents=self.normalization.fold_accents)
        topics = self._extract_topics(text)
        entities = self._extract_entities(text)
        signature = {
            "normalized_tokens": [topic for topic, _ in topics],
            "main_topics": [topic for topic, _ in topics[:self.policy.signature_topics]],
            "entity_canonical_keys": sorted({key for entity, _, _ in entities
                                             if (key := canonicalize(entity, fold_accents=self.normalization.fold_accents))}),
            "relation_predicates": [],
        }
        quality = min(1.0, self.policy.quality_base + self.policy.quality_per_topic * len(topics)
                      + self.policy.quality_per_entity * len(entities))
        return ExtractionResult(
            text=text,
            semantic_hash=semantic_hash,
            topics=topics,
            entities=entities,
            candidates=[],
            sentences=sentences,
            quality=quality,
            signature=signature,
        )

    def _extract_topics(self, text: str) -> list[tuple[str, float]]:
        return [
            (term, score)
            for term, score in keyword_scores(text, max_terms=self.policy.topic_candidates, policy=self.normalization)
            if len(term) >= self.policy.topic_min_chars and (" " in term or score >= self.policy.topic_min_signal)
        ][:self.policy.topic_limit]

    def _extract_entities(self, text: str) -> list[tuple[str, str, float]]:
        """Use lexical mentions as seeds without inferring their semantic type."""
        entities = extract_capitalized_entities(text, policy=self.policy, normalization=self.normalization)
        return [(entity, "named_thing", self.policy.entity_confidence)
                for entity in entities[:self.policy.entity_limit]]
