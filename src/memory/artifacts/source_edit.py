"""Apply line replacements against a verified source revision."""
from __future__ import annotations

from codecs import BOM_UTF8, BOM_UTF16_BE
from collections.abc import Collection
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
from typing import Any, TypedDict

from ..storage import StoreLease
from .fingerprint import normalize_relative_lookup_path, project_id
from .revision import ProjectRevision
from .scanner import fingerprint_file
from .text_encoding import TEXT_SAMPLE_BYTES, utf16_encoding


class SourceEditResult(TypedDict):
    changed: list[str]
    unchanged: list[str]


@dataclass(frozen=True, slots=True)
class LineEdit:
    """An original, one-based inclusive range or an insertion before start."""

    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class AnchoredEdit:
    """Replace a unique block of complete lines from the original file."""

    find: str
    text: str


def _plan(value: Any) -> dict[str, list[LineEdit | AnchoredEdit]]:
    if not isinstance(value, list) or not value:
        raise ValueError("An edit plan must contain one or more line edits in a JSON array")
    result: dict[str, list[LineEdit | AnchoredEdit]] = {}
    for item in value:
        if not isinstance(item, dict) or item.keys() not in (
            {"path", "start_line", "end_line", "text"}, {"path", "find", "text"},
        ):
            raise ValueError("Each edit needs path and text, with either start_line and end_line or find")
        path, text = item["path"], item["text"]
        if (not isinstance(path, str) or not path or not isinstance(text, str) or "\x00" in text or "\r" in text):
            raise ValueError("Use a project-relative path, valid inclusive lines and LF-separated text")
        if normalize_relative_lookup_path(path) != path:
            raise ValueError(f"Use the exact indexed project-relative path: {path}")
        edit: LineEdit | AnchoredEdit
        if "find" in item:
            anchor = item["find"]
            if not isinstance(anchor, str) or not anchor.strip() or "\x00" in anchor or "\r" in anchor:
                raise ValueError("Find needs a nonblank block of exact LF-separated lines")
            edit = AnchoredEdit(anchor.removesuffix("\n"), text)
        else:
            start, end = item["start_line"], item["end_line"]
            if type(start) is not int or type(end) is not int or not 1 <= start <= end + 1:
                raise ValueError("Use a project-relative path, valid inclusive lines and LF-separated text")
            edit = LineEdit(start, end, text)
        result.setdefault(path, []).append(edit)
    return result


def _ending(line: str) -> str:
    """Keep the separators recognized by the source excerpt's splitlines."""
    if line.endswith("\r\n"):
        return "\r\n"
    if line and line[-1] in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029":
        return line[-1]
    return ""


def _anchored_range(edit: AnchoredEdit, lines: str, path: str) -> LineEdit:
    """Resolve full-line boundaries and refuse multiple occurrences, including overlapping ones."""
    needle = "\n" + edit.find + "\n"
    offset = lines.find(needle)
    if offset < 0:
        raise ValueError(f"Source edit anchor was not found in {path}. Copy exact complete lines or use a line range.")
    start = lines.count("\n", 0, offset) + 1
    second = lines.find(needle, offset + 1)
    if second >= 0:
        other = lines.count("\n", 0, second) + 1
        raise ValueError(f"Source edit anchor is ambiguous in {path} at lines {start} and {other}. "
                         "Include more surrounding lines or use a line range.")
    return LineEdit(start, start + edit.find.count("\n"), edit.text)


