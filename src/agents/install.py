"""Install REQL agent instructions for common coding assistants."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import as_file, files
import importlib.util
from pathlib import Path
import json
import os
import platform as host_platform
import shutil
import shlex
import stat
import subprocess
import sys
from types import ModuleType
from typing import Iterable, TypedDict

from config import VERSION

from .clients import CLIENT_PROFILES, ClientPaths, ClientProfile


SECTION_START = "<!-- REQL-INSTALL:START -->"
SECTION_END = "<!-- REQL-INSTALL:END -->"
HOOK_ID = "REQL_AGENT_HOOK_V1"
VERSION_FILE = ".reql_version"
COMMAND_MARKER = "REQL-COMMAND-SHIM:V1"
COMMAND_ENV = "REQL_COMMAND_DIR"
_SKILL_GENERATOR: ModuleType | None = None

# Compatibility view for callers that enumerate the supported platforms.
PLATFORMS_CONFIG = {
    name: {"label": profile.label, "is_all": profile.install_all}
    for name, profile in CLIENT_PROFILES.items()
}


@dataclass(frozen=True)
class InstallAction:
    platform: str
    scope: str
    kind: str
    path: Path
    status: str

    def to_dict(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "scope": self.scope,
            "kind": self.kind,
            "path": str(self.path),
            "status": self.status,
        }


@dataclass(frozen=True)
class InstallResult:
    platforms: tuple[str, ...]
    scope: str
    actions: tuple[InstallAction, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "platforms": list(self.platforms),
            "scope": self.scope,
            "actions": [action.to_dict() for action in self.actions],
        }


def resolve_platforms(
    values: Iterable[str],
    *,
    install_all: bool = False,
    auto_detect: bool = False,
    project: bool = False,
    project_dir: Path | None = None,
    home_dir: Path | None = None,
) -> tuple[str, ...]:
    all_platforms = [name for name, profile in CLIENT_PROFILES.items() if profile.install_all]

    raw: list[str] = []
    if install_all:
        raw.extend(all_platforms)
    for value in values:
        raw.extend(part.strip().casefold() for part in value.split(",") if part.strip())
    if not raw:
        if auto_detect:
            raw.extend(detect_platforms(project=project, project_dir=project_dir, home_dir=home_dir))
        else:
            raw.append("codex")

    resolved: list[str] = []
    for name in raw:
        candidates = all_platforms if name == "all" else (name,)
        for candidate in candidates:
            if candidate not in CLIENT_PROFILES:
                supported_list = ", ".join(CLIENT_PROFILES)
                raise ValueError(f"unknown platform '{candidate}'. Choose from: {supported_list}, all")
            if candidate not in resolved:
                resolved.append(candidate)
    return tuple(resolved)


def detect_platforms(*, project: bool = False, project_dir: Path | None = None, home_dir: Path | None = None) -> tuple[str, ...]:
    """Detect supported coding-agent profiles or commands already present locally."""
    root = (project_dir or Path(".")).resolve() if project else (home_dir or Path.home()).expanduser()
    windows = host_platform.system() == "Windows"
    detected = []
    for name, profile in CLIENT_PROFILES.items():
        paths = profile.paths(root, project=project, windows=windows)
        if any(_is_agent_profile_signal(path) for path in paths.detection_paths(profile.signals)) or (
            not project and any(shutil.which(command) for command in profile.commands)
        ):
            detected.append(name)
    return tuple(detected)


def _is_agent_profile_signal(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_dir():
        return _directory_has_agent_profile_signal(path)
    return not _is_reql_owned_file(path)


def _directory_has_agent_profile_signal(path: Path) -> bool:
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    if not entries:
        return True
    return any(not _is_reql_owned_path(entry) for entry in entries)


def _is_reql_owned_path(path: Path) -> bool:
    if path.name == "reql-agent":
        return True
    if path.is_file():
        return _is_reql_owned_file(path)
    return False


def _is_reql_owned_file(path: Path) -> bool:
    if path.name == VERSION_FILE:
        return _is_reql_version_file(path)
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if SECTION_START in text and not _remove_section(text).strip():
        return True
    if path.suffix.casefold() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return False
        return not bool(_strip_reql_hooks(data))
    return False


def _is_reql_version_file(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("installer") == "reql-install"


def _strip_reql_hooks(value: object, *, in_list: bool = False) -> object | None:
    if in_list and _is_reql_hook(value):
        return None
    if isinstance(value, dict):
        cleaned = {key: cleaned_value for key, item in value.items() if (cleaned_value := _strip_reql_hooks(item)) is not None}
        return cleaned or None
    if isinstance(value, list):
        cleaned_list = [cleaned_value for item in value if (cleaned_value := _strip_reql_hooks(item, in_list=True)) is not None]
        return cleaned_list or None
    if _is_reql_hook(value):
        return None
    return value


def install_agent_files(
    platforms: Iterable[str],
    *,
    project: bool = False,
    project_dir: Path | None = None,
    home_dir: Path | None = None,
    command_dir: Path | None = None,
    dry_run: bool = False,
    hooks: bool = True,
) -> InstallResult:
    selected = tuple(platforms)
    scope = "project" if project else "user"
    root = (project_dir or Path(".")).resolve() if project else (home_dir or Path.home()).expanduser()
    windows = host_platform.system() == "Windows"
    actions: list[InstallAction] = []
    command_plan = _command_plan(command_dir)
    profiles = _selected_profiles(selected)

    for path, content in command_plan.files:
        status = _write_command_file(path, content, dry_run=dry_run)
        actions.append(InstallAction(platform="shared", scope=scope, kind="command", path=path, status=status))

    for name, profile in profiles:
        paths = profile.paths(root, project=project, windows=windows)
        planned = _planned_files(name, profile, paths, project=project, command=command_plan)
        for kind, path, content in planned:
            status = _write_file(path, content, sectioned=kind == "instructions", dry_run=dry_run)
            actions.append(InstallAction(platform=name, scope=scope, kind=kind, path=path, status=status))
        if hooks and paths.hook is not None:
            status = _install_hook(paths.hook, report_event=profile.hook_reports_event, dry_run=dry_run)
            actions.append(InstallAction(platform=name, scope=scope, kind="hook", path=paths.hook, status=status))
        for stamp_path in _version_stamp_paths(planned):
            status = _write_file(
                stamp_path,
                _version_payload(name, scope, command_path=command_plan.primary_path),
                sectioned=False,
                dry_run=dry_run,
            )
            actions.append(InstallAction(platform=name, scope=scope, kind="version", path=stamp_path, status=status))

    return InstallResult(platforms=selected, scope=scope, actions=tuple(actions))


def uninstall_agent_files(
    platforms: Iterable[str],
    *,
    project: bool = False,
    project_dir: Path | None = None,
    home_dir: Path | None = None,
    command_dir: Path | None = None,
    dry_run: bool = False,
) -> InstallResult:
    selected = tuple(platforms)
    scope = "project" if project else "user"
    root = (project_dir or Path(".")).resolve() if project else (home_dir or Path.home()).expanduser()
    windows = host_platform.system() == "Windows"
    actions: list[InstallAction] = []
    profiles = _selected_profiles(selected)
    command_plan = _command_plan(command_dir)
    if command_dir is not None:
        for path, _content in command_plan.files:
            status = _remove_command_file(path, dry_run=dry_run, stop=path.parent.parent)
            actions.append(InstallAction(platform="shared", scope=scope, kind="command", path=path, status=status))

    for name, profile in profiles:
        paths = profile.paths(root, project=project, windows=windows)
        planned = _planned_files(name, profile, paths, project=project, command=command_plan)
        for kind, path, _content in planned:
            if kind == "instructions":
                status = _remove_section_file(path, dry_run=dry_run, stop=root)
            else:
                status = _remove_owned_file(path, dry_run=dry_run, stop=root)
            actions.append(InstallAction(platform=name, scope=scope, kind=kind, path=path, status=status))
        if paths.hook is not None:
            status = _uninstall_hook(paths.hook, root=root, dry_run=dry_run)
            actions.append(InstallAction(platform=name, scope=scope, kind="hook", path=paths.hook, status=status))
        for stamp_path in _version_stamp_paths(planned):
            status = _remove_owned_file(stamp_path, dry_run=dry_run, stop=root)
            actions.append(InstallAction(platform=name, scope=scope, kind="version", path=stamp_path, status=status))
            if not dry_run:
                _cleanup_empty_dirs(stamp_path.parent, stop=root)

    return InstallResult(platforms=selected, scope=scope, actions=tuple(actions))


def _selected_profiles(names: tuple[str, ...]) -> list[tuple[str, ClientProfile]]:
    """Validate client names before changing any installation files."""
    profiles = []
    for name in names:
        if name not in CLIENT_PROFILES:
            raise ValueError(f"unknown platform '{name}'")
        profiles.append((name, CLIENT_PROFILES[name]))
    return profiles


class _CommandOptions(TypedDict):
    command_name: str
    command_path: Path
    fallback_command: str


def _planned_files(
    name: str, profile: ClientProfile, paths: ClientPaths, *, project: bool, command: CommandPlan,
) -> list[tuple[str, Path, str]]:
    """Render the resources described by a resolved client profile."""
    command_options: _CommandOptions = {
        "command_name": command.command_name,
        "command_path": command.primary_path,
        "fallback_command": command.fallback_command,
    }
    planned: list[tuple[str, Path, str]] = []
    if paths.skills is not None:
        planned.extend(_skill_files(base=paths.skills, platform_name=name, project=project, **command_options))
    if paths.rule is not None and profile.rule is not None:
        renderer = profile.rule[1]
        if renderer == "cursor":
            content = _cursor_rule(**command_options)
        elif renderer == "copilot":
            content = _vscode_copilot_rule(**command_options)
        else:
            content = _markdown_rule(profile.label, **command_options)
        planned.append(("rule", paths.rule, content))
    if paths.instructions is not None:
        instruction = ("instructions", paths.instructions, _instruction_section(name, project=project, **command_options))
        if paths.skills is None:
            planned.insert(0, instruction)
        else:
            planned.append(instruction)
    return planned


def _version_stamp_paths(planned: list[tuple[str, Path, str]]) -> tuple[Path, ...]:
    """Place stamps alongside skills or the client's first managed resource."""
    paths = tuple(path.parent / VERSION_FILE for kind, path, _content in planned if kind == "skill")
    if paths or not planned:
        return paths
    return (planned[0][1].parent / VERSION_FILE,)


