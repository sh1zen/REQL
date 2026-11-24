"""Lightweight artifact type and language detection."""
from __future__ import annotations

from codecs import getincrementaldecoder
from dataclasses import dataclass
from pathlib import Path
import re
import shlex

from ..code_analysis.catalog import display_language_for_path
from .models import ArtifactType
from .text_encoding import UTF16_BOMS, utf16_encoding

TEXT_EXTENSIONS = {
    ".css",
    ".htm",
    ".html",
    ".rts",
    ".txt",
    ".rst",
    ".log",
}

DATA_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".xml",
    ".ndjson",
}

UNSUPPORTED_MEDIA_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".png",
    ".tif",
    ".tiff",
    ".webm",
    ".webp",
}

CONFIG_EXTENSIONS = {
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".conf",
}

CONFIG_LANGUAGES = {
    ".json": "JSON",
    ".toml": "TOML",
    ".yaml": "YAML",
    ".yml": "YAML",
}

TEXT_LANGUAGES = {
    ".css": "CSS",
    ".htm": "HTML",
    ".html": "HTML",
    ".rst": "reStructuredText",
    ".rts": "Text",
    ".sql": "SQL",
}

MARKDOWN_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+\S")
MARKDOWN_FENCE_RE = re.compile(r"^ {0,3}(?:```|~~~)")
MARKDOWN_SETEXT_RE = re.compile(r"^\s*(?:=+|-+)\s*$")


@dataclass(frozen=True, slots=True)
class Classification:
    artifact_type: ArtifactType
    language: str | None
    binary: bool


def is_binary_sample(sample: bytes) -> bool:
    """Distinguish binary bytes from supported text encodings."""
    return utf16_encoding(sample) is None and _is_binary_bytes(sample)


def _is_binary_bytes(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample or sample.startswith(UTF16_BOMS):
        return True
    text_bytes = {7, 8, 9, 10, 12, 13, 27}
    non_text = sum(1 for byte in sample if byte < 32 and byte not in text_bytes)
    return non_text / len(sample) > 0.30


def classify_path(path: str | Path, sample: bytes) -> Classification:
    target = Path(path)
    suffix = target.suffix.casefold()
    lower_name = target.name.casefold()

    if sample.startswith(b"%PDF-"):
        return Classification("pdf", "PDF", True)

    if suffix == ".pdf":
        return Classification("pdf", "PDF", True)

    encoding = utf16_encoding(sample)
    if encoding is None and _is_binary_bytes(sample):
        return Classification("binary", None, True)

    code_language = display_language_for_path(path)
    if code_language:
        return Classification("code", code_language, False)
    if suffix in {".md", ".markdown"}:
        return Classification("markdown", "Markdown", False)
    if lower_name in {"license", "notice"}:
        return Classification("text", None, False)
    head = _decode_sample(sample[:512], encoding)
    if suffix not in TEXT_EXTENSIONS and suffix not in CONFIG_EXTENSIONS and suffix not in DATA_EXTENSIONS:
        if (interpreter := _shebang_language(head)) is not None:
            return Classification("code", interpreter, False)
    if suffix in {"", ".txt"} and _looks_like_structured_markdown(_decode_sample(sample, encoding)):
        return Classification("markdown", "Markdown", False)
    if suffix in CONFIG_EXTENSIONS:
        return Classification("config", CONFIG_LANGUAGES.get(suffix), False)
    if suffix in DATA_EXTENSIONS:
        return Classification("data", None, False)
    if suffix in TEXT_EXTENSIONS:
        return Classification("text", TEXT_LANGUAGES.get(suffix), False)

    head = head.lstrip()
    if head.startswith(("{", "[")):
        return Classification("data", "JSON", False)
    if head.startswith("# "):
        return Classification("markdown", "Markdown", False)
    return Classification("unknown", None, False)


def _shebang_language(text: str) -> str | None:
    """Classify the interpreter token, including common env wrappers."""
    if not text.startswith("#!"):
        return None
    try:
        tokens = shlex.split(text.splitlines()[0][2:])
    except ValueError:
        return None
    if not tokens:
        return None
    interpreter = Path(tokens[0]).name.casefold()
    if interpreter == "env":
        arguments = iter(tokens[1:])
        for token in arguments:
            if token in {"-S", "--split-string", "-i", "--ignore-environment", "--"} or "=" in token:
                continue
            if token in {"-u", "--unset", "-C", "--chdir"}:
                next(arguments, None)
                continue
            interpreter = Path(token).name.casefold()
            break
    if re.fullmatch(r"(?:python|pypy)(?:[23](?:\.\d+)?)?", interpreter):
        return "Python"
    return {
        "bash": "Bash", "sh": "Bash", "dash": "Bash",
        "node": "JavaScript", "nodejs": "JavaScript",
        "php": "PHP", "ruby": "Ruby", "pwsh": "PowerShell", "powershell": "PowerShell",
    }.get(interpreter)


def _decode_sample(sample: bytes, encoding: str | None) -> str:
    """Decode a prefix that can end partway through the final character."""
    if encoding is None:
        return sample.decode("utf-8-sig", errors="ignore")
    return getincrementaldecoder(encoding)(errors="ignore").decode(sample, final=False)


def _looks_like_structured_markdown(sample: str) -> bool:
    """Recognize Markdown markup in a bounded sample, irrespective of document names."""
    lines = sample.splitlines()
    return any(
        MARKDOWN_HEADING_RE.match(line)
        or MARKDOWN_FENCE_RE.match(line)
        or (index > 0 and lines[index - 1].strip() and MARKDOWN_SETEXT_RE.match(line))
        for index, line in enumerate(lines)
    )


def is_unsupported_media_file(path: str | Path, sample: bytes) -> bool:
    suffix = Path(path).suffix.casefold()
    if suffix in UNSUPPORTED_MEDIA_EXTENSIONS:
        return True
    return (
        sample.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
        or sample.startswith(b"RIFF") and sample[8:12] == b"WEBP"
        or len(sample) >= 12 and sample[4:8] == b"ftyp"
    )
