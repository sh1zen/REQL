"""Naming conventions shared by compile summaries and test associations."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import PurePosixPath

from ..storage.graph_store import GraphStore
from .context_scope import is_test_context


def project_test_paths(store: GraphStore, project_id: str) -> Iterator[str]:
    """Yield test paths from active artifacts in the owning project."""
    for artifact in store.find_nodes_by_property("project_id", project_id, type_="SourceArtifact",
                                                 status="active", limit=None, clone=False):
        path = artifact.properties.get("relative_path")
        if isinstance(path, str) and path and is_test_context(artifact.type, artifact.properties, path):
            yield path


def test_path_stem(path: str) -> str:
    """Extract a filename stem independently of the host path separator."""
    return PurePosixPath(path.replace("\\", "/")).stem.casefold()


def candidate_test_stems(path: str) -> set[str]:
    """Build the expected test stems for a source file."""
    stem = test_path_stem(path)
    if not stem or stem == "__init__":
        return set()
    return {f"test_{stem}", f"{stem}_test", f"{stem}.test", f"{stem}.spec", stem}


def candidate_test_names(name: str) -> set[str]:
    """Build candidate test-case names from a source symbol."""
    title = name[:1].upper() + name[1:]
    return {f"test_{name}", f"test{name}", f"test{title}"}