def _version_payload(platform_name: str, scope: str, *, command_path: Path | None = None) -> str:
    payload = {
        "installer": "reql-install",
        "package_version": VERSION,
        "platform": platform_name,
        "scope": scope,
    }
    if command_path is not None:
        payload["command"] = "reql"
        payload["command_path"] = str(command_path)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class CommandPlan:
    command_name: str
    primary_path: Path
    fallback_command: str
    files: tuple[tuple[Path, str], ...]


def _command_plan(command_dir: Path | None) -> CommandPlan:
    directory = _command_dir(command_dir)
    executable = _command_file_names()[0]
    path = directory / executable
    args = _launcher_args()
    return CommandPlan(
        command_name="reql",
        primary_path=path,
        fallback_command=_format_command(args),
        files=((path, _command_script(path, args)),),
    )


def _command_dir(command_dir: Path | None) -> Path:
    if command_dir is not None:
        return command_dir.expanduser().resolve()
    env_dir = os.environ.get(COMMAND_ENV)
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    existing = _find_owned_command_on_path()
    if existing is not None:
        return existing.parent

    path_dir = _first_writable_user_command_dir()
    if path_dir is not None:
        return path_dir

    home = Path.home()
    if host_platform.system() == "Windows":
        return home / ".reql" / "bin"
    return home / ".local" / "bin"


