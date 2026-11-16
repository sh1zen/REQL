"""Capture bounded source spans from files that match a committed graph revision."""
from __future__ import annotations

from codecs import BOM_UTF8
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Sequence

from ....artifacts.revision import ProjectRevision, RevisionRepository
from ....artifacts.fingerprint import read_bounded_file
from ....document_ingestion.formats.markdown import FENCE_RE, HEADING_RE
from ....domain.models import MemoryNode
from ....storage.graph_store import GraphStore


MAX_SOURCE_FILES = 3
MAX_SOURCE_REGIONS = 6
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_LINES = 120
MAX_SOURCE_CHARS = 6000
_BRIEF_SOURCE_LINES = 32
_BRIEF_SOURCE_CHARS = 1600
_MARKDOWN_SECTION_REF = re.compile(r"(?:^|[\s`(])([\w./-]+\.md)#([a-z0-9_-]+)(?![\w#/%?=&-])")
_MAX_SECTION_HEADINGS = 128
_TASK_GUIDANCE_LINES = 24
_TASK_GUIDANCE_CHARS = 1400
_TASK_SPAN = re.compile(r"`([^`#\r\n]+)#L([1-9][0-9]{0,7})-L([1-9][0-9]{0,7})@([0-9a-f]{64})`")
_MAX_TASK_SPANS = 4
_TASK_SPAN_LINES = 60
_TASK_SPAN_CHARS = 2600
_TASK_FILE_LINES = 90
_TASK_FILE_BYTES = 4600
_QUALIFIED_TASK_SYMBOL = re.compile(r"\b[A-Za-z_]\w*(?:(?:\.|::)[A-Za-z_]\w*)+\b")
_TASK_DEFINITION_LIMIT = 128
_TASK_REFERENCE_LIMIT = 24


def _read_bounded_source(path: Path) -> bytes | None:
    """Share one byte ceiling for verified excerpts and ordinary source previews."""
    return read_bounded_file(path, MAX_SOURCE_BYTES)


def capture_source_excerpts(
    store: GraphStore,
    reads: Sequence[dict[str, Any]],
    *,
    complete_only: bool = False,
    snapshots: dict[str, dict[str, Any]] | None = None,
    revisions: dict[str, ProjectRevision | None] | None = None,
    shown: Sequence[dict[str, Any]] = (),
    new_file_budget: int | None = None,
) -> list[dict[str, Any]]:
    """Read each selected file once, merge overlaps, and expose exact coverage or failure."""
    regions: list[dict[str, Any]] = []
    for item in reads:
        start, end = item.get("line_start"), item.get("line_end")
        if type(start) is not int or type(end) is not int or start < 1 or end < start:
            continue
        node = store.get_node(str(item.get("node_id") or ""), clone=False)
        if node is None:
            continue
        artifact_id = str(node.properties.get("artifact_id") or "")
        # Only merge overlapping spans. Do not include unrequested intervening source.
        overlapping = [row for row in regions if artifact_id and row["artifact_id"] == artifact_id
                       and row["line_start"] <= end and row["line_end"] >= start]
        if overlapping:
            first = overlapping[0]
            first["line_start"] = min(start, *(row["line_start"] for row in overlapping))
            first["line_end"] = max(end, *(row["line_end"] for row in overlapping))
            for row in overlapping[1:]:
                regions.remove(row)
        else:
            regions.append({"path": item.get("path"), "line_start": start, "line_end": end,
                            "node_id": node.id, "artifact_id": artifact_id, "type": node.type})

    snapshots = {} if snapshots is None else snapshots
    file_limit = MAX_SOURCE_FILES if new_file_budget is None else len(snapshots) + new_file_budget
    revisions = {} if revisions is None else revisions
    result: list[dict[str, Any]] = []
    # A brief preview is useful only if it replaces an entire owner read.
    lines_left, chars_left = (_BRIEF_SOURCE_LINES, _BRIEF_SOURCE_CHARS) if complete_only else (MAX_SOURCE_LINES, MAX_SOURCE_CHARS)
    for item in shown:
        if item.get("source_status") == "verified" and "text" in item:
            lines_left -= item["line_end"] - item["line_start"] + 1
            chars_left -= len(item["text"])
    for region in regions[:MAX_SOURCE_REGIONS]:
        artifact_id = region["artifact_id"]
        if artifact_id not in snapshots:
            if len(snapshots) >= file_limit:
                if new_file_budget is None:
                    break
                continue
            artifact = store.get_node(artifact_id, clone=False) if artifact_id else None
            project_id = str(artifact.properties.get("project_id") or "") if artifact else ""
            if project_id not in revisions:
                revisions[project_id] = RevisionRepository(store).latest(project_id) if project_id else None
            snapshots[artifact_id] = _source_snapshot(store, artifact, revisions[project_id])
        snapshot = snapshots[artifact_id]
        row = {**region, "source": "disk", "source_status": snapshot["status"]}
        if snapshot["status"] != "verified":
            row["reason"] = snapshot["reason"]
            result.append(row)
            continue
        if lines_left <= 0 or chars_left <= 0:
            break
        lines = snapshot["lines"]
        start, end = region["line_start"], min(region["line_end"], len(lines))
        selected: list[str] = []
        used_chars = 0
        for line in lines[start - 1:end][:lines_left]:
            cost = len(line) + bool(selected)
            if used_chars + cost > chars_left:
                break
            selected.append(line)
            used_chars += cost
        if complete_only and len(selected) < region["line_end"] - start + 1:
            continue
        if not selected:
            row.update(source_status="unavailable", reason="source span is outside the file or exceeds the remaining budget")
        else:
            actual_end = start + len(selected) - 1
            row.update(line_end=actual_end, requested_line_end=region["line_end"],
                       text="\n".join(selected), sha256=snapshot["sha256"],
                       truncated=actual_end < region["line_end"])
            if row["truncated"]:
                row["next_line"] = actual_end + 1
            lines_left -= len(selected)
            chars_left -= used_chars
        result.append(row)
    return result


