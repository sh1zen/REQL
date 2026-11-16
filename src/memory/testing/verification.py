"""Run a bounded batch of local checks and keep evidence at exact input states."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import distributions
import json
import os
from pathlib import Path
from stat import S_IMODE
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from typing import Any, Iterator, Sequence

from ..artifacts.fingerprint import project_id, read_bounded_file
from ..artifacts.mime import classify_path, is_unsupported_media_file
from ..artifacts.revision import ProjectRevision, RevisionRepository
from ..artifacts.scanner import ProjectScanner, fingerprint_file
from ..config import load_effective_config, resolve_config_path
from ..domain.timeutils import utcnow_iso
from ..storage import BlockGraphStore
from .evidence_store import TestEvidenceStore, require_verification_plan, verification_plan


_PATTERN = re.compile(r"[a-zA-Z0-9_.*?\[\]-]+\.py")
_TEST_COUNT = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
_UNITTEST_CASE = re.compile(r"^(FAIL|ERROR): ([A-Za-z_]\w*) \(([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\)\r?$",
                            re.MULTILINE | re.ASCII)
_UNITTEST_FRAME = re.compile(r'^  File "([^"\r\n]+)", line ([1-9][0-9]{0,6}), in (\w+)\r?$',
                             re.MULTILINE | re.ASCII)
_CARGO_FILTER = re.compile(r"[a-zA-Z0-9_:.][a-zA-Z0-9_:.-]{0,199}")
_CARGO_TEST_TARGET = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,199}")
_CARGO_COUNT = re.compile(r"^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed;", re.MULTILINE)
_CARGO_INPUTS = ("Cargo.toml", "Cargo.lock", ".cargo/config", ".cargo/config.toml", "rust-toolchain", "rust-toolchain.toml")
_VERIFICATION_VERSION = 3
SOURCE_CHECK_NAME = "REQL source check"
_CONFLICT_MARKER = re.compile(rb"^(?:<{7}|={7}|>{7})(?:[ \t]|$)")
_CARGO_VERIFICATION_VERSION = 1


def _resolve_input(root: Path, relative: str) -> Path:
    """Keep all fingerprinted inputs within the verified workspace."""
    path = Path(relative)
    if path.is_absolute() or path.drive or ".." in path.parts or not path.parts:
        raise ValueError(f"Verification input must be project-relative: {relative}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Verification input is outside the workspace: {relative}")
    return resolved


def _check_environment(root: Path) -> dict[str, str]:
    """Use the repository source tree ahead of installed or launcher modules."""
    environment = dict(os.environ)
    if (root / "src").is_dir():
        paths = [str(root / "src"), *environment.get("PYTHONPATH", "").split(os.pathsep)]
        environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(path for path in paths if path))
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


@dataclass(frozen=True, slots=True)
class _VerificationSnapshot:
    """Identify all selected inputs and the text files eligible for source checks."""

    fingerprint: str
    text_files: dict[str, str]
    max_file_size_bytes: int


def _capture_inputs(
    root: Path, store: TestEvidenceStore, extra_inputs: Sequence[str], *,
    cargo: bool, directories: dict[str, str], check_text: bool = False,
) -> _VerificationSnapshot:
    """Use REQL's scan policy and stable file hashes for local check inputs."""
    config_path = resolve_config_path(start_dir=root)
    config = load_effective_config(config_path, start_dir=root)
    storage = store.canonical_path.resolve()
    owned = [root / ".reql", storage.parent / "agents", store.verification_locks.resolve()]
    for base in (storage, store.path.resolve(), storage.with_name("agent-dashboard.reql")):
        owned.extend(base.with_name(base.name + suffix) for suffix in (
            "", ".wal", ".lock", ".tmp", ".readers", ".usage.jsonl", ".usage.jsonl.lock",
        ))
    if config.diagnostics.enabled and config.diagnostics.path:
        owned.append(Path(config.diagnostics.path))
    owned.extend(Path(path) for path in directories.values() if Path(path).resolve() != root)
    scanner = ProjectScanner.from_config(config.scan, config_path=config_path, ignored_paths=tuple(owned))
    try:
        paths = {path.relative_to(root).as_posix() for path in scanner.input_paths(root)}
    except OSError as exc:
        raise ValueError(f"Cannot scan verification inputs: {exc}") from exc
    required = {Path(path).as_posix() for path in extra_inputs}
    candidates = paths | required | (set(_CARGO_INPUTS) if cargo else set())
    digest = sha256()
    digest.update(f"{_VERIFICATION_VERSION}:{scanner.max_file_size_bytes}\x00".encode("ascii"))
    if cargo:
        digest.update(f"cargo-scope:{_CARGO_VERIFICATION_VERSION}\x00".encode("ascii"))
    text_files: dict[str, str] = {}
    for relative in sorted(candidates):
        path = _resolve_input(root, relative)
        digest.update(os.fsencode(relative) + b"\x00")
        try:
            stat, file_hash, sample = fingerprint_file(path)
        except FileNotFoundError:
            if relative in required:
                raise ValueError(f"Declared verification input is missing: {relative}") from None
            if relative in paths:
                raise ValueError(f"Verification input disappeared during the scan: {relative}") from None
            digest.update(b"missing\x00")
            continue
        except OSError as exc:
            raise ValueError(f"Cannot capture verification input {relative}: {exc}") from exc
        assert file_hash is not None
        digest.update(f"{S_IMODE(stat.st_mode)}:{file_hash}\x00".encode("ascii"))
        if (check_text and relative in paths and stat.st_size <= scanner.max_file_size_bytes
                and not is_unsupported_media_file(path, sample) and not classify_path(path, sample).binary):
            text_files[relative] = file_hash
    return _VerificationSnapshot(digest.hexdigest(), text_files, scanner.max_file_size_bytes)


