"""Deterministic lexical + bounded graph retrieval."""
from __future__ import annotations

import re
from typing import Iterator, Sequence
from unicodedata import category

from ...extraction.normalization import singular_source_variants

_SOURCE_WORD = r"[^\W\d]\w*"
_SOURCE_WORD_PATTERN = re.compile(rf"\b{_SOURCE_WORD}\b")
_SOURCE_REFERENCE_PATTERN = re.compile(rf"\b{_SOURCE_WORD}(?:(?:\.|::){_SOURCE_WORD})*\b")
_CALL_SUFFIX = re.compile(r"\s*\(")


def source_symbol_references(text: str, *, qualified: bool = True) -> Iterator[str]:
    """Find literal identifier spans, keeping combining marks and original spelling."""
    return (value for value, _ in _source_symbol_spans(text, qualified=qualified))


def source_contract_references(text: str) -> Iterator[str]:
    """Return calls before qualified mentions, in source order within each group."""
    calls, qualified = [], []
    for value, end in _source_symbol_spans(text, qualified=True):
        if _CALL_SUFFIX.match(text, end):
            calls.append(value)
        elif "." in value or "::" in value:
            qualified.append(value)
    yield from calls
    yield from qualified


def _source_symbol_spans(text: str, *, qualified: bool) -> Iterator[tuple[str, int]]:
    """Scan identifiers with source offsets."""
    # Python's word classes omit combining marks. Preserve offsets in the scan
    # and use the original text for the exact lookup.
    shape = "".join("_" if category(char).startswith("M") else char for char in text)
    pattern = _SOURCE_REFERENCE_PATTERN if qualified else _SOURCE_WORD_PATTERN
    for match in pattern.finditer(shape):
        value = text[match.start():match.end()]
        if shape[match.start()] == value[0]:
            yield value, match.end()

TECHNICAL_NODE_TYPES = {"RetrievalTrace", "System", "Session", "Debug", "Log", "Comment", "Docstring", "Import"}
GRAPH_SEED_NODE_TYPES = {"Topic", "Entity", "Fact", "File", "SourceArtifact", "SourceFragment", "Module", "Function", "Class", "Interface", "Method", "Variable", "Endpoint", "Schema", "StaticAnalysisFinding"}
CODE_CONTEXT_NODE_TYPES = {
    "SourceArtifact",
    "SourceFragment",
    "File",
    "Module",
    "Function",
    "Class",
    "Interface",
    "Method",
    "Variable",
    "Endpoint",
    "Schema",
    "Config",
    "Test",
    "Dependency",
    "StaticAnalysisFinding",
}
CODE_CONTEXT_EXCLUDED_NODE_TYPES = {
    "Entity",
    "Fact",
    "Topic",
}
SEMANTIC_EDGE_TYPES = {
    "ABOUT",
    "HAS_TOPIC",
    "MENTIONS",
    "DERIVED_FROM",
    "RELATED_TO",
    "IS_A",
    "LIKES",
    "SUPPORTS",
    "EXPRESSES",
    "EXPLAINS",
    "EVIDENCED_BY",
    "SUPPORTED_BY",
    "PART_OF",
    "INSTANCE_OF",
    "CONFIRMS",
    "CORRECTS",
    "SUPERSEDES",
    "UPDATED_BY",
    "TRACKS",
}
TECHNICAL_EDGE_TYPES = {
    "GENERATED_BY_QUERY",
    "USED_IN_CONTEXT",
    "COMPILED_IN",
    "AFFECTED_BY_DELTA",
    "ASSOCIATED_WITH",
}
CODE_CONTEXT_EDGE_TYPES = {
    "CALLS",
    "CONTAINS",
    "DEFINES",
    "DEPENDS_ON",
    "EVIDENCED_BY",
    "HANDLES_ROUTE",
    "HAS_FINDING",
    "HAS_SECTION",
    "IMPORTS",
    "IMPLEMENTS",
    "IMPORTS_FROM",
    "INHERITS",
    "INSTANTIATES",
    "OVERRIDES",
    "METHOD",
    "RAISES",
    "READS",
    "REFERENCES",
    "RE_EXPORTS",
    "RETURNS",
    "WRITES",
    "WRAPS",
}
DEFAULT_CONTEXT_EDGE_TYPES = SEMANTIC_EDGE_TYPES | CODE_CONTEXT_EDGE_TYPES
SOURCE_NODE_TYPES = {"SourceFragment", "DocumentFragment"}
SOURCE_EDGE_TYPES = {"EVIDENCED_BY", "DERIVED_FROM", "SUPPORTED_BY", "CONTAINS_FRAGMENT", "HAS_SECTION", "HAS_DOCSTRING", "HAS_COMMENT"}
QUERY_EXPLORE_VIEWS = {"owners", "callers", "public_surface", "serialization_paths", "docs_mentions", "structural_duplicates", "code"}
QUERY_EXPLORE_DEFAULT_VIEWS = ("owners", "callers", "public_surface", "serialization_paths", "docs_mentions", "code")
QUERY_EXPLORE_ALL_VIEWS = (*QUERY_EXPLORE_DEFAULT_VIEWS[:-1], "structural_duplicates", "code")
QUERY_EXPLORE_EDGE_TYPES = CODE_CONTEXT_EDGE_TYPES | SOURCE_EDGE_TYPES | {"DECORATED_BY", "HAS_CODE_BLOCK", "HAS_COMMENT", "HAS_DOCSTRING", "TESTS"}
FIELD_REFERENCE_PATTERN = re.compile(r"(?<![\w./\\])([^\W\d]\w*(?:\.[^\W\d]\w*)*)\.([^\W\d]\w*)(?![\w/\\]|\.\w)")
QUERY_CONTEXT_MODES = {"informative", "cleanup"}
QUERY_CONTEXT_SCOPES = {"code", "docs", "test"}
STRUCTURED_SEARCH_FIELDS = (
    "name",
    "qualified_name",
    "symbol_name",
    "module",
    "target",
    "relative_path",
    "source_file",
    "language",
    "kind",
    "args",
    "bases",
    "returns",
    "semantic_roles",
    "wrapper_targets",
    "overrides",
    "re_exports",
)
def _canonical_token_overlap(value: str, query_tokens: set[str], *, expand_plurals: bool = True, min_chars: int = 2) -> set[str]:
    """Return expanded query-token matches from already-canonical search text."""
    if not value or not query_tokens:
        return set()
    value_tokens = {
        token
        for raw_token in set(value.split())
        if len(token := raw_token.strip("_-")) >= min_chars
    }
    overlap = value_tokens & query_tokens
    if not expand_plurals:
        return overlap
    for query_token in query_tokens - overlap:
        if any(candidate in value_tokens for candidate in singular_source_variants(query_token)):
            overlap.add(query_token)
    return overlap


def _raw_query_token_overlap(parts: Sequence[str], query_tokens: set[str]) -> set[str] | None:
    """Return a conservative cheap overlap, or ``None`` when Unicode needs normalization."""
    raw = " ".join(parts)
    if not raw.isascii():
        return None
    folded = raw.casefold()
    overlap: set[str] = set()
    for token in query_tokens:
        needles = (token, f"{token[:-1]}ie") if token.endswith("y") and len(token) > 2 else (token,)
        if any(needle in folded for needle in needles):
            overlap.add(token)
    return overlap
