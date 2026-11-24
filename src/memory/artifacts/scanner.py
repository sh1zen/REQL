"""Recursive project scanner for source artifact discovery."""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from stat import S_ISREG
from typing import Iterable

from ..config.policies import TestDiscoveryPolicy
from ..config import (
    PROJECT_CONFIG_FILENAME,
    ScanConfig,
    ScanExcludeRule,
    default_config,
    load_project_config_data,
    normalize_scan_path_pattern,
    resolve_scan_exclude_pattern,
)
from ..domain.timeutils import utcnow_iso
from .fingerprint import (
    artifact_id,
    normalize_path,
    project_id,
    relative_path,
)
from .mime import classify_path, is_unsupported_media_file
from .models import Project, ScanError, ScanResult, ScanSkippedFile, SourceArtifact
from .text_encoding import TEXT_SAMPLE_BYTES

_DEFAULT_SCAN_CONFIG = default_config().scan
DEFAULT_MAX_FILE_SIZE_BYTES = _DEFAULT_SCAN_CONFIG.max_file_size_bytes
_DEFAULT_EXCLUDE_RULES = tuple(resolve_scan_exclude_pattern(pattern) for pattern in _DEFAULT_SCAN_CONFIG.exclude)
SMALL_SCAN_MAX_FILES = 64
SMALL_SCAN_WORKERS = 2
MAX_SCAN_WORKERS = 8


@dataclass(frozen=True, slots=True)
class _FileScanOutcome:
    artifact: SourceArtifact | None = None
    skipped: ScanSkippedFile | None = None
    error: ScanError | None = None


@dataclass(frozen=True, slots=True)
class _ExcludeScope:
    root: Path
    rules: tuple[ScanExcludeRule, ...]