def _compiled_source(root: Path, storage: Path) -> ProjectRevision | None:
    """Read the native diff baseline without creating or changing project storage."""
    if not (storage.is_file() or storage.with_name(f"{storage.name}.wal").is_file()):
        return None
    with closing(BlockGraphStore(storage, read_only=True, defer_lexical_index=True, lock_timeout_seconds=0.05)) as graph:
        return RevisionRepository(graph).latest(project_id(root.as_posix()))


def _check_source_diff(root: Path, snapshot: _VerificationSnapshot, baseline: ProjectRevision | None) -> dict[str, Any]:
    """Check text added or modified since the native revision for whitespace and conflict markers."""
    manifest = baseline.manifest if baseline is not None else {}
    errors: list[str] = []
    for relative, expected in snapshot.text_files.items():
        if len(errors) >= 40:
            break
        if manifest.get(relative, {}).get("sha256") == expected:
            continue
        try:
            content = read_bounded_file(_resolve_input(root, relative), snapshot.max_file_size_bytes)
        except OSError as exc:
            errors.append(f"{relative}: {exc}")
            continue
        if content is None or sha256(content).hexdigest() != expected:
            errors.append(f"{relative}: file changed during verification")
            continue
        for number, line in enumerate(content.removeprefix(b"\xef\xbb\xbf").split(b"\n"), 1):
            line = line.removesuffix(b"\r")
            if line.endswith((b" ", b"\t")):
                errors.append(f"{relative}:{number}: trailing whitespace")
            if _CONFLICT_MARKER.match(line):
                errors.append(f"{relative}:{number}: unresolved conflict marker")
            if len(errors) >= 40:
                break
    return {"name": SOURCE_CHECK_NAME, "passed": not errors, **({"error": "\n".join(errors)[:4000]} if errors else {})}


def _runner_fingerprints(environment: dict[str, str], *, isolated_bytecode: bool) -> tuple[str, str]:
    """Separate Python identity from process-specific environment values."""
    packages = sorted((distribution.metadata.get("Name") or "", distribution.version)
                      for distribution in distributions())
    runner = (sys.executable, sys.version, environment.get("PYTHONPATH", ""), packages)
    if isolated_bytecode:
        runner += ("isolated-bytecode",)
    return sha256(json.dumps(runner).encode("utf-8")).hexdigest(), _environment_fingerprint(environment)


def _environment_fingerprint(environment: dict[str, str]) -> str:
    return sha256(json.dumps(environment, sort_keys=True).encode("utf-8")).hexdigest()


def _tool_path(root: Path, program: str, environment: dict[str, str]) -> str:
    """Resolve a tool against the same project and PATH as its check."""
    path = Path(program)
    if path.drive or path.parent != Path(".") or program.startswith(("./", ".\\")):
        return str(root / path)
    search = os.pathsep.join(str(root / entry) for entry in environment.get("PATH", os.defpath).split(os.pathsep))
    executable = shutil.which(program, path=search)
    if executable is None:
        raise ValueError(f"Cannot locate verification tool: {program}")
    return executable


