"""Capture source spans from files that match a committed graph revision."""
from __future__ import annotations

from codecs import BOM_UTF8
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Sequence
from unicodedata import category, normalize
from urllib.parse import unquote

from ....artifacts.revision import ProjectRevision, RevisionRepository
from ....artifacts.scanner import fingerprint_file
from ....artifacts.text_encoding import TEXT_SAMPLE_BYTES, utf16_encoding
from ....config.loader import default_config
from ....config.policies import SourceEvidencePolicy
from ....document_ingestion.formats.markdown import FENCE_RE, HEADING_RE, closes_fence
from ....domain.models import MemoryNode
from ....domain.query_context import SeenSourceRange
from ....storage.graph_store import GraphStore
from ..common import source_symbol_references


_DEFAULT_SOURCE = default_config().retrieval.source
_MARKDOWN_SECTION_REF = re.compile(
    r"(?:^|[\s`(])([^\s`()<>\[\]\"'#?=&]+\.(?i:md|markdown))#([^\s`()<>\[\]\"'#/?=&]+)(?![^\s`()<>\[\]\"'])",
)
_TASK_SPAN = re.compile(r"`([^`#\r\n]+)#L([1-9][0-9]{0,7})-L([1-9][0-9]{0,7})@([0-9a-f]{64})`")


def limit_brief_source(payload: dict[str, Any], max_chars: int) -> None:
    """Keep requested bodies first and retain verified locations when a body does not fit."""
    spent = 0
    selected: list[dict[str, Any]] = []

    def limit(rows: Sequence[dict[str, Any]]) -> None:
        nonlocal spent
        for row in rows:
            text = row.get("text")
            if not text:
                continue
            size = len(text)
            verified = row.get("source_status") == "verified" and not row.get("truncated")
            if verified and _covers_verified_span(selected, row["path"], row["line_start"], row["line_end"], row["sha256"]):
                del row["text"]
                row["body_omitted"] = True
            elif spent + size <= max_chars:
                spent += size
                if verified:
                    selected.append(row)
            else:
                del row["text"]
                row["body_omitted"] = True

    limit(payload.get("task_edit_spans", {}).get("spans", ()))
    if row := payload.get("task_source_file"):
        limit((row,))
    limit(payload.get("snippets", ()))
    for key in ("task_documentation_source", "task_owner_source", "task_test_source", "linked_section"):
        if row := payload.get(key):
            limit((row,))
    for key in ("task_local_source", "task_contract_source"):
        limit(payload.get(key, ()))
    if payload.get("source_mode") == "verified":
        for read in payload.get("read_plan", ()):
            read["snippet_embedded"] = any(
                row["path"] == read["path"] and row["line_start"] <= read["line_start"]
                and read["line_end"] <= row["line_end"] for row in selected
            )


def _source_body(lines: Sequence[str], start: int, end: int, max_chars: int | None) -> dict[str, Any]:
    """Materialize a complete body only when its joined text fits."""
    if max_chars is not None and (max_chars == 0 or end - start > max_chars):
        return {"body_omitted": True}
    body_lines = lines[start - 1:end]
    if max_chars is not None and sum(map(len, body_lines)) + len(body_lines) - 1 > max_chars:
        return {"body_omitted": True}
    return {"text": "\n".join(body_lines)}


def _read_source_file(path: Path) -> bytes:
    """Read the filesystem snapshot shared by source projections."""
    return path.read_bytes()