def _source_snapshot(store: GraphStore, artifact: MemoryNode | None, revision: ProjectRevision | None) -> dict[str, Any]:
    """Check project confinement and committed fingerprints before exposing file text."""
    unavailable = {"status": "unavailable", "reason": "no committed source metadata for this span"}
    if artifact is None or artifact.type != "SourceArtifact" or artifact.status != "active" or revision is None:
        return unavailable
    props = artifact.properties
    relative = str(props.get("relative_path") or "").replace("\\", "/")
    entry = revision.manifest.get(relative)
    compiled_at = props.get("last_compiled_at")
    if (not entry or entry.get("artifact_id") != artifact.id or not compiled_at
            or not props.get("compiled_sha256") or props["compiled_sha256"] != props.get("sha256")
            or entry.get("sha256") != props.get("sha256")):
        return {"status": "stale", "reason": "source metadata does not match the committed revision; refresh the graph"}
    project = store.get_node(str(props.get("project_id") or ""), clone=False)
    root_path = project.properties.get("root_path") if project is not None and project.type == "Project" else None
    if not isinstance(root_path, str) or not root_path or not Path(root_path).is_absolute():
        return unavailable
    if not relative or PureWindowsPath(relative).drive or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
        return {"status": "unavailable", "reason": "source path is outside the project"}
    try:
        root = Path(root_path).resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            return {"status": "unavailable", "reason": "source path is outside the project"}
        if not path.is_file():
            return {"status": "stale", "reason": "indexed source file is missing or is no longer a regular file"}
        content = _read_bounded_source(path)
        if content is None:
            return {"status": "unavailable", "reason": "source file exceeds the 4 MiB read limit"}
        digest = hashlib.sha256(content).hexdigest()
        if digest != entry["sha256"]:
            return {"status": "stale", "reason": "source changed after indexing; graph spans require a refresh"}
        lines = content.decode("utf-8-sig").splitlines()
        crlf = content.count(b"\r\n")
        endings = [name for name, count in (
            ("CRLF", crlf), ("LF", content.count(b"\n") - crlf), ("CR", content.count(b"\r") - crlf),
        ) if count]
        return {"status": "verified", "sha256": digest, "lines": lines,
                "encoding": "UTF-8 BOM" if content.startswith(BOM_UTF8) else "UTF-8",
                "line_endings": endings[0] if len(endings) == 1 else "mixed" if endings else "none"}
    except (OSError, UnicodeError, ValueError) as exc:
        return {"status": "unavailable", "reason": f"cannot read source: {type(exc).__name__}"}


