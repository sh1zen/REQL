"""Stable path and content fingerprints for source artifacts."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from importlib.metadata import distributions
from os import fstat
from pathlib import Path
from typing import Any

from ..domain.ids import stable_id

# Advance this revision for project-wide changes to persisted facts.
# Language-specific revisions belong to the extractor that produces the facts.
PARSER_FACTS_VERSION = 15
DEFAULT_PARSER_VERSION = f"project-scan-v{PARSER_FACTS_VERSION}"
DEFAULT_CHUNKING_VERSION = "uncompiled-v1"


def read_bounded_file(path: Path, limit: int) -> bytes | None:
    """Read through EOF within a byte ceiling without allocating the full ceiling up front."""
    with path.open("rb") as stream:
        size = fstat(stream.fileno()).st_size
        if size > limit:
            return None
        content = stream.read(size + 1)
        if len(content) > size and len(content) <= limit:
            # A file grew between stat and read. Keep the same bound for the remainder.
            content += stream.read(limit + 1 - len(content))
    return content if len(content) <= limit else None


@lru_cache(maxsize=1)
def tree_sitter_versions() -> dict[str, str]:
    """Snapshot installed Tree-sitter runtime and grammar package versions."""
    installed = {}
    for distribution in distributions():
        name = (distribution.metadata.get("Name") or "").casefold().replace("_", "-")
        if name == "tree-sitter" or name.startswith("tree-sitter-"):
            installed.setdefault(name, distribution.version)
    return installed


def normalize_path(path: str | Path) -> str:
    """Return an absolute normalized path string with POSIX separators."""
    return Path(path).expanduser().resolve(strict=False).as_posix()


def normalize_relative_lookup_path(path: str | Path) -> str:
    """Normalize a project-relative lookup path without altering dotfile names."""
    value = str(path).strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if value.startswith("/") or (len(value) >= 2 and value[1] == ":"):
        raise ValueError("path must be project-relative")
    parts = []
    for part in value.rstrip("/").split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("path must not traverse outside the project")
        parts.append(part)
    return "/".join(parts)


def relative_path_lookup_key(path: str | Path) -> str:
    return normalize_relative_lookup_path(path).casefold()


def relative_path(root: str | Path, path: str | Path) -> str:
    root_path = Path(root).expanduser().resolve(strict=False)
    target = Path(path).expanduser().resolve(strict=False)
    return target.relative_to(root_path).as_posix()


def options_hash(options: dict[str, Any] | None = None) -> str:
    payload = json.dumps(options or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def project_id(root_path: str) -> str:
    """Identify a project by its normalized filesystem root."""
    return stable_id("project", normalize_path(root_path))


def artifact_id(project_id_: str, relative_path_: str) -> str:
    """Identify an artifact within its owning project."""
    return stable_id("artifact", project_id_, relative_path_)