def capture_source_excerpts(
    store: GraphStore,
    reads: Sequence[dict[str, Any]],
    *,
    complete_only: bool = False,
    snapshots: dict[str, dict[str, Any]] | None = None,
    revisions: dict[str, ProjectRevision | None] | None = None,
    max_chars: int | None = None,
    seen_source: Sequence[SeenSourceRange] = (),
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
            first["indexed_span"] = first["indexed_span"] and all(row["indexed_span"] for row in overlapping)
            first["contains_declaration"] = any(row["contains_declaration"] for row in overlapping)
            for row in overlapping[1:]:
                regions.remove(row)
        else:
            first = {"path": item.get("path"), "line_start": start, "line_end": end,
                     "node_id": node.id, "artifact_id": artifact_id, "type": node.type, "indexed_span": True,
                     "contains_declaration": False}
            regions.append(first)
        node_start, node_end = node.properties.get("start_line"), node.properties.get("end_line")
        first["indexed_span"] &= type(node_start) is int and type(node_end) is int and node_start <= start <= end <= node_end
        first["contains_declaration"] |= bool(node_start == start and node.properties.get("name"))

    snapshots = {} if snapshots is None else snapshots
    revisions = {} if revisions is None else revisions
    result: list[dict[str, Any]] = []
    remaining = max_chars
    for region in regions:
        artifact_id = region["artifact_id"]
        start, requested_end = region["line_start"], region["line_end"]
        # Each pair of lines has a separator. A declaration line also contains source text.
        minimum_size = requested_end - start + region.pop("contains_declaration")
        verify_only = (region.pop("indexed_span") and remaining is not None
                       and (remaining == 0 or minimum_size > remaining))
        seen_candidates = tuple(span for span in seen_source if span.path == region["path"]
                                and span.line_start <= start and requested_end <= span.line_end)
        snapshot = snapshots.get(artifact_id)
        if snapshot is None or (not verify_only and snapshot["status"] == "verified" and "lines" not in snapshot):
            artifact = store.get_node(artifact_id, clone=False) if artifact_id else None
            project_id = str(artifact.properties.get("project_id") or "") if artifact else ""
            if project_id not in revisions:
                revisions[project_id] = RevisionRepository(store).latest(project_id) if project_id else None
            revision = revisions[project_id]
            entry = revision.manifest.get(region["path"]) if revision else None
            already_seen = entry is not None and any(span.sha256 == entry["sha256"] for span in seen_candidates)
            snapshot = _cached_source_snapshot(
                store, artifact, revision, snapshots, verify_only=verify_only or already_seen,
            )
        row = {**region, "source": "disk", "source_status": snapshot["status"]}
        if snapshot["status"] != "verified":
            row["reason"] = snapshot["reason"]
        elif any(span.sha256 == snapshot["sha256"] for span in seen_candidates):
            row.update(requested_line_end=requested_end, sha256=snapshot["sha256"], truncated=False, already_seen=True)
        elif "lines" not in snapshot:
            row.update(requested_line_end=requested_end, sha256=snapshot["sha256"], truncated=False, body_omitted=True)
        else:
            lines = snapshot["lines"]
            actual_end = min(requested_end, len(lines))
            if complete_only and actual_end < requested_end:
                continue
            if start > actual_end:
                row.update(source_status="unavailable", reason="source span is outside the file")
            else:
                row.update(line_end=actual_end, requested_line_end=requested_end, sha256=snapshot["sha256"],
                           truncated=actual_end < requested_end)
                row.update(_source_body(lines, start, actual_end, remaining))
                if remaining is not None and "text" in row:
                    remaining -= len(row["text"])
                if row["truncated"]:
                    row["next_line"] = actual_end + 1
        result.append(row)
    return result


def _cached_source_snapshot(
    store: GraphStore, artifact: MemoryNode | None, revision: ProjectRevision | None,
    snapshots: dict[str, dict[str, Any]], *, verify_only: bool = False,
) -> dict[str, Any]:
    """Reuse verification and load text only when a consumer needs it."""
    if artifact is None:
        return _source_snapshot(store, artifact, revision, verify_only=verify_only)
    snapshot = snapshots.get(artifact.id)
    if snapshot is None or (not verify_only and snapshot["status"] == "verified" and "lines" not in snapshot):
        snapshot = _source_snapshot(store, artifact, revision, verify_only=verify_only)
        snapshots[artifact.id] = snapshot
    return snapshot


def _source_snapshot(
    store: GraphStore, artifact: MemoryNode | None, revision: ProjectRevision | None, *, verify_only: bool = False,
) -> dict[str, Any]:
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
        if verify_only:
            _, digest, _ = fingerprint_file(path)
        else:
            content = _read_source_file(path)
            digest = hashlib.sha256(content).hexdigest()
        if digest != entry["sha256"]:
            return {"status": "stale", "reason": "source changed after indexing; graph spans require a refresh"}
        if verify_only:
            return {"status": "verified", "sha256": digest}
        encoding = utf16_encoding(content[:TEXT_SAMPLE_BYTES])
        text = content.decode(encoding or "utf-8-sig")
        crlf = text.count("\r\n")
        endings = [name for name, count in (
            ("CRLF", crlf), ("LF", text.count("\n") - crlf), ("CR", text.count("\r") - crlf),
        ) if count]
        return {"status": "verified", "sha256": digest, "lines": text.splitlines(),
                "encoding": encoding.upper() if encoding else "UTF-8 BOM" if content.startswith(BOM_UTF8) else "UTF-8",
                "line_endings": endings[0] if len(endings) == 1 else "mixed" if endings else "none"}
    except (OSError, UnicodeError, ValueError) as exc:
        return {"status": "unavailable", "reason": f"cannot read source: {type(exc).__name__}"}


def _current_headings(store: GraphStore, artifact: MemoryNode, digest: str) -> list[MemoryNode]:
    """Read heading coordinates tied to the verified Markdown source."""
    edges = store.get_edges(from_id=artifact.id, type_="HAS_SECTION", limit=None, clone=False)
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
        if stop > end:
            return stop
    # Explicit task excerpts can include a nearby fenced example even if
    # general document indexing is disabled.
    first = next((index for index in range(end, len(lines)) if lines[index].strip()), None)
    if first is None:
        return end
    fence = FENCE_RE.match(lines[first])
    if fence is None or fence.group(2) is None:
        return end
    return next((index + 1 for index in range(first + 1, len(lines))
                 if closes_fence(lines[index], fence[1])), end)


def locate_linked_section(
    store: GraphStore,
    owners: Sequence[dict[str, Any]],
    *,
    snapshots: dict[str, dict[str, Any]],
    revisions: dict[str, ProjectRevision | None],
) -> dict[str, Any] | None:
    """Locate one explicitly named Markdown section in the owner's verified revision."""
    for owner in owners:
        if owner["source_status"] != "verified" or owner["truncated"] or not owner.get("text"):
            continue
        # Only project-relative links in verified bodies can select documentation.
        source = store.get_node(owner["artifact_id"], clone=False)
        project_id = source.properties.get("project_id") if source else None
        revision = revisions.get(project_id) if project_id else None
        if revision is None:
            continue
        for reference in _MARKDOWN_SECTION_REF.finditer(owner["text"]):
            path = PurePosixPath(unquote(reference[1])).as_posix()
            entry = revision.manifest.get(path)
            if not entry:
                continue
            artifact = store.get_node(entry["artifact_id"], clone=False)
            if (artifact is None or artifact.properties.get("artifact_type") != "markdown"
                    or artifact.properties.get("project_id") != project_id
                    or artifact.properties.get("relative_path") != path):
                continue
            headings = _current_headings(store, artifact, entry["sha256"])
            anchor = normalize("NFC", unquote(reference[2].rstrip(".,;:!")).casefold())
            matching = [node for node in headings if _heading_anchor(node.text) == anchor]
            if len(matching) != 1:
                continue
            heading = matching[0]
            start = heading.properties["start_line"]
            snapshot = _cached_source_snapshot(store, artifact, revision, snapshots)
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


def _heading_anchor(title: str) -> str:
    """Build a Markdown anchor while preserving letters and combining marks."""
    return "".join(
        "-" if char == " " else char for char in normalize("NFC", title.casefold())
        if char.isalnum() or char in " _-" or category(char).startswith("M")
    )


def capture_task_test_file(
    store: GraphStore,
    paths: Sequence[str],
    revision: ProjectRevision,
    snapshots: dict[str, dict[str, Any]],
    *,
    shown_paths: set[str],
    max_chars: int,
) -> dict[str, Any] | None:
    """Show one indexed test file identified by the assignment."""
    for path in paths:
        entry = revision.manifest.get(path)
        if path in shown_paths or not entry:
            continue
        artifact_id = entry["artifact_id"]
        artifact = store.get_node(artifact_id, clone=False)
        if artifact is None or artifact.properties.get("context_scope") != "test":
            continue
        snapshot = _cached_source_snapshot(store, artifact, revision, snapshots)
        if whole := _whole_task_file(path, artifact_id, snapshot, max_chars):
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
    max_chars: int,
) -> dict[str, Any] | None:
    """Read one unambiguous prose paragraph referring to a qualified task symbol."""
    symbols = {symbol for symbol in source_symbol_references(task) if "." in symbol or "::" in symbol}
    names = symbols | {re.split(r"\.|::", symbol)[-1] for symbol in symbols}
    if not names:
        return None
    documents = [path for path in dict.fromkeys(paths) if path in revision.manifest
                 and PurePosixPath(path).suffix.casefold() in {".md", ".markdown"}]
    if len(documents) != 1:
        return None
    path = documents[0]
    artifact_id = revision.manifest[path]["artifact_id"]
    artifact = store.get_node(artifact_id, clone=False)
    snapshot = _cached_source_snapshot(store, artifact, revision, snapshots)
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
            elif closes_fence(line, fence):
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
        stop = next((index + 1 for index in range(example + 1, len(lines))
                     if closes_fence(lines[index], opening[1])), None)
        if stop is not None:
            end = stop
            if end + 1 < len(lines) and not lines[end].strip():
                paragraph = end + 1
                while paragraph < len(lines) and lines[paragraph].strip() and not (
                    HEADING_RE.match(lines[paragraph]) or FENCE_RE.match(lines[paragraph])
                ):
                    paragraph += 1
                end = paragraph
    if _covers_verified_span(shown, path, start + 1, end, snapshot["sha256"]):
        return None
    return {"path": path, "node_id": artifact_id, "artifact_id": artifact_id,
            "line_start": start + 1, "line_end": end, "source_status": "verified", "sha256": snapshot["sha256"],
            "encoding": snapshot["encoding"], "line_endings": snapshot["line_endings"], "truncated": False,
            **_source_body(lines, start + 1, end, max_chars)}


