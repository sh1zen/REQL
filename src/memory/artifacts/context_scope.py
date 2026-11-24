"""Canonical compile-time scope labels for project artifacts."""
from __future__ import annotations

from functools import lru_cache
from pathlib import PurePosixPath
import re
from typing import Literal, Mapping

from ..code_analysis.catalog import ECMASCRIPT_EXTENSIONS
from ..config import default_config
from ..config.policies import TestDiscoveryPolicy
from ..domain.query_context import ContextScope
from .models import SourceArtifact


TEMPLATE_FILE_EXTENSIONS = (".php", ".phtml", ".html", ".htm", ".twig", ".blade.php", ".jinja", ".jinja2", ".hbs", ".mustache", ".vue")
STYLESHEET_EXTENSIONS = (".css", ".scss", ".sass", ".less")
_TEST_CASE_NAME = re.compile(r"test(?:_.*|[A-Z].*)?")


def artifact_context_scope(artifact: SourceArtifact) -> ContextScope:
    """Assign the scope persisted for an artifact and its derived records."""
    path = artifact.relative_path
    if artifact.artifact_type == "code" and is_test_context_path(path, artifact.test_discovery):
        return "test"
    if artifact.artifact_type == "code" or application_surface_kind(path) is not None:
        return "code"
    return "docs"


@lru_cache(maxsize=4096)
def is_test_context_path(path: str, policy: TestDiscoveryPolicy | None = None) -> bool:
    """Match standard test filenames and configured ancestor directories."""
    filename = PurePosixPath(path.replace("\\", "/").lstrip("/"))
    stem = filename.stem.casefold()
    if stem.startswith("test_") or stem.endswith("_test"):
        return True
    if filename.suffix.casefold() in ECMASCRIPT_EXTENSIONS and stem.endswith((".test", ".spec")):
        return True
    patterns = (policy or _default_test_discovery()).directories
    return bool(patterns) and any(_matches_test_directory(parent, patterns) for parent in filename.parents if parent.name)


@lru_cache(maxsize=4096)
def _matches_test_directory(directory: PurePosixPath, patterns: tuple[str, ...]) -> bool:
    """Cache directory matches shared by files under the same ancestors."""
    path = directory.as_posix()
    return any(re.fullmatch(pattern, path, re.IGNORECASE) or re.fullmatch(pattern, directory.name, re.IGNORECASE)
               for pattern in patterns)


@lru_cache(maxsize=1)
def _default_test_discovery() -> TestDiscoveryPolicy:
    """Share built-in conventions with callers handling legacy records."""
    return default_config().testing.discovery


def matches_test_case_name(name: str) -> bool:
    """Recognize conventional function and method names within a test file."""
    return _TEST_CASE_NAME.fullmatch(name) is not None


def is_test_context(node_type: str, properties: Mapping[str, object], path: str) -> bool:
    """Recognize indexed test records and test paths from older indexes."""
    scope = str(properties.get("context_scope") or "").strip().casefold()
    return node_type.casefold() == "test" or (scope == "test" if scope else is_test_context_path(path))


def is_documentation_context(node_type: str, properties: Mapping[str, object], path: str) -> bool:
    """Use the producer's document scope, with path conventions for older records."""
    if node_type in {"Docstring", "Comment", "DocumentFragment"}:
        return True
    scope = properties.get("context_scope")
    return scope == "docs" if scope else is_documentation_path(path)


@lru_cache(maxsize=4096)
def is_documentation_path(path: str) -> bool:
    """Recognize documentation paths when only a path is available."""
    normalized = PurePosixPath(path.replace("\\", "/").casefold())
    return normalized.suffix in {".md", ".markdown", ".rst"} or "docs" in normalized.parts[:-1]


@lru_cache(maxsize=4096)
def application_surface_kind(path: str) -> Literal["style", "script", "template"] | None:
    """Classify linkable application files by format, irrespective of directory."""
    normalized = path.replace("\\", "/").casefold()
    suffix = PurePosixPath(normalized).suffix
    if suffix in STYLESHEET_EXTENSIONS:
        return "style"
    if suffix in ECMASCRIPT_EXTENSIONS:
        return "script"
    if suffix in TEMPLATE_FILE_EXTENSIONS:
        return "template"
    return None
