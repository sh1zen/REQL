"""Typed contracts shared by every query-context provider."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any, Iterable, Literal


CONTEXT_RESULT_SCHEMA_VERSION = 2
DEFAULT_TOP_K = 20
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_ITEMS = 20
MIN_TOP_K = 1
MAX_TOP_K = 50
MIN_DEPTH = 0
MAX_DEPTH = 5
MIN_ITEMS = 1
MAX_ITEMS = 50

ContextScope = Literal["code", "docs", "test"]
ConfidenceStatus = Literal["sufficient", "insufficient"]
ContextPayload = dict[str, Any]

_CONTEXT_SCOPES = frozenset({"code", "docs", "test"})


class QueryMode(str, Enum):
    """Supported query-context projections."""

    INFORMATIVE = "informative"
    CLEANUP = "cleanup"


@dataclass(frozen=True, slots=True)
class RetrievalBudget:
    """Shared bounded-retrieval limits for every provider."""

    top_k: int = DEFAULT_TOP_K
    max_depth: int = DEFAULT_MAX_DEPTH
    max_items: int = DEFAULT_MAX_ITEMS

    def __post_init__(self) -> None:
        _validate_bounded_int(self.top_k, "top_k", minimum=MIN_TOP_K, maximum=MAX_TOP_K)
        _validate_bounded_int(self.max_depth, "max_depth", minimum=MIN_DEPTH, maximum=MAX_DEPTH)
        _validate_bounded_int(self.max_items, "max_items", minimum=MIN_ITEMS, maximum=MAX_ITEMS)


@dataclass(frozen=True, slots=True)
class SeenSourceRange:
    """A verified range delivered by a previous brief."""

    path: str
    line_start: int
    line_end: int
    sha256: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.path)
        if (not path.parts or path.is_absolute() or PureWindowsPath(self.path).drive or ".." in path.parts
                or "\\" in self.path or any(char in self.path for char in "\x00\r\n")
                or type(self.line_start) is not int or type(self.line_end) is not int
                or not 1 <= self.line_start <= self.line_end
                or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None):
            raise ValueError("seen source requires a project-relative path, ordered lines and a SHA-256 hash")

    @classmethod
    def parse(cls, value: str) -> "SeenSourceRange":
        """Parse the range format used in follow-up commands."""
        match = re.fullmatch(r"(.+)#L([1-9][0-9]{0,7})-L([1-9][0-9]{0,7})@([0-9a-f]{64})", value)
        if match is None:
            raise ValueError("use --seen-source PATH#LSTART-LEND@SHA256")
        return cls(match[1], int(match[2]), int(match[3]), match[4])

    def __str__(self) -> str:
        return f"{self.path}#L{self.line_start}-L{self.line_end}@{self.sha256}"


@dataclass(frozen=True, slots=True)
class QueryContextRequest:
    """Canonical request accepted by the query-context application service."""

    text: str
    mode: QueryMode = QueryMode.INFORMATIVE
    scopes: frozenset[ContextScope] = field(default_factory=frozenset)
    budget: RetrievalBudget = field(default_factory=RetrievalBudget)
    include_archived: bool = False
    include_source: bool = False
    task_id: str | None = None
    seen_source: tuple[SeenSourceRange, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("query context text must not be empty")
        if not isinstance(self.mode, QueryMode):
            raise TypeError("mode must be a QueryMode")
        if not isinstance(self.scopes, frozenset):
            raise TypeError("scopes must be a frozenset")
        invalid_scopes = sorted(str(scope) for scope in self.scopes if scope not in _CONTEXT_SCOPES)
        if invalid_scopes:
            raise ValueError(f"unknown query_context scope '{invalid_scopes[0]}'. Choose from: code, docs, test")
        if not isinstance(self.budget, RetrievalBudget):
            raise TypeError("budget must be a RetrievalBudget")
        if not isinstance(self.include_archived, bool):
            raise TypeError("include_archived must be a bool")
        if not isinstance(self.include_source, bool):
            raise TypeError("include_source must be a bool")
        if self.include_source and (not self.scopes or not self.scopes <= {"code", "test"}):
            raise ValueError("include_source requires explicit code or test scopes without docs")
        if self.task_id is not None and (not isinstance(self.task_id, str) or not self.task_id.strip()):
            raise ValueError("task_id must be a nonempty work record id")
        if not isinstance(self.seen_source, tuple) or any(not isinstance(span, SeenSourceRange) for span in self.seen_source):
            raise TypeError("seen_source must be a tuple of SeenSourceRange")

    @classmethod
    def from_raw(
        cls,
        *,
        text: str,
        mode: str | QueryMode = QueryMode.INFORMATIVE,
        scopes: Iterable[str] | None = None,
        top_k: int = DEFAULT_TOP_K,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_items: int = DEFAULT_MAX_ITEMS,
        include_archived: bool = False,
        include_source: bool = False,
        task_id: str | None = None,
        seen_source: Iterable[SeenSourceRange] = (),
    ) -> "QueryContextRequest":
        """Normalize provider primitives into the canonical typed request."""
        try:
            normalized_mode = mode if isinstance(mode, QueryMode) else QueryMode(str(mode or "").strip().casefold())
        except ValueError as exc:
            valid = ", ".join(item.value for item in QueryMode)
            raise ValueError(f"unknown query_context mode '{mode}'. Choose from: {valid}") from exc

        normalized_scopes: set[ContextScope] = set()
        for scope in scopes or ():
            normalized = str(scope or "").strip().casefold()
            if not normalized:
                continue
            if normalized not in _CONTEXT_SCOPES:
                raise ValueError(f"unknown query_context scope '{scope}'. Choose from: code, docs, test")
            normalized_scopes.add(normalized)  # type: ignore[arg-type]

        return cls(
            text=text,
            mode=normalized_mode,
            scopes=frozenset(normalized_scopes),
            budget=RetrievalBudget(top_k=top_k, max_depth=max_depth, max_items=max_items),
            include_archived=include_archived,
            include_source=include_source,
            task_id=task_id,
            seen_source=tuple(seen_source),
        )


@dataclass(frozen=True, slots=True)
class Confidence:
    """Typed retrieval-confidence metadata."""

    status: ConfidenceStatus
    max_score: float
    threshold: float
    targeted_rg_fallback_allowed: bool
    reason: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Confidence":
        status = str(payload.get("status") or "")
        if status not in {"sufficient", "insufficient"}:
            raise ValueError(f"unknown query_context confidence status '{status}'")
        return cls(
            status=status,  # type: ignore[arg-type]
            max_score=float(payload.get("max_score") or 0.0),
            threshold=float(payload.get("threshold") or 0.0),
            targeted_rg_fallback_allowed=bool(payload.get("targeted_rg_fallback_allowed")),
            reason=str(payload.get("reason") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "max_score": self.max_score,
            "threshold": self.threshold,
            "targeted_rg_fallback_allowed": self.targeted_rg_fallback_allowed,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SourceRevision:
    """Committed project revision represented by a context result."""

    id: str
    sequence: int
    tree_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "sequence": self.sequence, "tree_hash": self.tree_hash}


@dataclass(frozen=True, slots=True)
class GraphFreshness:
    """Availability and source-lag metadata for a committed graph read."""

    status: Literal["current", "stale", "unknown"] = "unknown"
    pending_paths: int = 0
    checked_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pending_paths": self.pending_paths,
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True, slots=True)
class ContextResult:
    """Versioned typed result returned by the shared application service."""

    schema_version: int
    graph_revision: str
    confidence: Confidence
    payload: ContextPayload
    source_revision: SourceRevision | None = None
    freshness: GraphFreshness = field(default_factory=GraphFreshness)

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned envelope used by typed consumers."""
        return {
            "schema_version": self.schema_version,
            "graph_revision": self.graph_revision,
            "source_revision": self.source_revision.to_dict() if self.source_revision else None,
            "freshness": self.freshness.to_dict(),
            "confidence": self.confidence.to_dict(),
            "payload": dict(self.payload),
        }


def _validate_bounded_int(value: int, name: str, *, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