def _find_owned_command_on_path() -> Path | None:
    for directory in _path_entries():
        for name in _command_file_names():
            candidate = directory / name
            if candidate.exists() and _is_owned_command_file(candidate):
                return candidate
    return None


def _first_writable_user_command_dir() -> Path | None:
    home = Path.home().resolve(strict=False)
    for directory in _path_entries():
        try:
            resolved = directory.resolve(strict=False)
        except OSError:
            continue
        if not resolved.is_relative_to(home):
            continue
        if _is_windows_apps_dir(resolved):
            continue
        if directory.exists() and os.access(directory, os.W_OK):
            target = directory / _command_file_names()[0]
            if target.exists() and not _is_owned_command_file(target):
                continue
            return directory
    return None


def _path_entries() -> list[Path]:
    entries: list[Path] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if raw:
            path = Path(raw).expanduser()
            if path not in entries:
                entries.append(path)
    return entries


def _command_file_names() -> tuple[str, ...]:
    if host_platform.system() == "Windows":
        return ("reql.cmd", "reql.bat", "reql.exe", "reql")
    return ("reql",)


def _is_windows_apps_dir(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    return host_platform.system() == "Windows" and "windowsapps" in parts


def _format_command(args: list[str], *, windows: bool | None = None) -> str:
    """Render one argument vector for the selected shell."""
    if windows is None:
        windows = host_platform.system() == "Windows"
    return subprocess.list2cmdline(args) if windows else shlex.join(args)


def _launcher_args() -> list[str]:
    python = sys.executable or "python"
    source_cli = Path(__file__).resolve().parents[2] / "cli.py"
    if source_cli.exists():
        return [python, str(source_cli)]
    return [python, "-m", "memory.cli"]


def _command_script(path: Path, args: list[str]) -> str:
    """Build the native shim from the same invocation shown in guidance."""
    windows = path.suffix.casefold() in {".cmd", ".bat"}
    command = _format_command(args, windows=windows)
    if windows:
        return (
            "@echo off\n"
            f"REM {COMMAND_MARKER}\n"
            "REM Managed by reql install. Do not edit by hand.\n"
            f"{command} %*\n"
            "exit /b %ERRORLEVEL%\n"
        )
    return (
        "#!/bin/sh\n"
        f"# {COMMAND_MARKER}\n"
        "# Managed by reql install. Do not edit by hand.\n"
        f"exec {command} \"$@\"\n"
    )


def _write_command_file(path: Path, content: str, *, dry_run: bool) -> str:
    if path.exists() and not _is_owned_command_file(path):
        return "not-owned"
    status = _write_file(path, content, sectioned=False, dry_run=dry_run)
    if not dry_run and status in {"created", "updated"} and path.suffix.casefold() not in {".cmd", ".bat"}:
        _make_executable(path)
    return status


def _remove_command_file(path: Path, *, dry_run: bool, stop: Path | None = None) -> str:
    if not path.exists():
        return "missing"
    if not _is_owned_command_file(path):
        return "not-owned"
    return _remove_owned_file(path, dry_run=dry_run, stop=stop)


def _is_owned_command_file(path: Path) -> bool:
    try:
        return COMMAND_MARKER in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _make_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _write_file(path: Path, content: str, *, sectioned: bool, dry_run: bool) -> str:
    if sectioned and path.exists():
        existing = path.read_text(encoding="utf-8")
        desired = _replace_or_append_section(existing, content)
    else:
        desired = content

    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == desired:
            return "unchanged"
        status = "would-update" if dry_run else "updated"
    else:
        status = "would-create" if dry_run else "created"

    if dry_run:
        return status

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(desired, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return status


def _remove_owned_file(path: Path, *, dry_run: bool, stop: Path | None = None) -> str:
    if not path.exists():
        return "missing"
    if dry_run:
        return "would-remove"
    path.unlink()
    _cleanup_empty_dirs(path.parent, stop=stop)
    return "removed"


def _remove_section_file(path: Path, *, dry_run: bool, stop: Path | None = None) -> str:
    if not path.exists():
        return "missing"
    existing = path.read_text(encoding="utf-8")
    cleaned = _remove_section(existing)
    if cleaned == existing:
        return "unchanged"
    if dry_run:
        return "would-update" if cleaned.strip() else "would-remove"
    if cleaned.strip():
        path.write_text(cleaned, encoding="utf-8")
        return "updated"
    path.unlink()
    _cleanup_empty_dirs(path.parent, stop=stop)
    return "removed"


def _replace_or_append_section(existing: str, section: str) -> str:
    start = existing.find(SECTION_START)
    end = existing.find(SECTION_END)
    if start != -1 and end != -1 and end > start:
        end += len(SECTION_END)
        parts = [part for part in (existing[:start].rstrip(), section.strip(), existing[end:].strip()) if part]
        return "\n\n".join(parts) + "\n"
    if existing.strip():
        return existing.rstrip() + "\n\n" + section.strip() + "\n"
    return section.strip() + "\n"


def _remove_section(existing: str) -> str:
    start = existing.find(SECTION_START)
    end = existing.find(SECTION_END)
    if start == -1 or end == -1 or end <= start:
        return existing
    end += len(SECTION_END)
    parts = [part for part in (existing[:start].rstrip(), existing[end:].strip()) if part]
    return ("\n\n".join(parts) + "\n") if parts else ""


def _install_hook(path: Path, *, report_event: bool, dry_run: bool) -> str:
    """Install a managed reminder for session startup."""
    settings = _read_json_object(path)
    _remove_managed_hooks(settings)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = settings["hooks"] = {}
    startup = hooks.get("SessionStart")
    if not isinstance(startup, list):
        startup = hooks["SessionStart"] = []
    startup.append({
        "matcher": "startup",
        "hooks": [{"type": "command", "command": _python_hook_command(report_event=report_event)}],
    })
    existed = path.exists()
    if existed and _read_json_object(path) == settings:
        return "unchanged"
    if dry_run:
        return "would-update" if existed else "would-create"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return "updated" if existed else "created"


def _uninstall_hook(path: Path, *, root: Path, dry_run: bool) -> str:
    """Remove REQL's startup reminder while keeping user configuration."""
    if not path.exists():
        return "missing"
    settings = _read_json_object(path)
    if not _remove_managed_hooks(settings):
        return "unchanged"
    if dry_run:
        return "would-update" if settings else "would-remove"
    if settings:
        path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return "updated"
    path.unlink()
    _cleanup_empty_dirs(path.parent, stop=root)
    return "removed"


def _remove_managed_hooks(settings: dict[str, object]) -> bool:
    """Remove owned startup commands, preserving other commands in each group."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    groups = hooks.get("SessionStart")
    if not isinstance(groups, list):
        return False
    remaining = []
    for group in groups:
        commands = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(commands, list):
            kept = [command for command in commands if not _is_reql_hook(command)]
            if len(kept) != len(commands):
                if kept:
                    remaining.append({**group, "hooks": kept})
                continue
        remaining.append(group)
    if remaining == groups:
        return False
    if remaining:
        hooks["SessionStart"] = remaining
    else:
        del hooks["SessionStart"]
        if not hooks:
            del settings["hooks"]
    return True


def _python_hook_command(*, report_event: bool) -> str:
    """Emit one short reminder when a supported client starts in a REQL project."""
    context = {"additionalContext": "Use REQL for repository discovery. Reuse verified excerpts and supplied checks."}
    if report_event:
        context["hookEventName"] = "SessionStart"
    encoded = json.dumps({"hookSpecificOutput": context}, separators=(",", ":"))
    script = (
        "import pathlib,sys;"
        "e=pathlib.Path('.reql/memory.reql').exists() or pathlib.Path('reql.conf').exists();"
        f"sys.stdout.write({encoded!r} if e else ''); # {HOOK_ID}"
    )
    return _format_command([sys.executable or "python", "-c", script])


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_reql_hook(item: object) -> bool:
    try:
        return HOOK_ID in json.dumps(item, sort_keys=True)
    except TypeError:
        return HOOK_ID in str(item)


def _cleanup_empty_dirs(path: Path, *, stop: Path | None = None) -> None:
    stop_resolved = stop.resolve() if stop is not None else None
    current = path
    while True:
        try:
            if stop_resolved is not None and current.resolve() == stop_resolved:
                return
            current.rmdir()
        except OSError:
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


def _skill_files(
    *,
    base: Path,
    platform_name: str,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> list[tuple[str, Path, str]]:
    files = [
        ("skill", base / skill_dir / "SKILL.md", content)
        for skill_dir, content in _skill_generator().skill_markdowns(
            platform_name=platform_name,
            project=project,
            command_name=command_name,
            command_path=command_path,
            fallback_command=fallback_command,
        )
    ]
    files.extend(
        ("skill-resource", base / skill_dir / relative_path, content)
        for skill_dir, relative_path, content in _skill_generator().skill_resources(
            platform_name=platform_name,
            project=project,
            command_name=command_name,
            command_path=command_path,
            fallback_command=fallback_command,
        )
    )
    return files


def _skill_generator() -> ModuleType:
    global _SKILL_GENERATOR
    if _SKILL_GENERATOR is not None:
        return _SKILL_GENERATOR

    resource = files("agents").joinpath("gen-skill.py")
    with as_file(resource) as path:
        spec = importlib.util.spec_from_file_location("agents.gen_skill_generated", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not load REQL skill generator from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    _SKILL_GENERATOR = module
    return module


def _instruction_section(
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> str:
    supported = ", ".join(profile.label for profile in CLIENT_PROFILES.values() if profile.install_all)
    return _skill_generator().instruction_section(
        platform_name=platform_name,
        project=project,
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        supported_clients=supported,
        section_start=SECTION_START,
        section_end=SECTION_END,
    )


def _cursor_rule(*, command_name: str, command_path: Path, fallback_command: str) -> str:
    return _skill_generator().cursor_rule(
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        section_start=SECTION_START,
        section_end=SECTION_END,
    )


def _vscode_copilot_rule(*, command_name: str, command_path: Path, fallback_command: str) -> str:
    return _skill_generator().vscode_copilot_rule(
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        section_start=SECTION_START,
        section_end=SECTION_END,
    )


def _markdown_rule(client_name: str, *, command_name: str, command_path: Path, fallback_command: str) -> str:
    return _skill_generator().markdown_rule(
        client_name,
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        section_start=SECTION_START,
        section_end=SECTION_END,
    )


def available_platforms_text() -> str:
    return ", ".join(CLIENT_PROFILES) + ", all"
