"""Provider-independent contracts for test links and line observations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EvidenceKind = Literal["observed", "static", "candidate"]


@dataclass(frozen=True, slots=True)
class TestAssociation:
    test_id: str
    target_symbol_id: str
    evidence_kind: EvidenceKind
    evidence_ref: str | None
    source_revision: str | None
    source_hash: str | None
    verified_current: bool
    test_path: str | None = None
    target_path: str | None = None


@dataclass(frozen=True, slots=True)
class LineObservation:
    test_path: str
    test_name: str
    source_path: str
    line: int
    context: str