def assigned_document_outlines(
    store: GraphStore, paths: Sequence[str], revision: ProjectRevision, snapshots: dict[str, dict[str, Any]],
    *, shown: Sequence[dict[str, Any]], max_items: int,
) -> list[dict[str, Any]]:
    """Share one section budget across assigned Markdown files with verified locations."""
    shown_paths = {row["path"] for row in shown if row.get("source_status") == "verified" and "text" in row}
    documents = []
    for path in dict.fromkeys(paths):
        if path in shown_paths or PurePosixPath(path).suffix.casefold() not in {".md", ".markdown"}:
            continue
        if indexed := _verified_document_headings(store, path, revision, snapshots):
            documents.append((path, *indexed))
            if len(documents) == max_items:
                break
    outlines = []
    remaining = max_items
    for index, (path, headings, snapshot) in enumerate(documents):
        limit = remaining // (len(documents) - index)
        depth = min((node.properties["level"] for node in headings if node.properties["level"] > 1), default=1)
        file_end = len(snapshot["lines"])
        boundary_lines = list(dict.fromkeys(
            node.properties["start_line"] for node in headings if node.properties["level"] <= depth
        ))
        ends = dict(zip(boundary_lines, [*(min(line - 1, file_end) for line in boundary_lines[1:]), file_end]))
        sections = [node for node in headings if node.properties["level"] == depth]
        locations = [{"title": " ".join(node.text.split()), "line_start": node.properties["start_line"],
                      "line_end": ends[node.properties["start_line"]]} for node in sections[:limit]]
        remaining -= len(locations)
        outlines.append({"path": path, "source_status": "verified", "sha256": snapshot["sha256"],
                         "omitted": len(sections) - len(locations), "headings": locations})
    return outlines


