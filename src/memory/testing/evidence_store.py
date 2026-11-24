"""Transactional test observations kept in a separate local graph file."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..config import StorageConfig, default_config
from ..domain.ids import stable_id
from ..domain.models import MemoryNode
from ..storage import BlockGraphStore
from .coverage_import import local_path


class TestEvidenceStore:
    """Use canonical store locking and WAL semantics for independent runtime data."""

    def __init__(self, storage: Path, *, policy: StorageConfig | None = None) -> None:
        self.policy = policy or default_config().storage
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
        store = BlockGraphStore(
            self.path, read_only=read_only, defer_lexical_index=True, wait_for_lock=True, policy=self.policy,
        )
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
