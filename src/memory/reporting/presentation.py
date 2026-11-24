"""Common wording for Markdown list limits."""


def omitted_items(total: int, limit: int) -> list[str]:
    """Return a Markdown line when a display limit hides available items."""
    if limit and total > limit:
        return [f"- {total - limit} more omitted ({limit} of {total} shown)."]
    return []