def _cargo_runner(root: Path, executable: str, environment: dict[str, str], timeout: int) -> str:
    """Fingerprint the Cargo runtime and inherited configuration for explicit reuse."""
    digest = sha256()
    for tool in (executable, _tool_path(root, environment.get("RUSTC") or "rustc", environment)):
        code, version = _run_check(root, [tool, "-vV"], environment, min(timeout, 30))
        if code or not version:
            raise ValueError(f"Cannot identify Cargo verification tool: {tool}\n{version[-1000:]}")
        digest.update(os.fsencode(Path(tool).resolve()) + b"\x00" + version.encode("utf-8") + b"\x00")
    home = Path(environment.get("CARGO_HOME") or Path.home() / ".cargo")
    for directory in dict.fromkeys((root / home, *(parent / ".cargo" for parent in root.parents))):
        for name in ("config", "config.toml"):
            path = directory / name
            digest.update(os.fsencode(path) + b"\x00")
            try:
                with path.open("rb") as stream:
                    config = sha256()
                    for chunk in iter(lambda: stream.read(65536), b""):
                        config.update(chunk)
                    digest.update(config.digest())
            except FileNotFoundError:
                digest.update(b"missing\x00")
    return digest.hexdigest()


def _run_check(root: Path, arguments: list[str], environment: dict[str, str], timeout: int, *,
               complete_output_limit: int | None = None) -> tuple[int, str]:
    """Capture a bounded tail of local output for failures and test counts."""
    with TemporaryFile() as output:
        try:
            result = subprocess.run(arguments, cwd=root, env=environment, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=timeout)
            code = result.returncode
        except subprocess.TimeoutExpired:
            return 124, f"Check exceeded {timeout} seconds."
        size = output.tell()
        if complete_output_limit is not None and size > complete_output_limit:
            return 125, f"Check output exceeded {complete_output_limit} bytes."
        output.seek(0 if complete_output_limit is not None else max(0, size - 16 * 1024))
        return code, output.read().decode("utf-8", errors="replace").strip()


def cargo_check_commands(filters: Sequence[str], targets: Sequence[str]) -> Iterator[tuple[str, list[str]]]:
    """Keep saved Cargo labels and executable arguments consistent across clients."""
    base = ["test", "--locked", "--offline"]
    for filter_ in filters:
        name = f"Cargo library tests ({filter_})" if filter_ else "Cargo library tests"
        yield name, [*base, "--lib", "--color", "never", *([filter_] if filter_ else [])]
    for target in targets:
        yield f"Cargo integration test ({target})", [*base, "--test", target, "--color", "never"]


def _cargo_metadata(root: Path, command: str, environment: dict[str, str], timeout: int) -> dict[str, Any]:
    """Get the effective Cargo workspace and artifact locations without building."""
    code, output = _run_check(
        root, [command, "metadata", "--locked", "--offline", "--no-deps", "--format-version", "1"],
        environment, min(timeout, 30), complete_output_limit=4 * 1024 * 1024,
    )
    if code:
        raise ValueError(f"Cannot resolve Cargo workspace configuration: {output[-1000:]}")
    try:
        metadata = json.loads(output)
    except (ValueError, RecursionError) as exc:
        raise ValueError("Invalid Cargo workspace metadata") from exc
    target = metadata.get("target_directory") if isinstance(metadata, dict) else None
    if not isinstance(target, str) or not Path(target).is_absolute():
        raise ValueError("Cargo metadata must identify its target directory")
    return metadata


def _isolate_cargo_artifacts(root: Path, environment: dict[str, str], directories: dict[str, str]) -> None:
    """Keep external caches on their chosen volume and separate build artifacts by checkout."""
    namespace = sha256(os.fsencode(os.path.normcase(root))).hexdigest()[:24]
    target = Path(directories["target_directory"]).resolve()
    if not target.is_relative_to(root):
        target /= Path("reql-workspaces") / namespace
        environment["CARGO_TARGET_DIR"] = str(target)
    if build_directory := directories.get("build_directory"):
        build = Path(build_directory).resolve()
        if not build.is_relative_to(root):
            environment["CARGO_BUILD_BUILD_DIR"] = str(build / "reql-workspaces" / namespace)
    else:
        # Older metadata can omit the intermediate directory. Use the resolved
        # target as the safe fallback for Cargo versions with a separate build dir.
        environment["CARGO_BUILD_BUILD_DIR"] = str(target)