def task_setup_location(
    store: GraphStore, revision: ProjectRevision, snapshots: dict[str, dict[str, Any]],
    *, excluded_paths: Sequence[str] = (),
    policy: SourceEvidencePolicy = _DEFAULT_SOURCE,
) -> dict[str, Any] | None:
    """Locate a configured setup heading in a current indexed document."""
    if not policy.setup_headings:
        return None
    titles = tuple(_heading_key(title) for title in policy.setup_headings)
    excluded = set(excluded_paths)
    preferred = [path for path in policy.setup_preferred_paths if path not in excluded]

    def location(path: str) -> dict[str, Any] | None:
        entry = revision.manifest.get(path)
        artifact = store.get_node(entry["artifact_id"], clone=False) if entry else None
        if artifact is None or entry is None:
            return None
        headings = _current_headings(store, artifact, entry["sha256"])
        keys = [_heading_key(node.text) for node in headings]
        title = next((key for key in titles if keys.count(key) == 1), None)
        if title is None:
            return None
        snapshot = _cached_source_snapshot(store, artifact, revision, snapshots)
        if snapshot["status"] != "verified":
            return None
        heading = headings[keys.index(title)]
        return {
            "path": path, "title": " ".join(heading.text.split()),
            "line_start": heading.properties["start_line"],
            "line_end": _section_end(headings, heading, len(snapshot["lines"])),
            "source_status": "verified", "sha256": snapshot["sha256"],
        }

    for path in preferred:
        if found := location(path):
            return found
    paths: dict[str, int] = {}
    for index, title in enumerate(titles):
        for node, _ in store.lexical_search(title, top_k=None, node_types={"SourceFragment"},
                                            project_id=revision.project_id, clone=False):
            path = node.properties.get("relative_path")
            if (isinstance(path, str) and path in revision.manifest and path not in excluded and path not in preferred
                    and node.properties.get("fragment_type") == "heading" and _heading_key(node.text) == title):
                paths.setdefault(path, index)
    ordered = sorted(paths, key=lambda path: (paths[path], path.count("/"), path.casefold(), path))
    return next((found for path in ordered if (found := location(path))), None)


