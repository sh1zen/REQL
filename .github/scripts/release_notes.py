"""Write GitHub release notes from the tagged checkout's changelog."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def release_notes(changelog: str, tag: str) -> str:
    """Return the Markdown body of the unique heading for the exact tag."""
    heading = rf"^## \[{re.escape(tag)}\](?: - [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})?[ \t]*$"
    sections = list(re.finditer(heading, changelog, re.MULTILINE))
    if len(sections) != 1:
        raise ValueError(f"CHANGELOG.md must contain exactly one section for {tag}.")

    body = changelog[sections[0].end():]
    next_heading = re.search(r"^## ", body, re.MULTILINE)
    notes = (body[:next_heading.start()] if next_heading else body).strip("\n")
    if not notes.strip():
        raise ValueError(f"CHANGELOG.md has no release notes for {tag}.")
    return notes + "\n"


def main() -> None:
    """Read the changelog in the working directory and write a UTF-8 notes file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    try:
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        notes = release_notes(changelog, args.tag)
        args.output.write_text(notes, encoding="utf-8", newline="\n")
    except (OSError, ValueError) as error:
        parser.exit(status=1, message=f"{error}\n")


if __name__ == "__main__":
    main()
