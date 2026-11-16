
"""Measure Codex token usage on frozen tasks with and without REQL."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import stat
import subprocess
import sys
from tempfile import mkdtemp
from typing import Any
from zipfile import ZipFile

from agents.install import install_agent_files
from memory.services.coordination import CoordinationStore
from memory.testing.evidence_store import TestEvidenceStore

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = Path(__file__).with_name("token_cases.json")
MODES = ("with-reql", "without-reql")
COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens",
            "uncached_input_plus_output_tokens")


def save(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run(command: list[str], cwd: Path, env: dict[str, str], *, timeout: int = 300) -> bytes:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    if result.returncode:
        message = (result.stdout + result.stderr).decode("utf-8", errors="replace")[-3000:]
        raise ValueError(f"{' '.join(command)} exited {result.returncode}\n{message}")
    return result.stdout


def git(cwd: Path, env: dict[str, str], *args: str) -> bytes:
    return run(["git", "-c", f"safe.directory={cwd.as_posix()}", "-C", str(cwd), *args], cwd, env)


def archive_path(root: Path, name: str) -> Path:
    """Resolve one input in the historical read-only fixture collection."""
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.exists():
        raise ValueError(f"Benchmark archive input is missing or outside {root}: {name}")
    return path


def unpack_snapshot(blob: bytes, project: Path) -> None:
    """Write regular tracked files from a Git snapshot into a fresh checkout."""
    with ZipFile(BytesIO(blob)) as archive:
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename)
            if (name.is_absolute() or ".." in name.parts or ".git" in name.parts
                    or "\\" in entry.filename or ":" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError(f"Unsupported fixture entry: {entry.filename}")
            dest = project.joinpath(*name.parts)
            if entry.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(archive.read(entry))
                if os.name != "nt" and entry.external_attr >> 16 & 0o111:
                    dest.chmod(0o755)


def fixture_project(arm: Path, case: dict, archives: Path, env: dict[str, str]) -> tuple[Path, str]:
    """Freeze the same source tree and baseline Git state for both arms."""
    source = archive_path(archives, case["fixture"])
    revision = case["revision"]
    snapshot = git(source, env, "archive", "--format=zip", revision)
    project = arm / "project"
    project.mkdir()
    unpack_snapshot(snapshot, project)
    rules = project / "AGENTS.md"
    if rules.is_file():
        data = rules.read_bytes()
        start = b"<!-- REQL-INSTALL:START -->"
        end = b"<!-- REQL-INSTALL:END -->"
        text, marker, tail = data.partition(start)
        if marker:
            _, closed, rest = tail.partition(end)
            if not closed:
                raise ValueError(f"Unclosed REQL rules in {source}")
            rules.write_bytes(text + rest)
    for dest, original in case.get("overlays", {}).items():
        target = project / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive_path(archives, original), target)
    for name in (*case["files"], *case.get("protected", ()), *case.get("index_files", ())):
        if not (project / name).is_file():
            raise ValueError(f"{case['id']}: missing fixture file {name}")
    for command in case["check_commands"]:
        for part in command:
            if part.startswith("{archives}/"):
                archive_path(archives, part.removeprefix("{archives}/"))
    git(project, env, "init", "-q")
    git(project, env, "config", "core.autocrlf", "false")
    git(project, env, "config", "core.whitespace", "cr-at-eol")
    git(project, env, "config", "core.hooksPath", ".git/empty-hooks")
    git_config = arm / "git.config"
    git(project, env, "config", "--file", str(git_config), "safe.directory", project.as_posix())
    env["GIT_CONFIG_GLOBAL"] = str(git_config)
    (project / ".git" / "info" / "exclude").write_text("/.reql/\n/.bench/\n", encoding="utf-8")
    git(project, env, "add", "-f", "--all")
    git(project, env, "-c", "user.name=REQL Benchmark", "-c", "user.email=benchmark@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "Frozen benchmark input")
    return project, git(project, env, "rev-parse", "HEAD^{tree}").decode().strip()


def worker_specs(case: dict) -> list[dict]:
    return case.get("workers") or [{"id": "worker", "task": case["task"], "files": case["files"]}]


def prepare_reql(arm: Path, project: Path, case: dict, env: dict[str, str]) -> dict:
    """Install current generated guidance and stage one REQL assignment per worker."""
    installed = install_agent_files(("codex",), project=True, project_dir=arm / "guidance",
                                    command_dir=arm / "bin", hooks=False)
    shim = next(item.path for item in installed.actions if item.kind == "command")
    skill = next(item.path for item in installed.actions if item.kind == "skill")
    storage = project / ".reql" / "memory.reql"
    store = CoordinationStore(storage.with_name("agent-dashboard.reql"))
    author = {"agent_id": "agent:benchmark", "session_id": "benchmark-planning"}
    prerequisites = []
    if case.get("workers"):
        constraint = store.put("constraint", case["task"], **author)["node"]
        prerequisites.append(constraint["id"])
    assignments = {}
    for worker in worker_specs(case):
        worker_id = worker["id"]
        depends = [*prerequisites, *(assignments[name] for name in worker.get("depends_on", ()))]
        task = store.put("task", worker["task"], files=worker.get("files", case["files"]),
                         depends_on=depends, **author)["node"]
        assignments[worker_id] = task["id"]
        if selection := case.get("saved"):
            TestEvidenceStore(storage).record_verification({
                **selection, "root": str(project), "task_id": task["id"], "fingerprint": "", "checks": [],
            })
    config_env = {**env, "REQL_CONFIG": str(ROOT / "reql.conf")}
    if case.get("index_files"):
        config_env["REQL_CONFIG_OVERRIDES"] = json.dumps({"scan": {"include": case["index_files"]}})
    (arm / "compile.log").write_bytes(
        run([sys.executable, str(ROOT / "cli.py"), "project", "compile"], project, config_env)
    )
    return {"assignments": assignments, "shim": str(shim), "skill": str(skill), "environment": config_env}


def trial_env(arm: Path) -> dict[str, str]:
    """Keep caches and configuration local to the comparable arms."""
    scratch = arm / "scratch"
    scratch.mkdir()
    target = str(scratch / "cargo-target")
    env = {key: value for key, value in os.environ.items() if not key.startswith("REQL_")}
    return {**env, "TEMP": str(scratch), "TMP": str(scratch), "TMPDIR": str(scratch),
            "CARGO_NET_OFFLINE": "true", "CARGO_TARGET_DIR": target, "CARGO_BUILD_BUILD_DIR": target,
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}


def prompt_for(case: dict, worker: dict, reql: dict | None) -> str:
    worker_id = worker["id"]
    scope = ", ".join(worker.get("files", case["files"]))
    prompt = [
        "Work only in this benchmark checkout. Leave changes uncommitted and preserve acceptance tests.",
        f"Edit scope. {scope}",
        f"Use {sys.executable} for Python checks. Review and verify the affected behavior.",
    ]
    if case.get("workers"):
        prompt.append("Two workers share this checkout. Coordinate changes to tests/test_test_intelligence.py "
                      "and edit only your own test. The API worker owns the Python contract.")
    if reql is not None:
        shim = str(reql["shim"])
        shell = f"& '{shim.replace(chr(39), chr(39) * 2)}'" if os.name == "nt" else shlex.quote(shim)
        identity = f"--agent agent:{worker_id} --activity {worker_id}"
        task = reql["assignments"][worker_id]
        query = worker.get("query", case["query"])
        prompt.extend([
            f"Start with {shell} agent {identity} init --task {task} --query {json.dumps(query)}",
            "Use the brief's verified ranges. Query a missing symbol with --follow-up or read its indicated lines. "
            "Review your scoped diff once after edits. Let finish run saved checks when present.",
            f"Finish with {shell} agent {identity} finish \"SUMMARY\" --task {task}.",
            f"For additional guidance use {reql['skill']}.",
        ])
        if worker.get("depends_on"):
            prerequisite = reql["assignments"][worker["depends_on"][0]]
            prompt.append(f"For API readiness use {shell} agent show {prerequisite}. "
                          "Use the shared contract for independent work and wait for the API task before finish.")
        elif case.get("workers"):
            prompt.append("Publish API readiness once with agent task update. Refer to the saved shared contract.")
    else:
        prompt.extend([
            "Use source search, Git and local test tools for discovery and coordination in this trial.",
            f"Shared requirements. {case['task']}",
            f"Your assignment. {worker['task']}" if case.get("workers") else "",
            f"After reviewing your patch, save an outcome and checks in .bench/{worker_id}.md.",
        ])
        for dependency in worker.get("depends_on", ()):
            prompt.append(f"Read .bench/{dependency}.md for that worker's handoff before declaring integration complete.")
    return "\n".join(line for line in prompt if line) + "\n"


def usage_from_trace(trace: Path) -> dict[str, int]:
    """Read token counters from successful Codex JSONL turns."""
    turns = [row["usage"] for line in trace.read_text(encoding="utf-8").splitlines()
             if (row := json.loads(line))["type"] == "turn.completed"]
    if not turns:
        raise ValueError(f"No completed Codex turn in {trace.name}")
    fields = COUNTERS[:3]
    for turn in turns:
        if (any(type(turn.get(key)) is not int or turn[key] < 0 for key in fields)
                or turn["cached_input_tokens"] > turn["input_tokens"]):
            raise ValueError(f"Invalid Codex token counters in {trace.name}")
    usage = {key: sum(turn[key] for turn in turns) for key in fields}
    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    usage["uncached_input_plus_output_tokens"] = usage["total_tokens"] - usage["cached_input_tokens"]
    return usage


def run_worker(arm: Path, project: Path, case: dict, worker: dict, reql: dict | None,
               env: dict[str, str], args: argparse.Namespace) -> dict:
    worker_id = worker["id"]
    prompt = prompt_for(case, worker, reql)
    (arm / f"{worker_id}-prompt.txt").write_text(prompt, encoding="utf-8")
    trace = arm / f"{worker_id}.jsonl"
    command = [args.codex, "-a", "never", "exec", "--ephemeral", "--json", "--color", "never",
               "-s", "workspace-write", "-C", str(project),
               "--add-dir", env["TEMP"], "-m", args.model,
               "-c", f"model_reasoning_effort={json.dumps(args.effort)}", "-"]
    with trace.open("wb") as stdout, (arm / f"{worker_id}.stderr").open("wb") as stderr:
        result = subprocess.run(command, cwd=project, env=env, input=prompt.encode("utf-8"),
                                stdout=stdout, stderr=stderr, timeout=args.timeout)
    if result.returncode:
        raise ValueError(f"Agent {worker_id} exited {result.returncode}. See {stderr.name}.")
    return usage_from_trace(trace)


def grade(arm: Path, project: Path, case: dict, archives: Path, env: dict[str, str], reql: dict | None) -> None:
    """Require a scoped patch, independent tests, and a durable handoff."""
    expected = set(case["files"])
    changed = set(git(project, env, "diff", "--name-only", "HEAD").decode().splitlines())
    if not changed or changed - expected or changed & set(case.get("protected", ())):
        raise ValueError(f"Patch paths outside acceptance scope: {sorted(changed)}")
    if git(project, env, "ls-files", "--others", "--exclude-standard"):
        raise ValueError("Unexpected untracked benchmark files")
    git(project, env, "diff", "--check", "HEAD")
    workers = worker_specs(case)
    if reql is not None:
        store = CoordinationStore(project / ".reql" / "agent-dashboard.reql")
        records = store.records()
        checkpoint_tasks = {task for record in records if record["kind"] == "checkpoint" and record["status"] == "done"
                            for task in record["summarizes"]}
        for task in reql["assignments"].values():
            if store.show(task)["status"] != "done" or task not in checkpoint_tasks:
                raise ValueError(f"Missing REQL task completion or checkpoint: {task}")
    else:
        for worker in workers:
            handoff = project / ".bench" / f"{worker['id']}.md"
            if not handoff.is_file() or not handoff.read_text(encoding="utf-8-sig").strip():
                raise ValueError(f"Missing control handoff: {handoff.name}")
    check_env = {**env, "PYTHONPATH": str(project / "src")}
    with (arm / "acceptance.log").open("wb") as log:
        for spec in case["check_commands"]:
            command = [part.replace("{python}", sys.executable).replace("{archives}", str(archives)) for part in spec]
            try:
                log.write(run(command, project, check_env))
            except ValueError as error:
                log.write(str(error).encode("utf-8"))
                raise
    (arm / "change.patch").write_bytes(git(project, env, "diff", "--binary", "HEAD"))


def execute_arm(run_dir: Path, case: dict, mode: str, args: argparse.Namespace,
                expected_tree: str | None) -> dict:
    arm = run_dir / case["id"] / mode
    arm.mkdir(parents=True)
    env = trial_env(arm)
    report = {"mode": mode, "status": "failed", "fixture_revision": case["revision"]}
    try:
        project, tree = fixture_project(arm, case, args.archive_root, env)
        report["fixture_tree"] = tree
        if expected_tree is not None and expected_tree != tree:
            raise ValueError("The two arms do not share an identical source tree")
        reql = prepare_reql(arm, project, case, env) if mode == "with-reql" else None
        if reql:
            save(arm / "assignments.json", reql["assignments"])
        if args.prepare_only:
            report["status"] = "prepared"
        else:
            (project / ".bench").mkdir()
            workers = worker_specs(case)
            worker_env = reql["environment"] if reql else env
            with ThreadPoolExecutor(max_workers=len(workers)) as pool:
                usage = list(pool.map(
                    lambda worker: run_worker(arm, project, case, worker, reql, worker_env, args), workers
                ))
            report["workers"] = dict(zip((worker["id"] for worker in workers), usage))
            report["usage"] = {key: sum(item[key] for item in usage) for key in COUNTERS}
            grade(arm, project, case, args.archive_root, env, reql)
            report["status"] = "passed"
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
    save(arm / "result.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    cases = {case["id"]: case for case in json.loads(CASES_PATH.read_bytes())}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List the seven token cases")
    parser.add_argument("--case", action="append", choices=cases, help="Select cases. Defaults to all seven.")
    parser.add_argument("--mode", choices=("both", *MODES), default="both")
    parser.add_argument("--archive-root", type=Path, default=ROOT / ".tmp")
    parser.add_argument("--output", type=Path, help="Create this directory for results. Defaults to .tmp/token-bench.")
    parser.add_argument("--prepare-only", action="store_true", help="Prepare both fixtures without running Codex")
    parser.add_argument("--codex", default="codex", help="Codex CLI executable")
    parser.add_argument("--model", default="gpt-6.2-sol")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--timeout", type=int, default=600, help="Maximum seconds for each agent invocation")
    args = parser.parse_args(argv)
    selected = list(dict.fromkeys(args.case or cases))
    if args.list:
        for case_id in selected:
            print(f"{case_id}: {cases[case_id]['task']}")
        return 0
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    if not args.prepare_only and shutil.which(args.codex) is None:
        parser.error(f"Codex CLI executable not found: {args.codex}")
    args.archive_root = args.archive_root.resolve()
    if args.output is None:
        base = ROOT / ".tmp" / "token-bench"
        base.mkdir(parents=True, exist_ok=True)
        output = Path(mkdtemp(dir=base))
    else:
        output = args.output.resolve()
        output.mkdir(parents=True, exist_ok=False)
    report = {"model": args.model, "effort": args.effort, "cases": {}}
    modes = MODES if args.mode == "both" else (args.mode,)
    failed = False
    for case_id in selected:
        arms = report["cases"][case_id] = {}
        fixture_tree = None
        for mode in modes:
            result = arms[mode] = execute_arm(output, cases[case_id], mode, args, fixture_tree)
            fixture_tree = fixture_tree or result.get("fixture_tree")
            failed |= result["status"] == "failed"
            print(f"{case_id} {mode}: {result['status']}", flush=True)
        if all(arms.get(mode, {}).get("status") == "passed" for mode in MODES):
            candidate, control = (arms[mode]["usage"] for mode in MODES)
            arms["delta_percent"] = {
                key: round(100 * (candidate[key] / control[key] - 1), 2) if control[key] else None
                for key in COUNTERS
            }
        save(output / "results.json", report)
    print(output / "results.json")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

