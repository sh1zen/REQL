"""Canonical public Python API for REQL."""
from __future__ import annotations

from sys import modules as _modules

from api import (
    BlockGraphStore,
    ConfigError,
    Confidence,
    ContextPayload,
    ContextResult,
    ContextScope,
    GraphFreshness,
    MemoryEdge,
    MemoryGraph,
    MemoryNode,
    MemoryQuery,
    MemorySubgraph,
    ProjectPipeline,
    QueryResult,
    QueryContextRequest,
    QueryMode,
    REQLConfig,
    REQLError,
    REQLEvaluationError,
    REQLSyntaxError,
    RetrievalBudget,
    SourceRevision,
    TestAssociation,
    load_config,
)
from memory import config, query

# Keep the public configuration and query import paths for existing callers.
_modules[f"{__name__}.config"] = config
_modules[f"{__name__}.query"] = query

__all__ = [
    "MemoryGraph",
    "MemoryNode",
    "MemoryEdge",
    "MemoryQuery",
    "MemorySubgraph",
    "ProjectPipeline",
    "Confidence",
    "ContextPayload",
    "ContextResult",
    "ContextScope",
    "GraphFreshness",
    "QueryContextRequest",
    "QueryMode",
    "RetrievalBudget",
    "SourceRevision",
    "TestAssociation",
    "BlockGraphStore",
    "QueryResult",
    "REQLError",
    "REQLSyntaxError",
    "REQLEvaluationError",
    "REQLConfig",
    "ConfigError",
    "load_config",
]
