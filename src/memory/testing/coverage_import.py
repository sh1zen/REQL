"""Read coverage.py JSON contexts without loading or running covered code."""
from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from ..artifacts.fingerprint import read_bounded_file
from .models import LineObservation

MAX_REPORT_BYTES = 32 * 1024 * 1024
_PYTEST_ID = re.compile(r"(?P<path>[^|:]+\.py)::(?P<name>(?:[A-Za-z_]\w*::)*test\w*)(?:\[[^\r\n]*\])?")


def local_path(root: Path, value: str | Path) -> Path:
    """Confine inputs and reported paths to the registered project."""
    if "\0" in str(value):
        raise ValueError("Path cannot contain NUL bytes")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Coverage paths must be within the registered project")
    return path


def read_coverage_json(root: Path, input_path: str | Path) -> tuple[dict[str, Any], str]:
    path = local_path(root, input_path)
    if not path.is_file():
        raise ValueError("Coverage input must be a regular project-local file")
    try:
        raw = read_bounded_file(path, MAX_REPORT_BYTES)
    except OSError as exc:
        raise ValueError(f"Cannot read coverage input {path.relative_to(root)}: {exc.strerror}") from exc
    if raw is None:
        raise ValueError("Coverage input exceeds the 32 MiB limit")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ValueError("Coverage JSON must contain a files object")
    return data, hashlib.sha256(raw).hexdigest()


def observations(root: Path, report: dict[str, Any]) -> Iterator[LineObservation]:
    """Accept pytest node IDs and pytest-cov's run phase as per-test identities."""
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
                test = _PYTEST_ID.fullmatch(context.removesuffix("|run"))
                if test is None:
                    continue
                test_path = local_path(root, test["path"]).relative_to(root).as_posix()
                yield LineObservation(test_path, test["name"].replace("::", "."), path, line, context)
