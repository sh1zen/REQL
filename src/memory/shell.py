"""Format command options for the host shell."""
from __future__ import annotations

import os
import shlex
from collections.abc import Sequence


def shell_argument(value: str) -> str:
    """Quote one literal argument for the host shell."""
    quoted = shlex.quote(value)
    if os.name == "nt" and (quoted != value or value.startswith("@")):
        return "'" + value.replace("'", "''") + "'"
    return quoted


def shell_option(name: str, value: str) -> str:
    """Quote an option, including values starting with a dash."""
    separator = "=" if value.startswith("-") else " "
    return f"{name}{separator}{shell_argument(value)}"


def shell_command(arguments: Sequence[str]) -> str:
    """Render a literal invocation for the host shell."""
    prefix = "& " if os.name == "nt" else ""
    return prefix + " ".join(map(shell_argument, arguments))
