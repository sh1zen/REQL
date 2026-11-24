"""Client profiles shared by detection and installation."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Literal


RuleKind = Literal["cursor", "copilot", "markdown"]
Signal = Literal["skills", "instructions", "rule_dir", "hook"]


@dataclass(frozen=True)
class ClientProfile:
    """Describe a client's supported resources and native detection signals."""

    label: str
    directory: str
    user_directory: str | None = None
    windows_user_directory: str | None = None
    user_directory_env: str | None = None
    skills: bool = True
    instructions: str | None = "AGENTS.md"
    project_instructions_at_root: bool = True
    user_instructions_at_root: bool = False
    rule: tuple[str, RuleKind] | None = None
    hook: str | None = None
    hook_reports_event: bool = False
    signals: tuple[Signal, ...] = ("skills",)
    project_signals: tuple[str, ...] = ()
    user_signals: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    install_all: bool = True

    def paths(self, root: Path, *, project: bool, windows: bool) -> ClientPaths:
        """Resolve a profile against exactly one project or home directory."""
        directory = self.directory
        if not project:
            directory = (self.windows_user_directory if windows else None) or self.user_directory or directory
        base = root / directory
        if not project and self.user_directory_env:
            if configured := os.environ.get(self.user_directory_env):
                base = Path(configured).expanduser()
        at_root = self.project_instructions_at_root if project else self.user_instructions_at_root
        return ClientPaths(
            skills=base / "skills" if self.skills else None,
            instructions=(root if at_root else base) / self.instructions if self.instructions else None,
            rule=base / self.rule[0] if self.rule else None,
            hook=base / self.hook if self.hook else None,
            extra_signals=tuple(root / path for path in (self.project_signals if project else self.user_signals)),
        )


@dataclass(frozen=True)
class ClientPaths:
    """Resolved paths for one installation target."""

    skills: Path | None
    instructions: Path | None
    rule: Path | None
    hook: Path | None
    extra_signals: tuple[Path, ...]

    def detection_paths(self, signals: tuple[Signal, ...]) -> tuple[Path, ...]:
        """Use installed resource locations alongside alternative profiles."""
        paths = {
            "skills": self.skills,
            "instructions": self.instructions,
            "rule_dir": self.rule.parent if self.rule else None,
            "hook": self.hook,
        }
        resolved = tuple(path for name in signals if (path := paths[name]) is not None)
        return tuple(dict.fromkeys((*resolved, *self.extra_signals)))


CLIENT_PROFILES = {
    "codex": ClientProfile("Codex", ".codex", user_instructions_at_root=True,
                           project_signals=(".codex/hooks.json",)),
    "claude": ClientProfile("Claude Code", ".claude", user_directory_env="CLAUDE_CONFIG_DIR",
                            instructions="CLAUDE.md", project_instructions_at_root=False,
                            hook="settings.json", hook_reports_event=True,
                            signals=("skills", "instructions", "hook"), commands=("claude",)),
    "opencode": ClientProfile("OpenCode", ".opencode", user_directory=".config/opencode", commands=("opencode",)),
    "kilo": ClientProfile("Kilo Code", ".kilocode", rule=("rules/reql.md", "markdown"),
                          project_signals=(".config/kilo/skills",), user_signals=(".config/kilo/skills",),
                          commands=("kilo", "kilocode")),
    "cursor": ClientProfile("Cursor", ".cursor", skills=False, instructions=None, rule=("rules/reql.mdc", "cursor"),
                            signals=("rule_dir",), commands=("cursor",)),
    "gemini": ClientProfile("Gemini CLI", ".gemini", windows_user_directory=".agents",
                            instructions="GEMINI.md", user_instructions_at_root=True, hook="settings.json",
                            signals=("skills", "instructions", "hook"), user_signals=(".gemini/skills",),
                            commands=("gemini",)),
    "copilot": ClientProfile("GitHub Copilot CLI and VS Code Copilot Chat", ".github",
                             skills=False, instructions="copilot-instructions.md", project_instructions_at_root=False,
                             rule=("instructions/reql.instructions.md", "copilot"), signals=("instructions", "rule_dir"),
                             user_signals=(".copilot/skills",), commands=("copilot",)),
    "openclaw": ClientProfile("OpenClaw", ".openclaw", commands=("openclaw",)),
    "hermes": ClientProfile("Hermes", ".hermes", commands=("hermes",)),
    "kimi": ClientProfile("Kimi Code", ".kimi", rule=("rules/reql.md", "markdown"), commands=("kimi",)),
    "antigravity": ClientProfile("Google Antigravity", ".antigravity", rule=("rules/reql.md", "markdown"),
                                 project_signals=(".agents/skills", ".agents/rules", ".agents/workflows"),
                                 user_signals=(".gemini/config/skills",), commands=("antigravity",)),
    "agents": ClientProfile("generic AGENTS-compatible clients", ".agents", install_all=False),
}