class ProjectScanner:
    """Scans a directory tree and returns project/artifact domain objects."""

    def __init__(
        self,
        *,
        max_file_size_bytes: int | None = DEFAULT_MAX_FILE_SIZE_BYTES,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        config_path: str | Path | None = None,
        use_default_ignores: bool = True,
        ignored_paths: tuple[Path, ...] = (),
        test_discovery: TestDiscoveryPolicy | None = None,
    ) -> None:
        self.max_file_size_bytes = max_file_size_bytes
        self.test_discovery = test_discovery
        self.include_patterns = include_patterns or []
        self.exclude_patterns = exclude_patterns or []
        self.exclude_rules = tuple(resolve_scan_exclude_pattern(pattern) for pattern in self.exclude_patterns)
        self.config_path = Path(config_path).expanduser().resolve(strict=False) if config_path else None
        self.use_default_ignores = use_default_ignores
        self.ignored_paths = frozenset(path.resolve() for path in ignored_paths)

    @classmethod
    def from_config(cls, config: ScanConfig, *, config_path: Path | None,
                    ignored_paths: tuple[Path, ...] = (),
                    test_discovery: TestDiscoveryPolicy | None = None) -> ProjectScanner:
        """Apply the effective project policy at filesystem entry points."""
        return cls(max_file_size_bytes=config.max_file_size_bytes,
                   include_patterns=config.include, exclude_patterns=config.exclude,
                   config_path=config_path, use_default_ignores=not config.ignore_defaults,
                   ignored_paths=ignored_paths, test_discovery=test_discovery)

    def scan(
        self,
        root_path: str | Path,
        *,
        name: str | None = None,
    ) -> ScanResult:
        root = _scan_root(root_path)

        now = utcnow_iso()
        normalized_root = _normalized_scan_path(root)
        project = Project(
            id=project_id(normalized_root),
            root_path=normalized_root,
            name=name or root.name or normalized_root,
            status="active",
            created_at=now,
            updated_at=now,
        )
        skipped: list[ScanSkippedFile] = []
        errors: list[ScanError] = []
        candidates = self._candidate_paths(root, skipped, errors)
        artifacts: list[SourceArtifact] = []

        # Hashing dominates scans of large projects. A small bounded pool avoids
        # paying for dozens of short-lived threads on the common small-file
        # workload, while ``map`` keeps the result order deterministic.
        worker_count = _scan_worker_count(len(candidates))
        if worker_count <= 1:
            outcomes = (self._scan_file(root, path, project, now) for path in candidates)
            _collect_scan_outcomes(outcomes, artifacts, skipped, errors)
        else:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="reql-scan") as executor:
                outcomes = executor.map(lambda path: self._scan_file(root, path, project, now), candidates)
                _collect_scan_outcomes(outcomes, artifacts, skipped, errors)

        counts: dict[str, int] = {}
        for artifact in artifacts:
            counts[artifact.artifact_type] = counts.get(artifact.artifact_type, 0) + 1
        artifacts.sort(key=lambda item: item.relative_path)
        return ScanResult(project=project, artifacts=artifacts, skipped_files=skipped, errors=errors, counts_by_type=counts)

    def input_paths(self, root_path: str | Path) -> list[Path]:
        """Discover verification inputs without applying compiler format or size limits."""
        errors: list[ScanError] = []
        paths = self._candidate_paths(_scan_root(root_path), [], errors)
        if errors:
            first = errors[0]
            raise OSError(f"Cannot scan verification inputs at {first.relative_path}: {first.message}")
        return paths

    def _candidate_paths(self, root: Path, skipped: list[ScanSkippedFile], errors: list[ScanError]) -> list[Path]:
        """Apply one traversal and path policy for compilation and verification."""
        default_rules = _DEFAULT_EXCLUDE_RULES if self.use_default_ignores else ()
        candidates: list[Path] = []
        selected_config = self.config_path
        base_scope_root = selected_config.parent if selected_config is not None else root
        if not root.is_relative_to(base_scope_root) and not base_scope_root.is_relative_to(root):
            base_scope_root = root
        base_scopes = (_ExcludeScope(base_scope_root, self.exclude_rules),) if self.exclude_rules else ()

        stack = [(root, base_scopes)]
        while stack:
            current, inherited_scopes = stack.pop()
            scopes = self._scopes_for_directory(root, current, inherited_scopes, selected_config=selected_config)
            try:
                entries = sorted(os.scandir(current), key=lambda item: item.name.casefold())
            except OSError as exc:
                rel = _safe_relative(root, current)
                errors.append(ScanError(_normalized_scan_path(current), rel, str(exc)))
                continue

            for entry in entries:
                path = Path(entry.path)
                rel = _safe_relative(root, path)
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError as exc:
                    errors.append(ScanError(_normalized_scan_path(path), rel, str(exc)))
                    continue

                if path in self.ignored_paths or any(rule.matches(rel) for rule in default_rules):
                    skipped.append(ScanSkippedFile(_normalized_scan_path(path), rel, "ignored"))
                    continue
                if _matches_exclude_scopes(path, scopes):
                    skipped.append(ScanSkippedFile(_normalized_scan_path(path), rel, "excluded"))
                    continue
                if is_dir:
                    stack.append((path, scopes))
                    continue
                if not is_file:
                    skipped.append(ScanSkippedFile(_normalized_scan_path(path), rel, "not_regular_file"))
                    continue
                if self.include_patterns and not _matches_include(rel, self.include_patterns):
                    skipped.append(ScanSkippedFile(_normalized_scan_path(path), rel, "not_included"))
                    continue
                candidates.append(path)
        return candidates

    def _scopes_for_directory(
        self,
        project_root: Path,
        current: Path,
        inherited: tuple[_ExcludeScope, ...],
        *,
        selected_config: Path | None,
    ) -> tuple[_ExcludeScope, ...]:
        """Extend inherited rules with a nested config scoped to ``current``."""

        if current == project_root:
            return inherited
        config_path = current / PROJECT_CONFIG_FILENAME
        if not config_path.is_file() or (selected_config is not None and config_path == selected_config):
            return inherited
        data = load_project_config_data(config_path)
        scan = data.get("scan")
        if not isinstance(scan, dict) or "exclude" not in scan:
            return inherited
        rules = tuple(resolve_scan_exclude_pattern(pattern) for pattern in scan["exclude"])
        return (*inherited, _ExcludeScope(current, rules)) if rules else inherited

    def _scan_file(
        self,
        root: Path,
        path: Path,
        project: Project,
        now: str,
    ) -> _FileScanOutcome:
        rel = _safe_relative(root, path)
        normalized = _normalized_scan_path(path)
        try:
            stat, sha256, sample = fingerprint_file(path, max_bytes=self.max_file_size_bytes)
        except OSError as exc:
            return _FileScanOutcome(error=ScanError(normalized, rel, str(exc)))
        size = stat.st_size
        if sha256 is None:
            return _FileScanOutcome(skipped=ScanSkippedFile(normalized, rel, "max_file_size_exceeded", size))
        if is_unsupported_media_file(path, sample):
            return _FileScanOutcome(skipped=ScanSkippedFile(normalized, rel, "unsupported_media", size))
        classification = classify_path(path, sample)
        return _FileScanOutcome(artifact=SourceArtifact(
            id=artifact_id(project.id, rel),
            project_id=project.id,
            uri=path.as_uri(),
            path=normalized,
            relative_path=rel,
            artifact_type=classification.artifact_type,
            test_discovery=self.test_discovery,
            language=classification.language,
            size_bytes=size,
            sha256=sha256,
            mtime=float(stat.st_mtime),
            status="active",
            created_at=now,
            updated_at=now,
            last_seen_at=now,
            last_compiled_at=None,
        ))