def _current_headings(store: GraphStore, artifact: MemoryNode, digest: str) -> list[MemoryNode]:
    """Read bounded heading coordinates tied to the verified Markdown source."""
    edges = store.get_edges(from_id=artifact.id, type_="HAS_SECTION", limit=_MAX_SECTION_HEADINGS + 1, clone=False)
    if len(edges) > _MAX_SECTION_HEADINGS:
        return []
    return sorted(
        (node for node in store.get_nodes([edge.to_id for edge in edges], clone=False)
         if node.status == "active" and node.properties.get("artifact_id") == artifact.id
         and node.properties.get("artifact_sha256") == digest
         and type(node.properties.get("start_line")) is int and type(node.properties.get("level")) is int),
        key=lambda node: node.properties["start_line"],
    )


def _section_end(headings: Sequence[MemoryNode], heading: MemoryNode, file_end: int) -> int:
    """Stop a Markdown section before the next heading at the same or higher level."""
    start, level = heading.properties["start_line"], heading.properties["level"]
    next_heading = next((node.properties["start_line"] - 1 for node in headings
                         if node.properties["start_line"] > start and node.properties["level"] <= level), file_end)
    return min(next_heading, file_end)


def _short_pinned_section(
    store: GraphStore, artifact: MemoryNode, digest: str, lines: Sequence[str], start: int, end: int,
) -> int:
    """Complete one heading or its adjacent fenced example from verified source."""
    heading_line = end
    while heading_line >= start and not lines[heading_line - 1].strip():
        heading_line -= 1
    if heading_line < start or HEADING_RE.match(lines[heading_line - 1]) is None:
        return end
    headings = _current_headings(store, artifact, digest)
    heading = next((node for node in headings if node.properties["start_line"] == heading_line), None)
    if heading is not None:
        stop = _section_end(headings, heading, len(lines))
        while stop > end and not lines[stop - 1].strip():
            stop -= 1
        if 0 < stop - end <= _TASK_GUIDANCE_LINES:
            return stop
    # Explicit task excerpts can include a nearby fenced example even if
    # general document indexing is disabled.
    ceiling = min(end + _TASK_GUIDANCE_LINES, len(lines))
    first = next((index for index in range(end, ceiling) if lines[index].strip()), None)
    if first is None:
        return end
    fence = FENCE_RE.match(lines[first])
    if fence is None or fence.group(2) is None:
        return end
    return next((index + 1 for index in range(first + 1, ceiling)
                 if lines[index].startswith(fence.group(1)[:3])), end)


def locate_linked_section(
    store: GraphStore,
    owners: Sequence[dict[str, Any]],
    *,
    snapshots: dict[str, dict[str, Any]],
    revisions: dict[str, ProjectRevision | None],
) -> dict[str, Any] | None:
    """Locate one explicitly named Markdown section in the owner's verified revision."""
    for owner in owners:
        if owner["source_status"] != "verified" or owner["truncated"]:
            continue
        # Only project-relative links in verified bodies can select documentation.
        reference = _MARKDOWN_SECTION_REF.search(owner["text"])
        if reference is None:
            continue
        path, anchor = reference.groups()
        source = store.get_node(owner["artifact_id"], clone=False)
        project_id = source.properties.get("project_id") if source else None
        revision = revisions.get(project_id) if project_id else None
        entry = revision.manifest.get(path) if revision else None
        if not entry:
            continue
        artifact = store.get_node(entry["artifact_id"], clone=False)
        if (artifact is None or artifact.properties.get("artifact_type") != "markdown"
                or artifact.properties.get("project_id") != project_id
                or artifact.properties.get("relative_path") != path):
            continue
        headings = _current_headings(store, artifact, entry["sha256"])
        matching = [node for node in headings
                    if re.sub(r"[^\w -]", "", node.text.casefold()).replace(" ", "-") == anchor]
        if len(matching) != 1:
            continue
        heading = matching[0]
        start = heading.properties["start_line"]
        if artifact.id not in snapshots:
            if len(snapshots) >= MAX_SOURCE_FILES:
                continue
            snapshots[artifact.id] = _source_snapshot(store, artifact, revision)
        snapshot = snapshots[artifact.id]
        if snapshot["status"] != "verified":
            continue
        lines = snapshot["lines"]
        end = _section_end(headings, heading, len(lines))
        if not 1 <= start <= end <= len(lines):
            continue
        while end > start and not lines[end - 1].strip():
            end -= 1
        return {"path": path, "node_id": heading.id, "line_start": start, "line_end": end,
                "sha256": snapshot["sha256"], "source_status": "verified"}
    return None


