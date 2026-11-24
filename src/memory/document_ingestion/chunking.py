"""Paragraph-oriented chunking helpers."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParagraphChunk:
    text: str
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int


def paragraph_chunks(text: str, *, max_chars: int = 3000) -> list[ParagraphChunk]:
    """Chunk nonblank paragraphs while retaining coordinates in the original text."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    chunks: list[ParagraphChunk] = []
    offsets: list[int] = []
    offset = paragraph_start = 0
    for line in text.splitlines(keepends=True):
        offsets.append(offset)
        if not line.strip():
            _flush_paragraph(chunks, text, offsets, paragraph_start, offset, max_chars)
            paragraph_start = offset + len(line)
        offset += len(line)
    _flush_paragraph(chunks, text, offsets, paragraph_start, len(text), max_chars)
    return chunks


def _flush_paragraph(
    chunks: list[ParagraphChunk],
    text: str,
    offsets: list[int],
    start: int,
    end: int,
    max_chars: int,
) -> None:
    """Skip surrounding whitespace and map each retained slice to its own lines."""
    while start < end:
        while start < end and text[start].isspace():
            start += 1
        stop = min(start + max_chars, end)
        piece = text[start:stop].rstrip()
        if piece:
            piece_end = start + len(piece)
            chunks.append(ParagraphChunk(
                "\n".join(piece.splitlines()), bisect_right(offsets, start), bisect_right(offsets, piece_end - 1),
                start, piece_end,
            ))
        start = stop
