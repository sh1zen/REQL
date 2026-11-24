"""Extract structural features from static template markup."""

from __future__ import annotations

from html.parser import HTMLParser


_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


class _MarkupStructure(HTMLParser):
    """Track nested elements, attribute names and consecutive element patterns."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.sequence: list[str] = []
        self.features: set[str] = set()

    def _element(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = ",".join(sorted({name for name, _ in attrs}))
        descriptor = f"{len(self.stack)}:{tag}[{names}]"
        parent = self.stack[-1] if self.stack else "root"
        self.sequence.append(descriptor)
        self.features.update((f"edge:{parent}>{tag}", f"node:{descriptor}"))
        if len(self.sequence) >= 3:
            self.features.add("sequence:" + "/".join(self.sequence[-3:]))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._element(tag, attrs)
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._element(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index] == tag:
                del self.stack[index:]
                break


def markup_structure(text: str) -> tuple[int, set[str]]:
    """Read literal markup and disregard comments, values and script bodies."""
    parser = _MarkupStructure()
    try:
        parser.feed(text)
        parser.close()
    except AssertionError:
        # HTMLParser rejects some malformed declarations. No complete signature exists.
        return 0, set()
    return len(parser.sequence), parser.features


class _MarkupSurface(HTMLParser):
    """Collect literal attributes from start tags, excluding markup in comments and scripts."""

    def __init__(self) -> None:
        super().__init__()
        self.attributes: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.attributes.extend((name, value) for name, value in attrs if name in {"class", "id", "name", "type"} and value)


def markup_surface_attributes(text: str) -> list[tuple[str, str]]:
    """Return application-surface attributes on static HTML tags."""
    parser = _MarkupSurface()
    try:
        parser.feed(text)
        parser.close()
    except AssertionError:
        # A malformed declaration can interrupt HTMLParser after valid tags.
        return parser.attributes
    return parser.attributes
