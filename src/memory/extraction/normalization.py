"""Text normalization and deterministic lexical utilities."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterator, Mapping
from functools import lru_cache
from itertools import pairwise
from types import MappingProxyType

from ..config.loader import default_config
from ..config.policies import LexicalNormalizationPolicy, SeedExtractionPolicy

_DEFAULT_LEXICAL = default_config().lexical

_WORD_RE = re.compile(r"[\w][\w'\-]*")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
TOKEN_ANALYSIS_CACHE_SIZE = 1024
TOKEN_ANALYSIS_MAX_CHARS = 2048


def strip_accents(value: str) -> str:
    if value.isascii():
        return value
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_text(value: str) -> str:
    value = value.strip().replace("\u2019", "'").replace("\u2018", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", value)


def canonicalize(value: str, *, fold_accents: bool = True) -> str:
    """Reuse short normalized strings without retaining large source bodies."""
    normalize = _cached_canonicalize if len(value) <= TOKEN_ANALYSIS_MAX_CHARS else _canonicalize
    return normalize(value, fold_accents)


def _canonicalize(value: str, fold_accents: bool) -> str:
    value = normalize_text(value).lower()
    if fold_accents:
        value = strip_accents(value)
        value = re.sub(r"[^a-z0-9_\-\s]", " ", value)
    else:
        value = re.sub(r"[^\w\-\s]", " ", unicodedata.normalize("NFC", value))
    return re.sub(r"\s+", " ", value).strip()


_cached_canonicalize = lru_cache(maxsize=8192)(_canonicalize)


def token_signal_score(token: str) -> float:
    """Estimate token usefulness without language-specific stopword lists."""
    return _token_signal_score_stripped(token.strip("_-"))


@lru_cache(maxsize=8192)
def _token_signal_score_stripped(token: str) -> float:
    """Reuse immutable token scores throughout lexical indexing and ranking."""
    if len(token) < 2:
        return 0.0
    has_alpha = False
    has_digit = False
    has_structure = False
    for char in token:
        if not has_alpha and char.isalpha():
            has_alpha = True
        elif not has_digit and char.isdigit():
            has_digit = True
        if not has_structure and char in "_-./\\#+":
            has_structure = True
    if not has_alpha and not has_digit:
        return 0.0
    score = 0.25
    if len(token) >= 4:
        score += 0.25
    if len(token) >= 6:
        score += 0.25
    if has_digit:
        score += 0.15
    if has_structure:
        score += 0.20
    return min(1.0, score)


def tokenize(value: str, *, keep_stopwords: bool = False,
             policy: LexicalNormalizationPolicy = _DEFAULT_LEXICAL.normalization) -> list[str]:
    """Return independent tokens while reusing bounded immutable text analysis."""
    tokens, _ = _tokenize_with_signal_scores(value, keep_stopwords=keep_stopwords, policy=policy)
    return list(tokens)


def identifier_expanded_text(value: str, *, split_identifiers: bool = True) -> str:
    """Expose path and identifier components as ordinary lexical words."""
    value = str(value)
    if not split_identifiers:
        return value
    expand = _cached_identifier_expansion if len(value) <= TOKEN_ANALYSIS_MAX_CHARS else _expand_identifiers
    return expand(value)


def _expand_identifiers(value: str) -> str:
    separated = value.replace("\\", " ").replace("/", " ").replace(".", " ")
    separated = separated.replace("_", " ").replace("-", " ").replace(":", " ")
    return _CAMEL_BOUNDARY_RE.sub(" ", separated)


_cached_identifier_expansion = lru_cache(maxsize=8192)(_expand_identifiers)


@lru_cache(maxsize=32768)
def expanded_tokens(value: str, *, policy: LexicalNormalizationPolicy = _DEFAULT_LEXICAL.normalization) -> tuple[str, ...]:
    """Return deterministic tokens including identifier and plural forms."""

    tokens: list[str] = []
    seen: set[str] = set()
    for candidate in dict.fromkeys((value, identifier_expanded_text(value, split_identifiers=policy.split_identifiers))):
        for token in tokenize(candidate, policy=policy):
            for variant in token_variants(token, expand_plurals=policy.expand_plurals):
                if variant not in seen:
                    seen.add(variant)
                    tokens.append(variant)
    return tuple(tokens)


def token_variants(token: str, *, expand_plurals: bool = True) -> tuple[str, ...]:
    variants = [token]
    singular = singular_token(token) if expand_plurals else None
    if singular and singular != token:
        variants.append(singular)
    return tuple(variants)


@lru_cache(maxsize=32768)
def singular_token(token: str) -> str | None:
    if len(token) <= 3:
        return None
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if token.endswith("sses"):
        return None
    if token.endswith("ses") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return None


@lru_cache(maxsize=32768)
def singular_source_variants(token: str) -> tuple[str, ...]:
    """Return plural spellings whose normalized form is ``token``."""

    candidates = {f"{token}s", f"{token}es"}
    if token.endswith("y"):
        candidates.add(f"{token[:-1]}ies")
    return tuple(candidate for candidate in candidates if singular_token(candidate) == token)


def _tokenize_with_signal_scores(value: str, *, keep_stopwords: bool = False,
                                 policy: LexicalNormalizationPolicy = _DEFAULT_LEXICAL.normalization) -> tuple[tuple[str, ...], Mapping[str, float]]:
    """Share short-text analysis without retaining large source bodies in this cache."""
    canonical = canonicalize(value, fold_accents=policy.fold_accents)
    if len(canonical) > TOKEN_ANALYSIS_MAX_CHARS:
        return _analyze_token_signals(canonical, keep_stopwords, policy.min_token_chars, policy.min_token_signal)
    return _cached_token_signals(canonical, keep_stopwords, policy.min_token_chars, policy.min_token_signal)


@lru_cache(maxsize=TOKEN_ANALYSIS_CACHE_SIZE)
def _cached_token_signals(canonical: str, keep_stopwords: bool, min_chars: int, min_signal: float) -> tuple[tuple[str, ...], Mapping[str, float]]:
    """Memoize immutable results keyed by the actual normalized text and token policy."""
    return _analyze_token_signals(canonical, keep_stopwords, min_chars, min_signal)


def _analyze_token_signals(canonical: str, keep_stopwords: bool, min_chars: int, min_signal: float) -> tuple[tuple[str, ...], Mapping[str, float]]:
    """Calculate the same ordered tokens and signal weights for cached and large inputs."""
    tokens: list[str] = []
    signal_scores: dict[str, float] = {}
    for match in _WORD_RE.finditer(canonical):
        token = match.group(0).strip("_-")
        if len(token) < min_chars:
            continue
        if keep_stopwords:
            tokens.append(token)
            continue
        score = signal_scores.get(token)
        if score is None:
            score = _token_signal_score_stripped(token)
            signal_scores[token] = score
        if score > 0.0 and score >= min_signal:
            tokens.append(token)
    return tuple(tokens), MappingProxyType(signal_scores)


def split_sentences(value: str, *, policy: LexicalNormalizationPolicy = _DEFAULT_LEXICAL.normalization) -> list[str]:
    pieces = [normalize_text(piece) for piece in _SENTENCE_RE.split(value) if piece.strip()]
    # Split very long comma-heavy statements into semantically useful chunks.
    out: list[str] = []
    for piece in pieces:
        if len(piece) > policy.sentence_clause_chars and ";" in piece:
            out.extend([p.strip() for p in piece.split(";") if p.strip()])
        else:
            out.append(piece)
    return out


def keyword_scores(value: str, *, max_terms: int = 12,
                   policy: LexicalNormalizationPolicy = _DEFAULT_LEXICAL.normalization) -> list[tuple[str, float]]:
    tokens, token_scores = _tokenize_with_signal_scores(value, policy=policy)
    scored = sorted(
        _iter_keyword_scores(tokens, token_scores, policy),
        key=lambda item: (item[1], len(item[0])),
        reverse=True,
    )
    return scored[:max_terms]


def _iter_keyword_scores(
    tokens: tuple[str, ...],
    token_scores: Mapping[str, float],
    policy: LexicalNormalizationPolicy,
    *,
    token_counts: Counter[str] | None = None,
) -> Iterator[tuple[str, float]]:
    """Score all keywords and phrases from one lexical analysis."""
    if not tokens:
        return
    counts = Counter(tokens) if token_counts is None else token_counts.copy()
    # Add compact bigrams when both sides have useful structural signal.
    for a, b in pairwise(tokens):
        if min(token_scores[a], token_scores[b]) >= policy.phrase_min_signal:
            counts[f"{a} {b}"] += policy.phrase_frequency
    max_count = max(counts.values()) or 1
    for term, count in counts.items():
        if " " in term:
            specificity = policy.phrase_specificity + min(token_scores[part] for part in term.split())
        else:
            specificity = token_scores[term]
        yield term, min(1.0, (count / max_count) * specificity)


def extract_capitalized_entities(value: str, *,
                                 policy: SeedExtractionPolicy = _DEFAULT_LEXICAL.seeds,
                                 normalization: LexicalNormalizationPolicy = _DEFAULT_LEXICAL.normalization) -> list[str]:
    candidates: list[str] = []
    # Quoted strings and code-like tokens carry useful entity signal without a language list.
    if policy.quoted_entities:
        candidates.extend(quoted.strip() for quoted in re.findall(r"['\"]([^'\"]{2,60})['\"]", value))
    if policy.capitalized_entities:
        candidates.extend(token for token in re.findall(r"\b[A-Z\u00C0-\u017F][\w+#.-]{1,}\b", value)
                          if _looks_entity_like(token))
    if policy.structured_entities:
        candidates.extend(token for token in re.findall(r"\b(?:[A-Z]{2,}|[A-Za-z]+\d[A-Za-z0-9]*)\b", value)
                          if _looks_entity_like(token))
    seen = set()
    out = []
    for candidate in candidates:
        key = canonicalize(candidate, fold_accents=normalization.fold_accents)
        if key and key not in seen:
            seen.add(key)
            out.append(candidate.strip())
    return out


def _looks_entity_like(token: str) -> bool:
    if len(token) < 3:
        return False
    if any(separator in token for separator in ("_", "-", ".", "/", "\\", "#", "+")):
        return True
    if any(char.isdigit() for char in token):
        return True
    if token.isupper() and len(token) >= 2:
        return True
    return any(char.islower() for char in token) and any(char.isupper() for char in token[1:])


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def short_label(value: str, max_len: int = 72) -> str:
    value = normalize_text(value)
    return value if len(value) <= max_len else value[: max_len - 1].rstrip() + "..."