def capture_task_test_file(
    store: GraphStore,
    paths: Sequence[str],
    revision: ProjectRevision,
    snapshots: dict[str, dict[str, Any]],
    *,
    shown_paths: set[str],
    extra_file_budget: int = 0,
    require_test_scope: bool = True,
) -> dict[str, Any] | None:
    """Reuse one small test file identified by the assignment or a saved check."""
    file_limit = max(MAX_SOURCE_FILES, len(snapshots) + extra_file_budget)
    for path in paths:
        entry = revision.manifest.get(path)
        if path in shown_paths or not entry:
            continue
        artifact_id = entry["artifact_id"]
        artifact = store.get_node(artifact_id, clone=False)
        if (artifact is None or (require_test_scope and artifact.properties.get("context_scope") != "test")
                or artifact.properties.get("size_bytes", _TASK_FILE_BYTES + 1) > _TASK_FILE_BYTES):
            continue
        if artifact_id not in snapshots:
            if len(snapshots) >= file_limit:
                return None
            snapshots[artifact_id] = _source_snapshot(store, artifact, revision)
        if whole := _whole_task_file(path, artifact_id, snapshots[artifact_id]):
            return whole
    return None


def capture_assigned_documentation(
    store: GraphStore,
    paths: Sequence[str],
    task: str,
    revision: ProjectRevision,
    snapshots: dict[str, dict[str, Any]],
    *,
    shown: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Read one unambiguous prose paragraph referring to a qualified task symbol."""
    symbols = set(_QUALIFIED_TASK_SYMBOL.findall(task))
    names = symbols | {re.split(r"\.|::", symbol)[-1] for symbol in symbols}
    if not names:
        return None
    documents = [path for path in dict.fromkeys(paths) if path in revision.manifest
                 and PurePosixPath(path).suffix.casefold() in {".md", ".markdown"}]
    if len(documents) != 1:
        return None
    path = documents[0]
    artifact_id = revision.manifest[path]["artifact_id"]
    if artifact_id not in snapshots:
        artifact = store.get_node(artifact_id, clone=False)
        snapshots[artifact_id] = _source_snapshot(store, artifact, revision)
    snapshot = snapshots[artifact_id]
    if snapshot["status"] != "verified":
        return None
    lines = snapshot["lines"]
    reference = re.compile(r"`(?:[^\s`]+ )*(?:" + "|".join(map(re.escape, sorted(names))) + r")`")
    span = None
    fence = ""
    start = 0
    matched = False
    for index in range(len(lines) + 1):
        line = lines[index] if index < len(lines) else ""
        if marker := FENCE_RE.match(line):
            if not fence:
                fence = marker[1]
            elif marker[1].startswith(fence) and marker[2] is None:
                fence = ""
        if marker or fence or not line.strip() or HEADING_RE.match(line):
            if matched:
                if span is not None:
                    return None
                span = (start, index)
            matched = False
            start = index + 1
        elif reference.search(line):
            matched = True
    if span is None:
        return None
    start, end = span
    # An adjacent short example and its first explanation form a single passage.
    example = next((index for index in range(end, min(end + 3, len(lines))) if lines[index].strip()), None)
    if example is not None and (opening := FENCE_RE.match(lines[example])):
        stop = next((index + 1 for index in range(example + 1, min(start + _TASK_GUIDANCE_LINES, len(lines)))
                     if lines[index].strip() == opening[1]), None)
        if stop is not None:
            end = stop
            if end + 1 < len(lines) and not lines[end].strip():
                paragraph = end + 1
                while paragraph < len(lines) and lines[paragraph].strip() and not (
                    HEADING_RE.match(lines[paragraph]) or FENCE_RE.match(lines[paragraph])
                ):
                    paragraph += 1
                if paragraph <= start + _TASK_GUIDANCE_LINES:
                    end = paragraph
    text = "\n".join(lines[start:end])
    if (end - start > _TASK_GUIDANCE_LINES or len(text) > _TASK_GUIDANCE_CHARS
            or _covers_verified_span(shown, path, start + 1, end, snapshot["sha256"])):
        return None
    return {"path": path, "node_id": artifact_id, "artifact_id": artifact_id,
            "line_start": start + 1, "line_end": end, "source_status": "verified", "sha256": snapshot["sha256"],
            "encoding": snapshot["encoding"], "line_endings": snapshot["line_endings"], "truncated": False, "text": text}


def assigned_document_outline(
    store: GraphStore, paths: Sequence[str], revision: ProjectRevision, snapshots: dict[str, dict[str, Any]],
    *, shown: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Offer bounded, verified locations when an assignment has no precise document passage."""
    documents = [path for path in dict.fromkeys(paths) if path in revision.manifest
                 and PurePosixPath(path).suffix.casefold() in {".md", ".markdown"}]
    if len(documents) != 1:
        return None
    path = documents[0]
    if any(row["path"] == path and row.get("source_status") == "verified" and "text" in row for row in shown):
        return None
    indexed = _verified_document_headings(store, path, revision, snapshots)
    if indexed is None:
        return None
    headings, snapshot = indexed
    depth = min((node.properties["level"] for node in headings if node.properties["level"] > 1), default=1)
    sections = [node for node in headings if node.properties["level"] == depth]
    visible = sections if len(sections) <= 8 else sections[:4] + sections[-4:]
    return {"path": path, "source_status": "verified", "sha256": snapshot["sha256"],
            "omitted": len(sections) - len(visible),
            "headings": [{"title": " ".join(node.text.split())[:100], "line_start": node.properties["start_line"],
                          "line_end": _section_end(headings, node, len(snapshot["lines"]))} for node in visible]}


def readme_setup_location(
    store: GraphStore, revision: ProjectRevision, snapshots: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Point to an unambiguous setup section without adding its body to the task brief."""
    path = "README.md"
    indexed = _verified_document_headings(store, path, revision, snapshots, max_files=len(snapshots) + 1)
    if indexed is None:
        return None
    headings, snapshot = indexed
    for title in ("quick start", "getting started", "development setup", "setup", "installation"):
        matches = [node for node in headings if " ".join(node.text.casefold().split()) == title]
        if len(matches) == 1:
            heading = matches[0]
            return {"path": path, "title": " ".join(heading.text.split()), "line_start": heading.properties["start_line"],
                    "line_end": _section_end(headings, heading, len(snapshot["lines"])),
                    "source_status": "verified", "sha256": snapshot["sha256"]}
    return None


def _verified_document_headings(
    store: GraphStore, path: str, revision: ProjectRevision, snapshots: dict[str, dict[str, Any]],
    *, max_files: int = MAX_SOURCE_FILES,
) -> tuple[list[MemoryNode], dict[str, Any]] | None:
    """Read indexed sections from a single current, hash-verified Markdown file."""
    entry = revision.manifest.get(path)
    if entry is None:
        return None
    artifact = store.get_node(entry["artifact_id"], clone=False)
    if artifact is None or not (headings := _current_headings(store, artifact, entry["sha256"])):
        return None
    if artifact.id not in snapshots:
        if len(snapshots) >= max_files:
            return None
        snapshots[artifact.id] = _source_snapshot(store, artifact, revision)
    snapshot = snapshots[artifact.id]
    return (headings, snapshot) if snapshot["status"] == "verified" else None


def _whole_task_file(path: str, artifact_id: str, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Construct a complete file excerpt using an existing verified snapshot."""
    if snapshot["status"] != "verified" or not 1 <= len(snapshot["lines"]) <= _TASK_FILE_LINES:
        return None
    return {"path": path, "node_id": artifact_id, "artifact_id": artifact_id,
            "line_start": 1, "line_end": len(snapshot["lines"]), "whole_file": True,
            "source_status": "verified", "sha256": snapshot["sha256"], "truncated": False,
            "encoding": snapshot["encoding"], "line_endings": snapshot["line_endings"],
            "text": "\n".join(snapshot["lines"])}


def complete_pinned_task_file(
    store: GraphStore, revision: ProjectRevision, snapshots: dict[str, dict[str, Any]],
    spans: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Fill one small production file when pinned ranges already cover much of it."""
    paths = dict.fromkeys(row["path"] for row in spans if row.get("source_status") == "verified" and "text" in row)
    for path in paths:
        artifact_id = revision.manifest[path]["artifact_id"]
        artifact = store.get_node(artifact_id, clone=False)
        if (artifact is None or artifact.properties.get("artifact_type") != "code"
                or artifact.properties.get("context_scope") == "test"
                or artifact.properties.get("size_bytes", _TASK_FILE_BYTES + 1) > _TASK_FILE_BYTES):
            continue
        whole = _whole_task_file(path, artifact_id, snapshots[artifact_id])
        if whole is None:
            continue
        covered = {number for row in spans if row["path"] == path and row.get("sha256") == whole["sha256"]
                   for number in range(row["line_start"], row["line_end"] + 1)}
        if (len(covered) < _BRIEF_SOURCE_LINES or len(covered) == whole["line_end"]
                or len(covered) * 3 < whole["line_end"]):
            continue
        added = sum(len(line) + 1 for number, line in enumerate(snapshots[artifact_id]["lines"], 1)
                    if number not in covered)
        if added <= _BRIEF_SOURCE_CHARS:
            return whole
    return None


def pinned_code_context(
    store: GraphStore,
    revision: ProjectRevision,
    snapshots: dict[str, dict[str, Any]],
    spans: Sequence[dict[str, Any]],
    instructions: str,
    *,
    shown: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Complete a short owner and recover declarations connected to verified edit pins."""
    completed = None
    reads: list[dict[str, Any]] = []
    loaded: dict[str, list[MemoryNode]] = {}
    mentioned = set(re.findall(r"\b[A-Za-z_]\w*\b", instructions))
    for row in uncovered_source_rows(spans, shown):
        if row.get("source_status") != "verified":
            continue
        path = row["path"]
        artifact_id = revision.manifest[path]["artifact_id"]
        if artifact_id not in loaded:
            artifact = store.get_node(artifact_id, clone=False)
            if artifact is None or artifact.properties.get("context_scope") != "code":
                loaded[artifact_id] = []
            else:
                definitions = store.get_edges(from_id=artifact_id, type_="DEFINES",
                                              limit=_TASK_DEFINITION_LIMIT + 1, clone=False)
                loaded[artifact_id] = [] if len(definitions) > _TASK_DEFINITION_LIMIT else [
                    node for node in store.get_nodes([edge.to_id for edge in definitions], clone=False)
                    if _local_definition(node, artifact_id)
                ]
        definitions = loaded[artifact_id]
        first, last = row["line_start"], row["line_end"]
        owners = [node for node in definitions if node.type in {"Function", "Method"}
                  and first <= node.properties["start_line"] <= last]
        tails = [node for node in owners if node.properties["end_line"] > last
                 and (last - node.properties["start_line"] + 1) * 2 >= last - first + 1]
        if completed is None and len(tails) == 1:
            stop = tails[0].properties["end_line"]
            lines = snapshots[artifact_id]["lines"]
            if stop <= len(lines) and stop - last <= 16:
                tail = "\n".join(lines[last:stop])
                if len(tail) <= 1000:
                    completed = {**row, "line_end": stop, "node_id": tails[0].id, "artifact_id": artifact_id,
                                 "text": row["text"] + "\n" + tail}
        if len(reads) < 2 and owners:
            shown_now = (*shown, *spans, *([completed] if completed else []))
            reads.extend(_pinned_local_references(store, definitions, owners, row, mentioned, shown_now, reads))
    return completed, reads


def _local_definition(node: MemoryNode, artifact_id: str) -> bool:
    """Limit pinned context to unambiguous indexed definitions with usable coordinates."""
    props = node.properties
    start, end = props.get("start_line"), props.get("end_line")
    return (node.status == "active" and props.get("artifact_id") == artifact_id
            and node.type in {"Function", "Method", "Class", "Interface"}
            and not props.get("ambiguous_declaration")
            and type(start) is int and type(end) is int and 0 < start <= end)


def _pinned_local_references(
    store: GraphStore,
    definitions: Sequence[MemoryNode],
    owners: Sequence[MemoryNode],
    pin: dict[str, Any],
    mentioned: set[str],
    shown: Sequence[dict[str, Any]],
    selected: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prefer resolved local types, then short sibling methods named in the assignment."""
    types = {node.id: node for node in definitions if node.type in {"Class", "Interface"}}
    candidates: dict[str, MemoryNode] = {}
    if types and len(owners) <= 8:
        for owner in sorted(owners, key=lambda node: node.properties["start_line"]):
            edges = store.get_edges(from_id=owner.id, type_="READS", limit=_TASK_REFERENCE_LIMIT + 1, clone=False)
            if len(edges) > _TASK_REFERENCE_LIMIT:
                continue
            for edge in edges:
                line = edge.properties.get("line_start")
                if (edge.to_id in types and edge.properties.get("resolver") == "symbol_table"
                        and type(line) is int and pin["line_start"] <= line <= pin["line_end"]):
                    candidates.setdefault(edge.to_id, types[edge.to_id])
    parent_names = {node.properties["parent_qualified_name"] for node in owners
                    if node.type == "Method" and node.properties.get("parent_qualified_name")}
    siblings: dict[str, list[MemoryNode]] = {}
    for node in definitions:
        props = node.properties
        if node.type == "Method" and props.get("parent_qualified_name") in parent_names and props.get("name") in mentioned:
            siblings.setdefault(props["name"], []).append(node)
    for matches in siblings.values():
        if len(matches) == 1:
            candidates.setdefault(matches[0].id, matches[0])
    result = []
    used = {row["node_id"] for row in selected}
    for node in sorted(candidates.values(), key=lambda candidate: (
        candidate.type == "Method", candidate.properties["start_line"], candidate.id,
    )):
        first, last = node.properties["start_line"], node.properties["end_line"]
        if (node.id not in used and last - first < _TASK_REFERENCE_LIMIT
                and (last < pin["line_start"] or first > pin["line_end"])
                and not _covers_verified_span(shown, pin["path"], first, last, pin["sha256"])):
            result.append({"path": pin["path"], "node_id": node.id, "line_start": first, "line_end": last})
            if len(result) + len(selected) == 2:
                break
    return result

def reuse_task_source(
    snippets: Sequence[dict[str, Any]],
    linked_section: dict[str, Any] | None,
    revision: ProjectRevision,
    snapshots: dict[str, dict[str, Any]],
    *,
    shown: Sequence[dict[str, Any]],
    include_guidance: bool = True,
) -> None:
    """Make already verified task source edit-ready and include short linked guidance."""
    for snippet in snippets:
        snapshot = snapshots.get(snippet.get("artifact_id", ""))
        if (snippet.get("source_status") == "verified" and snapshot
                and snippet["sha256"] == snapshot.get("sha256")):
            snippet.update(encoding=snapshot["encoding"], line_endings=snapshot["line_endings"])
    if linked_section is None or not include_guidance:
        return
    path = linked_section["path"]
    start, end, digest = (linked_section[key] for key in ("line_start", "line_end", "sha256"))
    if end - start + 1 > _TASK_GUIDANCE_LINES or _covers_verified_span(shown, path, start, end, digest):
        return
    entry = revision.manifest.get(path)
    snapshot = snapshots.get(entry["artifact_id"]) if entry else None
    if snapshot is None or snapshot.get("sha256") != digest:
        return
    section = "\n".join(snapshot["lines"][start - 1:end])
    if len(section) <= _TASK_GUIDANCE_CHARS:
        linked_section.update(text=section, truncated=False)


def brief_task_next_action(value: str, shown: Sequence[dict[str, Any]]) -> str:
    """Omit hashes from display only when the matching range is already in the response."""
    def shorten(match: re.Match[str]) -> str:
        path, first, last, digest = match.groups()
        if _covers_verified_span(shown, path, int(first), int(last), digest):
            return f"`{path}#L{first}-L{last}`"
        return match[0]

    return _TASK_SPAN.sub(shorten, value)


def _covers_verified_span(
    excerpts: Sequence[dict[str, Any]], path: str, start: int, end: int, digest: str,
) -> bool:
    """Check that a displayed verified body contains the full requested span."""
    return any(row.get("source_status") == "verified" and "text" in row
               and row["path"] == path and row.get("sha256") == digest
               and row["line_start"] <= start <= end <= row["line_end"] for row in excerpts)


def uncovered_source_rows(
    rows: Sequence[dict[str, Any]], covering: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Retain rows whose exact verified bodies are not already covered."""
    return [
        row for row in rows
        if row.get("source_status") != "verified" or "text" not in row
        or not _covers_verified_span(covering, row["path"], row["line_start"], row["line_end"], row["sha256"])
    ]


def capture_task_edit_spans(
    store: GraphStore,
    paths: Sequence[str],
    next_action: str,
    revision: ProjectRevision,
    snapshots: dict[str, dict[str, Any]],
    *,
    shown: Sequence[dict[str, Any]],
    replaceable: Sequence[dict[str, Any]] = (),
) -> tuple[list[dict[str, Any]], int]:
    """Resolve pinned ranges and reuse the budget of verified excerpts they replace."""
    allowed = set(paths)
    references = list(dict.fromkeys(ref.groups() for ref in _TASK_SPAN.finditer(next_action)
                                    if ref[1] in allowed))
    lines_left, chars_left = _TASK_SPAN_LINES, _TASK_SPAN_CHARS
    continuation_used = False
    results: list[dict[str, Any]] = []
    remaining_excerpts = list(replaceable)
    for path, first, last, expected_hash in references[:_MAX_TASK_SPANS]:
        start, end = int(first), int(last)
        row = {"path": path, "line_start": start, "line_end": end}
        entry = revision.manifest.get(path)
        if entry is None or entry["sha256"] != expected_hash:
            results.append({**row, "source_status": "stale",
                            "reason": "the saved range belongs to different source; read the current file"})
            continue
        if _covers_verified_span((*shown, *results), path, start, end, expected_hash):
            continue
        displaced = [
            excerpt for excerpt in (*remaining_excerpts, *results)
            if excerpt.get("source_status") == "verified" and "text" in excerpt
            and excerpt["path"] == path and excerpt["sha256"] == expected_hash
            and start <= excerpt["line_start"] <= excerpt["line_end"] <= end
        ]
        line_credit = sum(excerpt["line_end"] - excerpt["line_start"] + 1 for excerpt in displaced)
        char_credit = sum(len(excerpt["text"]) for excerpt in displaced)
        size = end - start + 1
        if size < 1 or size > lines_left + line_credit:
            reason = "invalid saved line range" if size < 1 else "assigned source budget exceeded"
            results.append({**row, "source_status": "unavailable", "reason": reason})
            continue
        artifact_id = entry["artifact_id"]
        if artifact_id not in snapshots:
            artifact = store.get_node(artifact_id, clone=False)
            snapshots[artifact_id] = _source_snapshot(store, artifact, revision)
        snapshot = snapshots[artifact_id]
        if snapshot["status"] != "verified":
            results.append({**row, "source_status": snapshot["status"], "reason": snapshot["reason"]})
            continue
        lines = snapshot["lines"]
        content = "\n".join(lines[start - 1:end])
        if end > len(lines) or len(content) > chars_left + char_credit:
            reason = "saved line range is outside the file" if end > len(lines) else "assigned source budget exceeded"
            results.append({**row, "source_status": "unavailable", "reason": reason})
            continue
        pinned_chars = len(content)
        if not continuation_used and path.lower().endswith((".md", ".markdown")):
            artifact = store.get_node(artifact_id, clone=False)
            if artifact is not None and artifact.properties.get("artifact_type") == "markdown":
                stop = _short_pinned_section(store, artifact, expected_hash, lines, start, end)
                extension = "\n".join(lines[end:stop])
                if extension and len(extension) <= _TASK_GUIDANCE_CHARS:
                    row["pinned_line_end"] = end
                    row["line_end"] = stop
                    content += "\n" + extension
                    continuation_used = True
        current = {**row, "source_status": "verified", "sha256": expected_hash,
                        "encoding": snapshot["encoding"], "line_endings": snapshot["line_endings"],
                        "truncated": False, "text": content}
        if displaced:
            results = uncovered_source_rows(results, (current,))
            remaining_excerpts = uncovered_source_rows(remaining_excerpts, (current,))
        results.append(current)
        lines_left += line_credit - size
        chars_left += char_credit - pinned_chars
    return results, max(0, len(references) - _MAX_TASK_SPANS)