def _cargo_test_inputs(root: Path, metadata: dict[str, Any], targets: Sequence[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Resolve named test entry points from Cargo metadata, including ignored files."""
    try:
        packages = metadata["packages"]
        sources: dict[str, set[str]] = {target: set() for target in targets}
        inputs: set[str] = set()
        for package in packages:
            for target in package["targets"]:
                name = target["name"]
                if name not in sources or "test" not in target["kind"]:
                    continue
                source = Path(target["src_path"]).relative_to(root).as_posix()
                manifest = Path(package["manifest_path"]).relative_to(root).as_posix()
                _resolve_input(root, source)
                _resolve_input(root, manifest)
                sources[name].add(source)
                inputs.update((source, manifest))
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Cargo integration target metadata must identify project-local source and manifest files") from exc
    if missing := [target for target, paths in sources.items() if not paths]:
        raise ValueError(f"Unknown Cargo integration test target: {', '.join(missing)}")
    return {target: sorted(paths) for target, paths in sources.items()}, sorted(inputs)


def verification_handoff(root: Path, store: TestEvidenceStore, task_id: str) -> dict[str, Any]:
    """Keep the recorded check selection available even when a past result is stale."""
    observed = store.verification(str(root), task_id)
    if observed is None:
        return {}
    plan = verification_plan(observed)
    paths = [rows[0] for rows in observed.get("cargo_test_sources", {}).values() if len(rows) == 1]
    # A declared input matching an exact discovery pattern names a concrete check
    # without rescanning tests or importing code during the handoff.
    patterns = {pattern for pattern in plan["patterns"] if not any(char in pattern for char in "*?[]")}
    for relative in plan["extra_inputs"]:
        path = Path(relative)
        if len(path.parts) == 2 and path.parts[0] == "tests" and path.name in patterns:
            paths.append(path.as_posix())
    inputs_match = _verification_inputs_match(root, store, observed)
    matching = _source_matching_observation(root, observed) if inputs_match else None
    history = {}
    if matching is not None:
        if all(check["passed"] for check in matching["checks"]):
            history["verified_checks"] = matching
        else:
            history["baseline_failure"] = {
                **{key: matching[key] for key in ("verified_at", "environment_matches", "runtime_checked") if key in matching},
                "checks": [_baseline_check(check, root, paths, patterns) for check in matching["checks"]],
            }
    return {"verification_plan": plan,
            **({"verification_test_paths": list(dict.fromkeys(paths))} if paths and inputs_match else {}),
            **history}


def _baseline_check(check: dict[str, Any], root: Path, paths: Sequence[str], patterns: set[str]) -> dict[str, Any]:
    """Recover a test case and its frame from the recorded unittest outcome without copying logs."""
    summary = {key: check[key] for key in ("name", "passed", "tests") if key in check}
    if check["passed"] or not check.get("tests") or check["name"] not in patterns:
        return summary
    candidates = {root / relative: relative for relative in paths if Path(relative).name == check["name"]}
    output = check.get("error") or ""
    match = _UNITTEST_CASE.search(output)
    if match is None:
        return summary
    outcome, method, qualified = match.groups()
    if len(qualified) > 160:
        return summary
    evidence = {"case": qualified, "outcome": outcome}
    next_case = _UNITTEST_CASE.search(output, match.end())
    for file, number, function in _UNITTEST_FRAME.findall(output, match.end(), next_case.start() if next_case else len(output)):
        if function == method and (path := candidates.get(Path(file))) is not None:
            evidence.update(path=path, line=int(number))
    return {**summary, "failure": evidence}


def _source_matching_observation(root: Path, receipt: dict[str, Any]) -> dict[str, Any] | None:
    """Annotate an input-matched observation with the runtime evidence available without launching checks."""
    environment = _check_environment(root)
    if receipt.get("cargo") or receipt.get("cargo_test"):
        # Task completion checks runtime identity again before using a cached pass.
        if receipt["patterns"] and _runner_fingerprints(environment, isolated_bytecode=True)[0] != receipt.get("python_runner"):
            return None
        return {**receipt, "environment_matches": _environment_fingerprint(environment) == receipt.get("environment"),
                "runtime_checked": False}
    runner, environment_hash = _runner_fingerprints(environment, isolated_bytecode=bool(receipt["patterns"]))
    if runner != receipt.get("runner"):
        return None
    return {**receipt, "environment_matches": environment_hash == receipt.get("environment")}


def receipt_matches_source(root: Path, store: TestEvidenceStore, receipt: dict[str, Any]) -> bool:
    """Confirm a passing receipt still describes the current project inputs."""
    return all(check["passed"] for check in receipt["checks"]) and _verification_inputs_match(root, store, receipt)


def _verification_inputs_match(root: Path, store: TestEvidenceStore, receipt: dict[str, Any]) -> bool:
    """Check the source snapshot independently of the test result."""
    try:
        inputs = (*receipt["extra_inputs"], *receipt.get("cargo_test_inputs", ()))
        cargo = bool(receipt.get("cargo") or receipt.get("cargo_test"))
        snapshot = _capture_inputs(root, store, inputs, cargo=cargo, directories=receipt.get("cargo_artifact_dirs", {}))
        return snapshot.fingerprint == receipt["fingerprint"]
    except (OSError, ValueError):
        return False


def run_saved_checks(
    root: Path, store: TestEvidenceStore, *, task_id: str, timeout: int, expected_plan: str | None = None,
) -> dict[str, Any]:
    """Run the saved task selection under its verification lease."""
    if not 1 <= timeout <= 3600:
        raise ValueError("Verification timeout must be between 1 and 3600 seconds")
    if not task_id:
        raise ValueError("Saved checks require a task id")
    with store.verification_lease(root, task_id, timeout=timeout):
        receipt = store.verification(str(root), task_id)
        plan = require_verification_plan(receipt, expected_plan)
        return _run_check_plan(root, store, task_id=task_id, plan=plan, receipt=receipt, timeout=timeout)


def _run_check_plan(
    root: Path, store: TestEvidenceStore, *, task_id: str, plan: dict[str, Any],
    receipt: dict[str, Any] | None, timeout: int,
) -> dict[str, Any]:
    """Execute the captured plan and reuse a matching passing receipt."""
    patterns = tuple(dict.fromkeys(plan["patterns"]))
    cargo = tuple(dict.fromkeys(plan.get("cargo", ())))
    cargo_test = tuple(dict.fromkeys(plan.get("cargo_test", ())))
    diff = plan["diff"]
    uses_cargo = bool(cargo or cargo_test)
    if len(patterns) + len(cargo) + len(cargo_test) > 8:
        raise ValueError("Verification supports up to eight Python or Cargo checks")
    if any(not _PATTERN.fullmatch(p) or p.startswith("-") for p in patterns):
        raise ValueError("Python verification requires unittest filename patterns")
    if any(filter_ and not _CARGO_FILTER.fullmatch(filter_) for filter_ in cargo):
        raise ValueError("Cargo library checks require literal test filters")
    if any(not _CARGO_TEST_TARGET.fullmatch(target) for target in cargo_test):
        raise ValueError("Cargo integration checks require literal test target names")
    if uses_cargo and not _resolve_input(root, "Cargo.toml").is_file():
        raise ValueError("Cargo verification requires a Cargo.toml at the project root")
    extras = sorted(set(plan["extra_inputs"]))
    selection = {"patterns": list(patterns), "diff": diff, "extra_inputs": extras}
    if cargo:
        selection["cargo"] = list(cargo)
    if cargo_test:
        selection["cargo_test"] = list(cargo_test)
    environment = _check_environment(root)
    cargo_command = _tool_path(root, "cargo", environment) if uses_cargo else ""
    directories: dict[str, str] = {}
    cargo_environment = environment
    test_sources: dict[str, list[str]] = {}
    target_inputs: list[str] = []
    if uses_cargo:
        metadata = _cargo_metadata(root, cargo_command, environment, timeout)
        directories = {key: str(Path(metadata[key]).resolve()) for key in ("target_directory", "build_directory")
                       if isinstance(metadata.get(key), str) and metadata[key]}
        cargo_environment = dict(environment)
        _isolate_cargo_artifacts(root, cargo_environment, directories)
        if cargo_test:
            test_sources, target_inputs = _cargo_test_inputs(root, metadata, cargo_test)
    inputs = (*extras, *target_inputs)
    snapshot = _capture_inputs(root, store, inputs, cargo=uses_cargo, directories=directories, check_text=diff)
    before = snapshot.fingerprint
    baseline = _compiled_source(root, store.canonical_path) if diff else None
    baseline_id = baseline.id if baseline is not None else None
    if patterns or not uses_cargo:
        python_runner, environment_hash = _runner_fingerprints(environment, isolated_bytecode=bool(patterns))
    else:
        python_runner, environment_hash = "", _environment_fingerprint(environment)
    cargo_runner = _cargo_runner(root, cargo_command, cargo_environment, timeout) if uses_cargo else ""
    runner = cargo_runner or python_runner
    if (receipt and receipt["fingerprint"] == before and receipt.get("runner") == runner
            and receipt.get("environment") == environment_hash and all(check["passed"] for check in receipt["checks"])
            and (not uses_cargo or (receipt.get("python_runner") == python_runner
                                    and receipt.get("cargo_artifact_dirs") == directories))
            and receipt.get("source_check_revision") == baseline_id
            and all(receipt.get(key) == value for key, value in selection.items())):
        with store.guard_verification_plan(str(root), task_id, plan["id"], verified=receipt):
            return {"status": "passed", "reused": True, "receipt": receipt, "checks": receipt["checks"]}

    checks = []
    if patterns:
        # Timestamp-based bytecode can survive a same-size edit within one second.
        with TemporaryDirectory(prefix="reql-unittest-") as cache:
            for pattern in patterns:
                arguments = [sys.executable, "-X", f"pycache_prefix={cache}", "-m", "unittest",
                             "discover", "-s", "tests", "-p", pattern]
                code, output = _run_check(root, arguments, environment, timeout)
                counts = _TEST_COUNT.findall(output)
                count = int(counts[-1]) if counts else 0
                passed = code == 0 and count > 0
                checks.append({"name": pattern, "tests": count, "passed": passed,
                               **({"error": output[-4000:] or "No tests ran"} if not passed else {})})
    for name, arguments in cargo_check_commands(cargo, cargo_test):
        code, output = _run_check(root, [cargo_command, *arguments], cargo_environment, timeout)
        count = sum(int(passed) + int(failed) for passed, failed in _CARGO_COUNT.findall(output))
        passed = code == 0 and count > 0
        checks.append({"name": name, "tests": count, "passed": passed,
                       **({"error": output[-4000:] or "No tests ran"} if not passed else {})})
    if diff:
        checks.append(_check_source_diff(root, snapshot, baseline))
    passed = all(check["passed"] for check in checks)
    receipt = None
    if before == _capture_inputs(root, store, inputs, cargo=uses_cargo, directories=directories).fingerprint:
        after_environment = _check_environment(root)
        unchanged = environment_hash == _environment_fingerprint(after_environment)
        if unchanged and python_runner:
            unchanged = python_runner == _runner_fingerprints(after_environment, isolated_bytecode=bool(patterns))[0]
        if unchanged and uses_cargo:
            _isolate_cargo_artifacts(root, after_environment, directories)
            unchanged = cargo_runner == _cargo_runner(root, _tool_path(root, "cargo", after_environment),
                                                       after_environment, timeout)
        if unchanged and cargo_test:
            metadata = _cargo_metadata(root, cargo_command, after_environment, timeout)
            unchanged = (test_sources, target_inputs) == _cargo_test_inputs(root, metadata, cargo_test)
        if unchanged:
            observed = {**selection, "root": str(root), "task_id": task_id, "fingerprint": before,
                        "runner": runner, "environment": environment_hash, "verified_at": utcnow_iso(), "checks": checks}
            if diff:
                observed["source_check_revision"] = baseline_id
            if uses_cargo:
                observed["python_runner"] = python_runner
                observed["cargo_artifact_dirs"] = directories
            if cargo_test:
                observed["cargo_test_sources"] = test_sources
                observed["cargo_test_inputs"] = target_inputs
            store.record_verification(observed, expected_plan=plan["id"])
            if passed:
                receipt = observed
    return {"status": "passed" if passed else "failed", "reused": False, "receipt": receipt, "checks": checks}
