"""Deterministic lexical search and ranking primitives."""
from __future__ import annotations

import re
from typing import Collection, MutableMapping, Sequence
from unicodedata import normalize

from ...domain.constants import INACTIVE_STATUSES
from ...domain.models import MemoryNode, MemoryQuery
from ...extraction.normalization import canonicalize, expanded_tokens, identifier_expanded_text, token_signal_score
from .common import (
    FIELD_REFERENCE_PATTERN,
    STRUCTURED_SEARCH_FIELDS,
    TECHNICAL_NODE_TYPES,
    _canonical_token_overlap,
    _raw_query_token_overlap,
    source_symbol_references,
)
from .context.models import NodeMatchMetrics, QueryProfile

RANKED_SYMBOL_TYPES = {"Function", "Method", "Class", "Interface", "Endpoint", "Schema"}


def symbol_lookup_key(value: str, *, ignore_case: bool = True) -> str:
    """Match canonically equivalent qualified symbols without folding accents."""
    qualified = value.replace("::", ".")
    return normalize("NFC", qualified.casefold() if ignore_case else qualified)


class RetrievalSearchMixin:
    def _search_key(self, text: str) -> str:
        return canonicalize(text, fold_accents=self.normalization.fold_accents)

    def _search_identifier_text(self, text: str) -> str:
        return identifier_expanded_text(text, split_identifiers=self.normalization.split_identifiers)

    def _search_tokens(self, text: str) -> tuple[str, ...]:
        return expanded_tokens(text, policy=self.normalization)

    def _search_token_overlap(self, text: str, tokens: set[str]) -> set[str]:
        return _canonical_token_overlap(text, tokens, expand_plurals=self.normalization.expand_plurals,
                                        min_chars=self.normalization.min_token_chars)

    def _identifier_terms(self, text: str) -> set[str]:
        tokens = [symbol_lookup_key(value) for value in source_symbol_references(text)]
        whole = symbol_lookup_key(text.strip())
        return {part for token in tokens for part in [token, *token.split(".")]
                if len(part) >= self.policy.search.identifier_min_chars or part == whole}

    @staticmethod
    def _exact_symbol_match(node: MemoryNode, terms: set[str]) -> bool:
        return node.type in RANKED_SYMBOL_TYPES and any(
            isinstance(value, str) and symbol_lookup_key(value) in terms
            for value in (node.properties.get("name"), node.properties.get("qualified_name"))
        )

    def _named_symbol_nodes(self, name: str, kinds: Collection[str]) -> list[MemoryNode]:
        """Look up literal names in the index, allowing canonically equivalent forms."""
        nodes = {}
        for spelling in dict.fromkeys((name, normalize("NFC", name), normalize("NFD", name))):
            for node in self.store.find_nodes_by_property("name", spelling, limit=None, clone=False):
                if node.type in kinds:
                    nodes[node.id] = node
        return list(nodes.values())

    def _include_exact_symbols(
        self, matches: list[tuple[MemoryNode, float]], text: str, terms: set[str],
    ) -> list[tuple[MemoryNode, float]]:
        """Keep symbol lookup independent of the lexical index's token filters."""
        seen = {node.id for node, _ in matches}
        names = dict.fromkeys(value.replace("::", ".").rsplit(".", 1)[-1]
                              for value in source_symbol_references(text))
        for name in names:
            if symbol_lookup_key(name) not in terms:
                continue
            for node in self._named_symbol_nodes(name, RANKED_SYMBOL_TYPES):
                if node.id not in seen:
                    matches.append((node, self.policy.search.exact_symbol_score))
                    seen.add(node.id)
        return matches

    def _field_owner_seeds(self, query: MemoryQuery, lexical_node_types: Sequence[str] | None,
                           scopes: set[str]) -> dict[str, float]:
        """Use literal field appearances in source fragments to find their owners."""
        policy = self.policy.search
        fields = tuple(dict.fromkeys(field for _, field in FIELD_REFERENCE_PATTERN.findall(query.text)))
        owners: dict[str, float] = {}
        for field in fields:
            expression = re.compile(rf"(?<!\w){re.escape(field)}(?!\w)", re.IGNORECASE)
            fragments = self.store.lexical_search(field, top_k=None, node_types=("SourceFragment",),
                                                  include_archived=query.include_archived, clone=False)
            for fragment, _ in fragments:
                if not query.allows_project(fragment) or not expression.search(fragment.text or ""):
                    continue
                for _, owner in self.store.neighbors(fragment.id, direction="in", edge_types={"EVIDENCED_BY"},
                                                       limit=None, clone=False):
                    if not query.allows_project(owner):
                        continue
                    if owner.type not in RANKED_SYMBOL_TYPES or not query.include_archived and owner.status in INACTIVE_STATUSES:
                        continue
                    if lexical_node_types is not None and owner.type not in lexical_node_types:
                        continue
                    if scopes and not self._node_matches_query_context_scope(owner, scopes):
                        continue
                    owners[owner.id] = policy.field_owner_score
        return owners

    @staticmethod
    def _normalized_query_path(query_text: str) -> str:
        """Normalize a whole-query path without treating free-form prose as a path."""
        value = str(query_text or "").strip().strip("'\"`").replace("\\", "/")
        while value.startswith("./"):
            value = value[2:]
        return value.casefold()

    @classmethod
    def _node_matches_exact_query_path(cls, node: MemoryNode, query_text: str) -> bool:
        """Return whether the complete query names this node's path or filename."""
        query_path = cls._normalized_query_path(query_text)
        relative_path = str(node.properties.get("relative_path") or "").strip().replace("\\", "/").casefold()
        if not query_path or not relative_path:
            return False
        return query_path == relative_path or ("/" not in query_path and query_path == relative_path.rsplit("/", 1)[-1])

    def _query_profile(self, query_text: str) -> QueryProfile:
        ordered_tokens = self._search_tokens(query_text)
        tokens = set(ordered_tokens)
        informative = {
            token
            for token in tokens
            if len(token) >= self.policy.search.informative_min_chars
            or token_signal_score(token) >= self.policy.search.strong_identifier_signal
        }
        phrase_terms = {
            f"{a} {b}"
            for a, b in zip(ordered_tokens, ordered_tokens[1:])
            if min(token_signal_score(a), token_signal_score(b)) >= self.normalization.phrase_min_signal
        }
        return QueryProfile(
            text=query_text,
            canonical=self._search_key(query_text),
            tokens=tokens,
            informative_tokens=informative or tokens,
            ordered_tokens=ordered_tokens,
            phrase_terms=phrase_terms,
        )

    def _nodes_for_types(self, node_types: Sequence[str]) -> list[MemoryNode]:
        """Load only indexed node types while preserving ``all_nodes`` order."""
        return self.store.find_nodes_by_types(node_types, clone=False)

    def _scoped_lexical_search(
        self,
        query: MemoryQuery,
        query_profile: QueryProfile,
        *,
        lexical_node_types: Sequence[str] | None,
        scopes: set[str],
        metrics_cache: MutableMapping[str, NodeMatchMetrics] | None = None,
    ) -> list[tuple[MemoryNode, float]]:
        allowed_types = set(lexical_node_types) if lexical_node_types is not None else None
        policy = self.policy.search
        indexed = self.store.lexical_search(
            query.text,
            top_k=None,
            node_types=allowed_types,
            include_archived=query.include_archived,
            clone=False,
        )
        terms = self._identifier_terms(query.text)
        candidates = [node for node, _score in self._include_exact_symbols(indexed, query.text, terms)]
        if not candidates:
            candidates = self._nodes_for_types(allowed_types) if allowed_types is not None else self.store.all_nodes()
        matches: list[tuple[MemoryNode, float]] = []
        for node in candidates:
            if not query.allows_project(node):
                continue
            if allowed_types is not None and node.type not in allowed_types:
                continue
            if node.type in TECHNICAL_NODE_TYPES:
                continue
            if not query.include_archived and node.status in INACTIVE_STATUSES:
                continue
            if not self._node_matches_query_context_scope(node, scopes):
                continue
            exact = bool(terms and self._exact_symbol_match(node, terms))
            if exact:
                matches.append((node, policy.exact_symbol_score))
                continue
            raw_overlap = _raw_query_token_overlap(self._node_search_parts(node), query_profile.informative_tokens)
            if (
                raw_overlap is not None
                and len(query_profile.informative_tokens) >= self.policy.ranking.weak_query_min_terms
                and len(raw_overlap) <= policy.weak_overlap_count
                and not self._has_strong_identifier_overlap(raw_overlap)
            ):
                continue
            metrics = metrics_cache.get(node.id) if metrics_cache is not None else None
            if metrics is None:
                metrics = self._node_match_metrics(node, query_profile)
                if metrics_cache is not None:
                    metrics_cache[node.id] = metrics
            score = metrics.match_score
            if score <= 0.0:
                continue
            if self._is_weak_multiterm_match(
                node,
                query_tokens=query_profile.informative_tokens,
                direct_relevance=score,
                overlap_count=metrics.overlap_count,
                has_strong_identifier_overlap=metrics.strong_identifier_overlap,
            ):
                continue
            matches.append((node, score))
        matches.sort(
            key=lambda item: (
                item[1],
                self._retrieval_type_bonus(item[0], item[1]),
                item[0].salience,
                self._node_relative_path(item[0]) or "",
                self._node_label(item[0]),
            ),
            reverse=True,
        )
        return matches


    def _node_match_metrics(self, node: MemoryNode, query_profile: QueryProfile) -> NodeMatchMetrics:
        policy = self.policy.search
        query_key = query_profile.canonical
        if not query_key:
            return NodeMatchMetrics(0.0, 0.0)
        canonical_parts = [
            self._search_key(part)
            for part in (node.canonical_key, node.label, node.text)
            if part
        ]
        if query_key in canonical_parts:
            return NodeMatchMetrics(policy.exact_topic if node.type == "Topic" else policy.exact_text, 1.0, frozenset(query_profile.informative_tokens))
        node_text = self._node_search_text(node)
        node_key = self._search_key(node_text)
        if not node_key:
            return NodeMatchMetrics(0.0, 0.0)
        overlap = self._search_token_overlap(node_key, query_profile.informative_tokens)
        strong_identifier_overlap = self._has_strong_identifier_overlap(overlap)
        identifier_keys = [
            self._search_key(self._search_identifier_text(part))
            for part in self._node_identifier_parts(node)
            if part
        ]
        identifier_overlap = self._search_token_overlap(" ".join(identifier_keys), query_profile.informative_tokens)
        identifier_coverage = self._coverage(identifier_overlap, query_profile)

        def result(match_score: float, coverage: float) -> NodeMatchMetrics:
            return NodeMatchMetrics(match_score, coverage, frozenset(overlap), strong_identifier_overlap)

        coverage = self._coverage(overlap, query_profile)
        phrase_coverage = self._phrase_coverage(node_key, query_profile)
        coverage = max(coverage, phrase_coverage)
        if query_key in identifier_keys:
            return result(policy.exact_identifier, 1.0)
        if any(f" {query_key} " in f" {key} " for key in identifier_keys):
            return result(policy.identifier_phrase, max(coverage, identifier_coverage, policy.identifier_phrase_coverage_floor))
        if identifier_coverage >= 1.0:
            return result(policy.identifier_full_with_phrase if phrase_coverage >= policy.phrase_full_coverage else policy.identifier_full, 1.0)
        if f" {query_key} " in f" {node_key} ":
            return result(policy.text_phrase, max(coverage, policy.text_phrase_coverage_floor))
        if node_key.startswith(query_key) or any(part.startswith(query_key) for part in canonical_parts):
            return result(policy.text_prefix, max(coverage, policy.prefix_coverage_floor))
        if query_profile.informative_tokens and len(overlap) == len(query_profile.informative_tokens):
            score = policy.text_full_with_phrase if phrase_coverage >= policy.phrase_full_coverage else policy.text_full
            return result(score, 1.0)
        if phrase_coverage >= policy.phrase_high_coverage and coverage >= policy.high_phrase_min_coverage:
            return result(policy.phrase_high, coverage)
        if phrase_coverage >= policy.phrase_medium_coverage and coverage >= policy.medium_phrase_min_coverage:
            return result(policy.phrase_medium, coverage)
        if strong_identifier_overlap:
            return result(policy.strong_overlap, max(coverage, policy.strong_coverage_floor))
        if coverage >= policy.high_token_coverage:
            return result(policy.coverage_high, coverage)
        if coverage >= policy.medium_token_coverage:
            return result(policy.coverage_medium, coverage)
        if coverage > 0:
            phrase_bonus = policy.coverage_phrase_weight * phrase_coverage
            return result(min(policy.coverage_partial_limit, policy.coverage_partial_weight * coverage + phrase_bonus), coverage)
        source = str(node.properties.get("relative_path") or node.properties.get("path") or "")
        source_key = self._search_key(" ".join((source, self._search_identifier_text(source))))
        source_overlap = self._search_token_overlap(source_key, query_profile.informative_tokens)
        source_coverage = self._coverage(source_overlap, query_profile)
        if source_coverage >= policy.source_min_coverage:
            return result(policy.source_coverage_weight * source_coverage, source_coverage)
        return result(0.0, 0.0)

    @staticmethod
    def _coverage(tokens: set[str], query_profile: QueryProfile) -> float:
        if not query_profile.informative_tokens:
            return 0.0
        return len(tokens & query_profile.informative_tokens) / len(query_profile.informative_tokens)

    @staticmethod
    def _phrase_coverage(node_key: str, query_profile: QueryProfile) -> float:
        if not query_profile.phrase_terms:
            return 0.0
        haystack = f" {node_key} "
        matched = sum(1 for phrase in query_profile.phrase_terms if f" {phrase} " in haystack)
        return matched / len(query_profile.phrase_terms)

    def _significant_query_phrases(self, query_text: str) -> list[str]:
        tokens = [
            token
            for token in self._search_tokens(query_text)
            if len(token) >= self.policy.search.phrase_min_chars or token_signal_score(token) >= self.policy.search.phrase_word_signal
        ]
        phrases: list[str] = []
        seen: set[str] = set()
        for size in (4, 3, 2):
            for index in range(len(tokens) - size + 1):
                phrase = " ".join(tokens[index : index + size])
                key = self._search_key(phrase)
                if key and key not in seen:
                    seen.add(key)
                    phrases.append(key)
        return phrases

    def _retrieval_type_bonus(self, node: MemoryNode, match_score: float) -> float:
        return self.policy.search.type_bonuses.get(node.type, 0.0) if match_score > 0.0 else 0.0

    def _direct_relevance_score(self, node: MemoryNode, query_text: str, *, query_tokens: set[str] | None = None) -> float:
        profile = self._query_profile(query_text)
        if query_tokens is not None:
            profile.tokens = query_tokens
            profile.informative_tokens = {
                token
                for token in query_tokens
                if len(token) >= self.policy.search.informative_min_chars
            or token_signal_score(token) >= self.policy.search.strong_identifier_signal
            } or query_tokens
            profile.ordered_tokens = tuple(token for token in profile.ordered_tokens if token in query_tokens)
            profile.phrase_terms = {
                f"{a} {b}"
                for a, b in zip(profile.ordered_tokens, profile.ordered_tokens[1:])
                if min(token_signal_score(a), token_signal_score(b)) >= self.normalization.phrase_min_signal
            }
        return self._node_match_metrics(node, profile).match_score

    def _is_weak_multiterm_match(
        self,
        node: MemoryNode,
        *,
        query_tokens: set[str],
        direct_relevance: float,
        overlap_count: int | None = None,
        has_strong_identifier_overlap: bool | None = None,
    ) -> bool:
        search, ranking = self.policy.search, self.policy.ranking
        if len(query_tokens) < ranking.weak_query_min_terms or direct_relevance >= ranking.weak_match_min_score:
            return False
        overlap_tokens: set[str] | None = None
        if overlap_count is None or has_strong_identifier_overlap is None:
            overlap_tokens = self._node_query_token_overlap_tokens(node, query_tokens)
        if has_strong_identifier_overlap is None:
            has_strong_identifier_overlap = self._has_strong_identifier_overlap(overlap_tokens or set())
        if has_strong_identifier_overlap:
            return False
        overlap = overlap_count if overlap_count is not None else len(overlap_tokens or set())
        if overlap <= search.weak_overlap_count:
            return True
        return (overlap / len(query_tokens)) < search.weak_overlap_ratio

    @staticmethod
    def _node_identifier_parts(node: MemoryNode) -> list[str]:
        parts = [str(part) for part in (node.label, node.canonical_key) if part]
        for key in STRUCTURED_SEARCH_FIELDS:
            value = node.properties.get(key)
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                parts.extend(str(item) for item in value if item)
            else:
                parts.append(str(value))
        return parts

    @classmethod
    def _node_search_parts(cls, node: MemoryNode) -> list[str]:
        parts = [str(node.text)] if node.text else []
        parts.extend(cls._node_identifier_parts(node))
        return parts

    def _node_search_text(self, node: MemoryNode) -> str:
        parts = self._node_search_parts(node)
        if self.normalization.split_identifiers:
            parts.extend(self._search_identifier_text(part) for part in tuple(parts))
        return " ".join(parts)

    def _node_query_token_overlap_tokens(self, node: MemoryNode, query_tokens: set[str]) -> set[str]:
        if not query_tokens:
            return set()
        node_text = self._node_search_text(node)
        node_key = self._search_key(node_text)
        return self._search_token_overlap(node_key, query_tokens)

    def _has_strong_identifier_overlap(self, tokens: set[str]) -> bool:
        return any(
            token_signal_score(token) >= self.policy.search.strong_identifier_signal and (any(separator in token for separator in ("_", "-")) or any(char.isdigit() for char in token))
            for token in tokens
        )
