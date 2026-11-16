"""Canonical compile-time scope labels for project artifacts."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..domain.query_context import ContextScope
from .models import SourceArtifact


def artifact_context_scope(artifact: SourceArtifact) -> ContextScope:
    rel = artifact.relative_path.replace("\\", "/").lstrip("/")
    if artifact.artifact_type == "code" and is_test_context_path(rel):
        return "test"
    if artifact.artifact_type == "code" or _is_application_surface_path(rel):
        return "code"
    return "docs"


@lru_cache(maxsize=4096)
def is_test_context_path(path: str) -> bool:
    """Identify test paths consistently across compilation and retrieval."""
    normalized = path.replace("\\", "/").lstrip("/").casefold()
    parts = normalized.split("/")
    name = parts[-1]
    javascript = Path(name).suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
    return (any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in parts[:-1]) or name.startswith("test_")
            or name.endswith("_test.py") or javascript and Path(name).stem.endswith((".test", ".spec")))


def _is_application_surface_path(path: str) -> bool:
    value = path.replace("\\", "/").lstrip("/").casefold()
    return value.startswith(
        (
            "app/views/",
            "app/templates/",
            "public/assets/",
            "public/css/",
            "public/js/",
            "resources/views/",
            "resources/css/",
            "resources/js/",
            "templates/",
            "views/",
            "assets/",
            "static/",
        )
    ) or any(part in value for part in ("/views/", "/templates/", "/public/assets/", "/static/"))