def _splice(raw: bytes, edits: list[LineEdit | AnchoredEdit], path: str) -> bytes:
    encoding = utf16_encoding(raw[:TEXT_SAMPLE_BYTES]) or "utf-8"
    bom = b""
    if encoding == "utf-16":
        bom = raw[:2]
        encoding = "utf-16-be" if bom == BOM_UTF16_BE else "utf-16-le"
    elif encoding == "utf-8" and raw.startswith(BOM_UTF8):
        bom = BOM_UTF8
    try:
        source = raw[len(bom):].decode(encoding)
    except UnicodeError as exc:
        raise ValueError(f"Source edit requires UTF-8 or UTF-16: {path}") from exc
    if "\x00" in source:
        raise ValueError(f"Source edit requires text: {path}")
    lines = source.splitlines(keepends=True)
    normalized = "\n" + "\n".join(source.splitlines()) + "\n" if any(isinstance(edit, AnchoredEdit) for edit in edits) else ""
    ordered = sorted(
        (edit if isinstance(edit, LineEdit) else _anchored_range(edit, normalized, path) for edit in edits),
        key=lambda item: item.start,
    )
    previous_end, previous_start = 0, 0
    for edit in ordered:
        if edit.end > len(lines) or edit.start > len(lines) + 1:
            raise ValueError(f"Source edit range is outside the file: {path}")
        if edit.start <= previous_end or edit.start == previous_start:
            raise ValueError(f"Source edit ranges overlap: {path}")
        previous_end, previous_start = edit.end, edit.start
    # Work backwards so every range stays relative to the original source.
    default_ending = next((eol for line in lines if (eol := _ending(line))), "\n")
    has_final_ending = not lines or bool(_ending(lines[-1]))
    for edit in reversed(ordered):
        start, end = edit.start - 1, edit.end
        replacement = edit.text.split("\n")
        if replacement[-1] == "":
            replacement.pop()
        original_endings = [_ending(line) for line in lines[start:end]]
        neighbors = lines[start:start + 1] or lines[max(0, start - 1):start]
        local_ending = next((ending for ending in original_endings if ending),
                            (_ending(neighbors[0]) if neighbors else "") or default_ending)
        rendered = [line + ((original_endings[i] if i < len(original_endings) else "") or local_ending)
                    for i, line in enumerate(replacement)]
        if start == len(lines) and lines and rendered and not _ending(lines[-1]):
            lines[-1] += local_ending
        if rendered and end == len(lines) and not has_final_ending:
            rendered[-1] = replacement[-1]
        lines[start:end] = rendered
    return bom + "".join(lines).encode(encoding)


def apply_source_edits(root: Path, revision: ProjectRevision, value: Any, *,
                       allowed_paths: Collection[str] | None = None) -> SourceEditResult:
    """Validate the entire plan, then replace files under a workspace edit lease."""
    root = root.resolve()
    if revision.project_id != project_id(str(root)):
        raise ValueError("The source revision belongs to a different workspace")
    plan = _plan(value)
    if allowed_paths is not None and (unassigned := plan.keys() - set(allowed_paths)):
        raise ValueError(f"Source edit is outside the assigned files: {', '.join(sorted(unassigned))}")
    private = root / ".reql"
    if private.resolve() != private:
        raise ValueError("The workspace edit directory must not be redirected")
    private.mkdir(exist_ok=True)
    changed: list[str] = []
    unchanged: list[str] = []

    def indexed_path(relative: str) -> Path:
        path = root / relative
        if relative not in revision.manifest or path.resolve() != path or not path.is_file():
            raise ValueError(f"Source edit needs a regular indexed file in the workspace: {relative}")
        return path

    def verify_digest(relative: str, digest: str | None) -> None:
        if digest != revision.manifest[relative]["sha256"]:
            raise ValueError(f"Source changed since compilation: {relative}")

    try:
        with (
            StoreLease(private / "source-edit", blocking=True),
            TemporaryDirectory(dir=private) as folder,
        ):
            staged = []
            for relative, edits in plan.items():
                path = indexed_path(relative)
                raw = path.read_bytes()
                verify_digest(relative, sha256(raw).hexdigest())
                updated = _splice(raw, edits, relative)
                if updated == raw:
                    unchanged.append(relative)
                    continue
                temporary = Path(folder) / str(len(staged))
                temporary.write_bytes(updated)
                temporary.chmod(S_IMODE(path.stat().st_mode))
                staged.append((relative, path, temporary))
            for relative, path, temporary in staged:
                _, digest, _ = fingerprint_file(indexed_path(relative))
                verify_digest(relative, digest)
                os.replace(temporary, path)
                changed.append(relative)
    except (OSError, ValueError) as exc:
        if changed:
            raise ValueError(f"{exc}; already edited: {', '.join(changed)}") from exc
        if isinstance(exc, OSError):
            raise ValueError(f"Cannot apply source edits: {exc}") from exc
        raise
    return {"changed": changed, "unchanged": unchanged}
