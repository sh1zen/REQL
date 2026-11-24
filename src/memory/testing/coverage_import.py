"""Read coverage.py JSON contexts without loading or running covered code."""
from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
from typing import Any

from .models import LineObservation

_NODE_ID = re.compile(r"(?P<path>(?:[A-Za-z]:[\\/])?[^|:\r\n]+)::(?P<name>\w+(?:::\w+)*)(?:\[[^\r\n]*\])?")


def local_path(root: Path, value: str | Path) -> Path:
    """Confine inputs and reported paths to the registered project."""
    if "\0" in str(value):
        raise ValueError("Path cannot contain NUL bytes")
    normalized = str(value).replace("\\", "/")
    if PureWindowsPath(normalized).drive and not Path(normalized).is_absolute():
        raise ValueError("Coverage paths must be within the registered project")
    path = (root / normalized).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Coverage paths must be within the registered project")
    return path


def read_coverage_json(root: Path, input_path: str | Path) -> tuple[dict[str, Any], str]:
    path = local_path(root, input_path)
    if not path.is_file():
        raise ValueError("Coverage input must be a regular project-local file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Cannot read coverage input {path.relative_to(root)}: {exc.strerror}") from exc
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ValueError("Coverage JSON must contain a files object")
    return data, hashlib.sha256(raw).hexdigest()


def observations(root: Path, report: dict[str, Any]) -> Iterator[LineObservation]:
    """Resolve per-test contexts before verifying them against compiled symbols."""
    metadata = report.get("reql", {})
    addresses = metadata.get("test_contexts", {}) if isinstance(metadata, dict) else None
    if not isinstance(addresses, dict):
        raise ValueError("reql.test_contexts must map context strings to test paths and names")
    explicit: dict[str, tuple[str, str]] = {}
    for context, address in addresses.items():
        if (not context or not isinstance(address, dict) or set(address) != {"path", "name"}
                or not all(isinstance(value, str) and value.strip() for value in address.values())):
            raise ValueError("Each reql.test_contexts entry requires a non-empty path and name")
        explicit[context] = local_path(root, address["path"]).relative_to(root).as_posix(), address["name"]

    identities: dict[str, tuple[str, str] | None] = dict(explicit)
    for filename, entry in report["files"].items():
        path = local_path(root, filename).relative_to(root).as_posix()
        if not isinstance(entry, dict) or not isinstance(entry.get("contexts", {}), dict):
            raise ValueError(f"Invalid coverage contexts for {path}")
        executed = entry.get("executed_lines", [])
        if not isinstance(executed, list) or any(type(line) is not int or line < 1 for line in executed):
            raise ValueError(f"Invalid executed lines for {path}")
        executed_lines = set(executed)
        for raw_line, contexts in entry.get("contexts", {}).items():
            if (not raw_line.isascii() or not raw_line.isdecimal() or len(raw_line) > 9
                    or not isinstance(contexts, list) or not all(isinstance(context, str) for context in contexts)):
                raise ValueError(f"Invalid per-line coverage contexts for {path}")
            line = int(raw_line)
            if line not in executed_lines:
                continue
            for context in contexts:
                if context.endswith(("|setup", "|teardown")):
                    continue
                if context not in identities:
                    test = _NODE_ID.fullmatch(context.removesuffix("|run"))
                    identities[context] = (
                        (local_path(root, test["path"]).relative_to(root).as_posix(), test["name"].replace("::", "."))
                        if test is not None else None
                    )
                identity = identities[context]
                if identity is not None:
                    yield LineObservation(*identity, path, line, context)