def _heading_key(title: str) -> str:
    """Match a complete heading irrespective of casing or whitespace."""
    return " ".join(title.casefold().split())


def _verified_document_headings(
    store: GraphStore, path: str, revision: ProjectRevision, snapshots: dict[str, dict[str, Any]],
) -> tuple[list[MemoryNode], dict[str, Any]] | None:
    """Read indexed sections from a single current, hash-verified Markdown file."""
    entry = revision.manifest.get(path)
    if entry is None:
        return None
    artifact = store.get_node(entry["artifact_id"], clone=False)
    if artifact is None or not (headings := _current_headings(store, artifact, entry["sha256"])):
        return None
    snapshot = _cached_source_snapshot(store, artifact, revision, snapshots)
    return (headings, snapshot) if snapshot["status"] == "verified" else None


def _whole_task_file(path: str, artifact_id: str, snapshot: dict[str, Any], max_chars: int) -> dict[str, Any] | None:
    """Construct a complete file excerpt using an existing verified snapshot."""
    if snapshot["status"] != "verified" or not snapshot["lines"]:
        return None
    return {"path": path, "node_id": artifact_id, "artifact_id": artifact_id,
            "line_start": 1, "line_end": len(snapshot["lines"]), "whole_file": True,
            "source_status": "verified", "sha256": snapshot["sha256"], "truncated": False,
            "encoding": snapshot["encoding"], "line_endings": snapshot["line_endings"],
            **_source_body(snapshot["lines"], 1, len(snapshot["lines"]), max_chars)}


