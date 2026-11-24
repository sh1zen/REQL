"""Compute lexical weights from complete node text and searchable metadata."""
from __future__ import annotations

from collections import Counter
from itertools import chain
from typing import Sequence

from ...config.policies import LexicalConfig, LexicalIndexPolicy
from ...domain.models import MemoryNode
from ...extraction.normalization import (
    _iter_keyword_scores,
    _tokenize_with_signal_scores,
    identifier_expanded_text,
    token_signal_score,
    token_variants,
)

LEXICAL_INDEX_SCHEMA_VERSION = 2


def _text_term_weights(value: str, config: LexicalConfig) -> dict[str, float]:
    """Build postings from every eligible term and phrase in node text."""
    return _term_weights(value, config)


def _metadata_term_weights(node: MemoryNode, property_values: Sequence[str], config: LexicalConfig) -> dict[str, float]:
    """Weight metadata terms, including phrases from expanded identifiers."""
    text = " ".join(value for value in (node.type, node.label, node.canonical_key, *property_values) if value)
    weight = config.index.metadata_weight
    return {
        term: min(weight, score * weight)
        for term, score in _term_weights(text, config, expanded_phrases=True).items()
    }


def _term_weights(text: str, config: LexicalConfig, *, expanded_phrases: bool = False) -> dict[str, float]:
    """Analyze each representation once and reuse its terms for scoring."""
    normalization = config.normalization
    tokens, signals = _tokenize_with_signal_scores(text, policy=normalization)
    raw_counts = Counter(tokens)
    expanded_text = identifier_expanded_text(text, split_identifiers=normalization.split_identifiers)
    expanded, expanded_signals = (
        _tokenize_with_signal_scores(expanded_text, policy=normalization)
        if expanded_text != text else ((), {})
    )
    expanded_counts = Counter(expanded)
    expanded_only = ((term, count) for term, count in expanded_counts.items() if term not in raw_counts)
    counts: Counter[str] = Counter()
    for token, count in chain(raw_counts.items(), expanded_only):
        for variant in token_variants(token, expand_plurals=normalization.expand_plurals):
            counts[variant] += count

    weights = {token: _token_index_weight(token, count, config.index) for token, count in counts.items()}
    for term, score in _iter_keyword_scores(tokens, signals, normalization, token_counts=raw_counts):
        weights[term] = max(weights.get(term, 0.0), score)
    if expanded_phrases:
        for term, score in _iter_keyword_scores(expanded, expanded_signals, normalization, token_counts=expanded_counts):
            weights[term] = max(weights.get(term, 0.0), score)
    return weights


def _token_index_weight(token: str, count: int, policy: LexicalIndexPolicy) -> float:
    """Combine signal and rarity into a lexical weight between zero and one."""
    signal = token_signal_score(token)
    rarity = 1.0 / max(1.0, float(count) ** 0.5)
    return min(1.0, policy.token_base_weight + policy.token_signal_weight * signal + policy.token_rarity_weight * rarity)
