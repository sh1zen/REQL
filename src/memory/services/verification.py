"""Run an ordered verification plan and retain evidence for identical checks."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from ..artifacts.fingerprint import normalize_relative_lookup_path, options_hash, project_id
from ..artifacts.revision import RevisionRepository
from ..config import REQLConfig
from ..storage import BlockGraphStore
from ..storage.adapters.store_lock import StoreLease


@dataclass(frozen=True)
class VerificationCheck:
    """A command and the project files required to reuse its result."""

    command: tuple[str, ...]
    inputs: tuple[str, ...]


def _read_plan(raw: object) -> tuple[tuple[str, tuple[VerificationCheck, ...]], ...]:
    """Validate the entire plan before executing its first command."""
    if not isinstance(raw, dict) or raw.keys() - {"focused", "broad"}:
        raise ValueError("A verification plan accepts focused and broad lists")
    stages = []
    for name in ("focused", "broad"):
        rows = raw.get(name, [])
        if not isinstance(rows, list) or (name == "focused" and not rows):
            raise ValueError(f"Verification stage '{name}' requires a list of checks with at least one focused check")
        checks = []
        for row in rows:
            if not isinstance(row, dict) or row.keys() - {"command", "inputs"}:
                raise ValueError("Each verification check accepts command and inputs lists")
            command = row.get("command")
            files = row.get("inputs", [])
            if (not isinstance(command, list) or not command or not command[0]
                    or any(not isinstance(value, str) or "\0" in value for value in command)):
                raise ValueError("Verification command must be a nonempty argument list")
            if not isinstance(files, list) or any(not isinstance(value, str) or "\0" in value for value in files):
                raise ValueError("Verification inputs must be a list of project-relative files")
            paths = {normalize_relative_lookup_path(value) for value in files}
            if "" in paths:
                raise ValueError("Verification inputs must name regular project files")
            checks.append(VerificationCheck(tuple(command), tuple(sorted(paths))))
        stages.append((name, tuple(checks)))
    return tuple(stages)


def _file_digest(path: Path) -> str:
    """Hash the live bytes without loading a whole input into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class VerificationService:
    """Keep verification independent of task completion and private sessions."""

    def __init__(self, root: Path, storage: Path, config: REQLConfig) -> None:
        self.root = root.resolve()
        self.storage = storage.resolve()
        self.config = config
        self._project_id = project_id(str(self.root))
        self._payload_version = None
        self._revision = None

    def _current_revision(self) -> str:
        """Refresh the compiled revision when another process changes the graph."""
        version = BlockGraphStore.read_payload_version(self.storage)
        if version == (None, None):
            raise ValueError("Run reql project compile from the project root before verification")
        if version != self._payload_version:
            with closing(BlockGraphStore(
                self.storage, create=False, read_only=True, defer_lexical_index=True, wait_for_lock=True,
                allow_empty_read_only=False, policy=self.config.storage, lexical_config=self.config.lexical,
            )) as store:
                self._revision = RevisionRepository(store).latest_id(self._project_id)
            self._payload_version = version
        if self._revision is None:
            raise ValueError("Run reql project compile from the project root before verification")
        return self._revision

    def _fingerprint(self, check: VerificationCheck, executable: Path, revision: str, environment: str) -> str:
        """Bind a passing result to the actual executable, source revision, and live inputs."""
        inputs = {}
        for name in check.inputs:
            path = (self.root / name).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                raise ValueError(f"Verification input must be a regular project-local file: {name}")
            inputs[name] = [path.relative_to(self.root).as_posix(), _file_digest(path)]
        return options_hash({
            "schema": 1, "root": self.root.as_posix(), "revision": revision, "command": check.command,
            "executable": [str(executable), _file_digest(executable)], "environment": environment, "inputs": inputs,
        })

    def verify(self, raw_plan: object, *, emit: Callable[[str], None]) -> dict[str, Any]:
        """Execute focused checks before broad checks, stopping on the first failure."""
        plan = _read_plan(raw_plan)
        revision = self._current_revision()
        environment = dict(os.environ)
        environment_hash = options_hash(environment)
        cache = self.storage.with_name(f"{self.storage.name}.verification")
        if cache.resolve() != cache:
            raise ValueError("Verification cache must remain next to project storage")
        results = []
        verified: list[tuple[VerificationCheck, Path, str]] = []

        def confirm(check: VerificationCheck, executable: Path, key: str) -> None:
            if (self._current_revision() != revision
                    or self._fingerprint(check, executable, revision, environment_hash) != key):
                raise ValueError("Verification inputs or revision changed. Run the verification plan again.")

        def run(check: VerificationCheck, executable: Path, environment: Mapping[str, str]) -> int:
            process = subprocess.run(
                (str(executable), *check.command[1:]), cwd=self.root, env=environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, errors="replace",
            )
            for output in (process.stdout, process.stderr):
                if output:
                    emit(output)
            return process.returncode

        for stage, checks in plan:
            for number, check in enumerate(checks, start=1):
                program = check.command[0]
                if Path(program).parent != Path(".") or "/" in program or "\\" in program:
                    program = str(self.root / program)
                found = shutil.which(program, path=environment.get("PATH", os.defpath))
                if found is None:
                    raise ValueError(f"Verification executable not found: {check.command[0]}")
                executable = Path(found).resolve()
                reused = False
                if check.inputs:
                    key = self._fingerprint(check, executable, revision, environment_hash)
                    receipt = cache / key
                    if receipt.is_symlink():
                        raise ValueError("Verification receipt must be a local file")
                    with StoreLease(receipt, blocking=True):
                        confirm(check, executable, key)
                        if receipt.is_symlink():
                            raise ValueError("Verification receipt must be a local file")
                        reused = receipt.is_file()
                        code = 0 if reused else run(check, executable, environment)
                        if code == 0 and not reused:
                            confirm(check, executable, key)
                            receipt.touch(exist_ok=False)
                    if code == 0:
                        verified.append((check, executable, key))
                else:
                    code = run(check, executable, environment)
                results.append({"stage": stage, "check": number, "exit_code": code, "reused": reused})
                if code != 0:
                    return {"source_revision": revision, "passed": False, "checks": results}
            if self._current_revision() != revision:
                raise ValueError("Source revision changed during verification. Run the verification plan again.")
            for check, executable, key in verified:
                confirm(check, executable, key)
        return {"source_revision": revision, "passed": True, "checks": results}