def complete_pinned_task_file(
    store: GraphStore, revision: ProjectRevision, snapshots: dict[str, dict[str, Any]],
    spans: Sequence[dict[str, Any]],
    *, max_chars: int,
) -> dict[str, Any] | None:
    """Fill one production file when pinned ranges already cover much of it."""
    paths = dict.fromkeys(row["path"] for row in spans if row.get("source_status") == "verified" and "text" in row)
    for path in paths:
        artifact_id = revision.manifest[path]["artifact_id"]
        artifact = store.get_node(artifact_id, clone=False)
        if (artifact is None or artifact.properties.get("artifact_type") != "code"
                or artifact.properties.get("context_scope") == "test"):
            continue
        whole = _whole_task_file(path, artifact_id, snapshots[artifact_id], max_chars)
        if whole is None:
            continue
        covered = {number for row in spans if row["path"] == path and row.get("sha256") == whole["sha256"]
                   for number in range(row["line_start"], row["line_end"] + 1)}
        if len(covered) == whole["line_end"] or len(covered) * 3 < whole["line_end"]:
            continue
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
    max_chars: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Complete an owner and recover declarations connected to verified edit pins."""
    completed = None
    reads: list[dict[str, Any]] = []
    loaded: dict[str, list[MemoryNode]] = {}
    mentioned = set(source_symbol_references(instructions, qualified=False))
    for row in spans:
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
                                              limit=None, clone=False)
                loaded[artifact_id] = [
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
            if stop <= len(lines) and not _covers_verified_span(shown, path, first, stop, row["sha256"]):
                completed = {**row, "line_end": stop, "node_id": tails[0].id, "artifact_id": artifact_id}
                completed.pop("text", None)
                completed.pop("body_omitted", None)
                completed.update(_source_body(lines, first, stop, max_chars))
        if owners:
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
    """Prefer resolved local types, then sibling methods named in the assignment."""
    types = {node.id: node for node in definitions if node.type in {"Class", "Interface"}}
    candidates: dict[str, MemoryNode] = {}
    if types:
        for owner in sorted(owners, key=lambda node: node.properties["start_line"]):
            edges = store.get_edges(from_id=owner.id, type_="READS", limit=None, clone=False)
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
        if (node.id not in used
                and (last < pin["line_start"] or first > pin["line_end"])
                and not _covers_verified_span(shown, pin["path"], first, last, pin["sha256"])):
            result.append({"path": pin["path"], "node_id": node.id, "line_start": first, "line_end": last})
    return result

def reuse_task_source(
    snippets: Sequence[dict[str, Any]],
    linked_section: dict[str, Any] | None,
    revision: ProjectRevision,
    snapshots: dict[str, dict[str, Any]],
    *,
    shown: Sequence[dict[str, Any]],
    include_guidance: bool = True,
    max_chars: int,
) -> None:
    """Make already verified task source edit-ready and include linked guidance."""
    for snippet in snippets:
        snapshot = snapshots.get(snippet.get("artifact_id", ""))
        if (snippet.get("source_status") == "verified" and snapshot and "lines" in snapshot
                and snippet["sha256"] == snapshot.get("sha256")):
            snippet.update(encoding=snapshot["encoding"], line_endings=snapshot["line_endings"])
    if linked_section is None or not include_guidance:
        return
    path = linked_section["path"]
    start, end, digest = (linked_section[key] for key in ("line_start", "line_end", "sha256"))
    if _covers_verified_span(shown, path, start, end, digest):
        return
    entry = revision.manifest.get(path)
    snapshot = snapshots.get(entry["artifact_id"]) if entry else None
    if snapshot is None or snapshot.get("sha256") != digest:
        return
    linked_section.update(truncated=False, **_source_body(snapshot["lines"], start, end, max_chars))


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
    *, max_chars: int,
) -> list[dict[str, Any]]:
    """Resolve every distinct pinned range against the committed source."""
    allowed = set(paths)
    references = list(dict.fromkeys(ref.groups() for ref in _TASK_SPAN.finditer(next_action)
                                    if ref[1] in allowed))
    continuation_used = False
    results: list[dict[str, Any]] = []
    for path, first, last, expected_hash in references:
        start, end = int(first), int(last)
        row = {"path": path, "line_start": start, "line_end": end}
        entry = revision.manifest.get(path)
        if entry is None or entry["sha256"] != expected_hash:
            results.append({**row, "source_status": "stale",
                            "reason": "the saved range belongs to different source; read the current file"})
            continue
        if _covers_verified_span(results, path, start, end, expected_hash):
            continue
        if end < start:
            results.append({**row, "source_status": "unavailable", "reason": "invalid saved line range"})
            continue
        artifact_id = entry["artifact_id"]
        artifact = store.get_node(artifact_id, clone=False)
        snapshot = _cached_source_snapshot(store, artifact, revision, snapshots)
        if snapshot["status"] != "verified":
            results.append({**row, "source_status": snapshot["status"], "reason": snapshot["reason"]})
            continue
        lines = snapshot["lines"]
        if end > len(lines):
            results.append({**row, "source_status": "unavailable", "reason": "saved line range is outside the file"})
            continue
        if not continuation_used and path.lower().endswith((".md", ".markdown")):
            artifact = store.get_node(artifact_id, clone=False)
            if artifact is not None and artifact.properties.get("artifact_type") == "markdown":
                stop = _short_pinned_section(store, artifact, expected_hash, lines, start, end)
                if stop > end and (stop > end + 1 or lines[end]):
                    row["pinned_line_end"] = end
                    row["line_end"] = stop
                    continuation_used = True
        current = {**row, "source_status": "verified", "sha256": expected_hash,
                        "encoding": snapshot["encoding"], "line_endings": snapshot["line_endings"],
                        "truncated": False, **_source_body(lines, start, row["line_end"], max_chars)}
        results = uncovered_source_rows(results, (current,))
        results.append(current)
    return results
