"""Markdown report for project compilation cache state."""
from __future__ import annotations

from typing import Any

from ..config import default_config
from ..config.models import MarkdownReportPolicy
from .presentation import omitted_items


class CacheReportBuilder:
    def __init__(self, *, policy: MarkdownReportPolicy | None = None) -> None:
        self.policy = policy if policy is not None else default_config().reporting.markdown

    def render(self, context: dict[str, Any]) -> str:
        entries = context["cache_entries"]
        status = context.get("scan_status") or {}
        lines = [
            "# REQL Cache Report",
            "",
            f"Generated at: {context['generated_at']}",
            "",
            "## Cache summary",
            "",
            f"- Project id: `{context['project_id']}`",
            f"- Cached artifacts: {status.get('cached_artifacts', sum(1 for n in entries if n.status == 'active'))}",
            f"- Dirty artifacts: {status.get('dirty_artifacts', 0)}",
            f"- Deleted artifacts: {status.get('deleted_artifacts', 0)}",
            f"- Skipped artifacts: {len(context.get('skipped_files') or [])}",
            "",
            "## Cache entries",
        ]
        if not entries:
            lines.append("- No data yet.")
        else:
            lines.extend(
                f"- `{node.properties.get('relative_path', node.label)}` status={node.status} "
                f"compiled_at={node.properties.get('compiled_at', '')}"
                for node in sorted(entries, key=lambda n: str(n.properties.get("relative_path") or n.label))[:self.policy.cache_limit or None]
            )
            lines.extend(omitted_items(len(entries), self.policy.cache_limit))
        return "\n".join(lines).rstrip() + "\n"
