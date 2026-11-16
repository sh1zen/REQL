"""Transactional test observations kept in a separate local graph file."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from typing import Any, Iterator

from ..domain.ids import stable_id
from ..domain.models import MemoryNode
from ..storage import BlockGraphStore, StoreLease
from .coverage_import import local_path


class VerificationPlanChanged(ValueError):
    """The check selection in the handoff has been replaced."""

    def __init__(self) -> None:
        super().__init__("Saved verification plan changed. Recover the current task brief and review its checks.")


class VerificationEvidenceChanged(ValueError):
    """The pass used for completion has been superseded by different evidence."""

    def __init__(self) -> None:
        super().__init__("Saved verification evidence changed. Review the latest checks before completing.")


def verification_plan(receipt: dict[str, Any]) -> dict[str, Any]:
    """Identify check selection independently of a run's inputs and outcome."""
    plan = {key: receipt[key] for key in ("task_id", "patterns", "cargo", "cargo_test", "diff", "extra_inputs") if key in receipt}
    normalized = {**plan, "cargo": plan.get("cargo") or []}
    return {**plan, "id": stable_id("checks", json.dumps(normalized, sort_keys=True, separators=(",", ":")))}


def require_verification_plan(receipt: dict[str, Any] | None, expected: str | None = None) -> dict[str, Any]:
    """Resolve a runnable saved selection and honour an observed plan identity."""
    plan = verification_plan(receipt) if receipt is not None else None
    if expected is not None and (plan is None or plan["id"] != expected):
        raise VerificationPlanChanged()
    if plan is None or not (plan["patterns"] or plan.get("cargo") or plan.get("cargo_test") or plan["diff"]):
        raise ValueError("No saved checks for this task")
    return plan


class TestEvidenceStore:
    """Use canonical store locking and WAL semantics for independent runtime data."""

    def __init__(self, storage: Path) -> None:
        self.path = storage.with_name("test-evidence.reql")
        self.canonical_path = storage

    @contextmanager
    def open(self, *, read_only: bool) -> Iterator[BlockGraphStore | None]:
        wal = self.path.with_name(f"{self.path.name}.wal")
        if read_only and not (self.path.exists() or wal.exists()):
            yield None
            return
        if self.path.resolve() == self.canonical_path.resolve():
            if read_only:
                yield None
                return
            raise ValueError("Choose a canonical graph filename other than test-evidence.reql before importing")
        root = self.path.parent.resolve()
        for path in (self.path, wal):
            local_path(root, path.absolute())
        store = BlockGraphStore(self.path, read_only=read_only, defer_lexical_index=True)
        try:
            yield store
        finally:
            store.close()

    def record(self, rows: dict[str, dict[str, Any]]) -> int:
        if not rows:
            return 0
        added = 0
        with self.open(read_only=False) as store:
            assert store is not None
            with store.transaction():
                for key, properties in sorted(rows.items()):
                    row_id = stable_id("test-observation", key)
                    if store.get_node(row_id, clone=False) is not None:
                        continue
                    store.upsert_node(MemoryNode(id=row_id, type="TestObservation", canonical_key=key,
                                                 properties={**properties, "name": properties["target_symbol_id"]}), return_clone=False)
                    added += 1
        return added

    def verification(self, root: str, task_id: str) -> dict[str, Any] | None:
        """Read the latest assigned check bundle without opening the source graph."""
        with self.open(read_only=True) as store:
            if store is None:
                return None
            node = store.get_node(stable_id("test-verification", root, task_id), clone=False)
            return dict(node.properties) if node is not None else None

    @contextmanager
    def verification_lease(self, root: Path, task_id: str, *, timeout: int) -> Iterator[None]:
        """Serialize verification for one task without locking either graph during checks."""
        identity = stable_id("verification", str(root.resolve()), task_id).split(":", 1)[1]
        parent = self.path.parent.resolve()
        directory = local_path(parent, self.verification_locks.absolute())
        directory.mkdir(parents=True, exist_ok=True)
        # On Windows an absent directory can resolve through a different alias
        # once either process creates it. Resolve the existing parent first.
        directory = local_path(parent, directory)
        target = directory / identity
        local_path(directory, target.with_name(f"{target.name}.lock"))
        with StoreLease(target, timeout_seconds=timeout):
            yield

    @property
    def verification_locks(self) -> Path:
        """Locate transient verification leases alongside the evidence store."""
        return self.path.with_name(f"{self.path.name}.verify")

    @contextmanager
    def guard_verification_plan(self, root: str, task_id: str, expected: str | None, *,
                                verified: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Keep the selected recipe stable for a short write outside the evidence store."""
        with self.open(read_only=True) as store:
            node = store.get_node(stable_id("test-verification", root, task_id), clone=False) if store is not None else None
            observed = node.properties if node else None
            plan = require_verification_plan(observed, expected)
            if verified is not None and (observed is None or any(
                observed.get(key) != verified.get(key) for key in (
                    "fingerprint", "runner", "environment", "python_runner", "source_check_revision", "checks",
                )
            )):
                raise VerificationEvidenceChanged()
            yield plan

    def record_verification(self, receipt: dict[str, Any], *, expected_plan: str | None = None) -> None:
        """Replace the last observed result for this workspace and assignment."""
        node_id = stable_id("test-verification", receipt["root"], receipt["task_id"])
        with self.open(read_only=False) as store:
            assert store is not None
            if expected_plan is not None:
                prior = store.get_node(node_id, clone=False)
                require_verification_plan(prior.properties if prior else None, expected_plan)
            store.upsert_node(MemoryNode(id=node_id, type="TestVerification", canonical_key=node_id,
                                         properties=receipt), return_clone=False)