def fingerprint_file(path: Path, *, max_bytes: int | None = None) -> tuple[os.stat_result, str | None, bytes]:
    """Hash stable regular-file bytes and retain a classification sample."""
    for _attempt in range(3):
        before = path.stat(follow_symlinks=False)
        if not S_ISREG(before.st_mode):
            raise OSError(f"Expected a regular file: {path}")
        if max_bytes is not None and before.st_size > max_bytes:
            return before, None, b""
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _file_stamp(before) != _file_stamp(opened):
                continue
            sample = stream.read(TEXT_SAMPLE_BYTES)
            digest.update(sample)
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            closed = os.fstat(stream.fileno())
        after = path.stat(follow_symlinks=False)
        # Windows reports different ctime values for path and descriptor stats.
        if (_file_stamp(before) == _file_stamp(after) and before.st_ctime_ns == after.st_ctime_ns
                and _file_stamp(opened) == _file_stamp(closed) and opened.st_ctime_ns == closed.st_ctime_ns):
            return after, digest.hexdigest(), sample
    raise OSError("file changed repeatedly while being scanned")


def _file_stamp(info: os.stat_result) -> tuple[int, ...]:
    """Identify the file and detect concurrent changes to its content or mode."""
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns


def _scan_root(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve(strict=False)
    if not root.exists():
        raise FileNotFoundError(f"Project path does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Project path is not a directory: {root}")
    return root


def _safe_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        try:
            return relative_path(root, path)
        except ValueError:
            return Path(path).name


def _normalized_scan_path(path: Path) -> str:
    """Normalize paths already produced beneath the resolved scan root."""

    return path.as_posix() if path.is_absolute() else normalize_path(path)


def _scan_worker_count(candidate_count: int) -> int:
    """Keep small scans lean and bound larger scans by available CPUs and eight workers."""
    if candidate_count <= 1:
        return candidate_count
    if candidate_count <= SMALL_SCAN_MAX_FILES:
        return min(SMALL_SCAN_WORKERS, candidate_count)
    available = os.cpu_count() or 1
    return min(MAX_SCAN_WORKERS, available, candidate_count)


def _collect_scan_outcomes(
    outcomes: Iterable[_FileScanOutcome],
    artifacts: list[SourceArtifact],
    skipped: list[ScanSkippedFile],
    errors: list[ScanError],
) -> None:
    for outcome in outcomes:
        if outcome.artifact is not None:
            artifacts.append(outcome.artifact)
        if outcome.skipped is not None:
            skipped.append(outcome.skipped)
        if outcome.error is not None:
            errors.append(outcome.error)


def _matches_include(relative: str, patterns: list[str]) -> bool:
    rel = relative.replace("\\", "/").strip("/")
    for pattern in patterns:
        normalized = normalize_scan_path_pattern(pattern)
        if not normalized:
            continue
        if normalized.endswith("/**") and rel == normalized[:-3]:
            return True
        if normalized.startswith("**/"):
            tail = normalized[3:]
            if fnmatchcase(rel, tail):
                return True
        if fnmatchcase(rel, normalized):
            return True
        if "/" not in normalized and any(fnmatchcase(part, normalized) for part in rel.split("/")):
            return True
    return False


def _matches_exclude_scopes(path: Path, scopes: tuple[_ExcludeScope, ...]) -> bool:
    for scope in scopes:
        try:
            relative = path.relative_to(scope.root).as_posix()
        except ValueError:
            continue
        if any(rule.matches(relative) for rule in scope.rules):
            return True
    return False
