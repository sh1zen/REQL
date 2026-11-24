"""Detection and decoding of UTF-16 source and document text."""
from __future__ import annotations

from codecs import BOM_UTF16_BE, BOM_UTF16_LE, BOM_UTF32_LE, getincrementaldecoder


TEXT_SAMPLE_BYTES = 8192
UTF16_BOMS = (BOM_UTF16_LE, BOM_UTF16_BE)
_TEXT_CONTROLS = "\a\b\t\n\f\r\x1b"


def utf16_encoding(sample: bytes) -> str | None:
    """Recognize UTF-16 BOMs and a strong alternating-null pattern in text."""
    if sample.startswith(BOM_UTF32_LE):
        return None
    if sample.startswith(UTF16_BOMS):
        encoding = "utf-16"
    elif b"\x00" not in sample:
        return None
    else:
        even, odd = sample[::2], sample[1::2]
        even_nulls, odd_nulls = even.count(b"\x00"), odd.count(b"\x00")
        if even_nulls >= 2 and not odd_nulls and even_nulls * 2 >= len(even):
            encoding = "utf-16-be"
        elif odd_nulls >= 2 and not even_nulls and odd_nulls * 2 >= len(odd):
            encoding = "utf-16-le"
        else:
            return None
    try:
        # A scanner sample can end inside a code unit or a surrogate pair.
        text = getincrementaldecoder(encoding)().decode(sample, final=False)
    except UnicodeDecodeError:
        return None
    if any(ord(char) < 32 and char not in _TEXT_CONTROLS for char in text):
        return None
    return encoding


def decode_text(content: bytes, *, default_encoding: str = "utf-8-sig", errors: str = "strict") -> str:
    """Decode text as UTF-16 when detected, using the caller's default otherwise."""
    return content.decode(utf16_encoding(content[:TEXT_SAMPLE_BYTES]) or default_encoding, errors=errors)
