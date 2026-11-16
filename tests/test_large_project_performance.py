"""Checkpointed lexical postings must reflect deferred edits and deletions."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memory.domain.models import MemoryNode
from memory.storage import BlockGraphStore


class LargeProjectPerformanceTests(unittest.TestCase):
    """Exercise the index lifecycle with a small deterministic fixture."""

    def setUp(self) -> None:
        scratch = Path.cwd() / ".tmp" / "regression-tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_loaded_postings_reconcile_deferred_edits_and_deletions(self) -> None:
        """Both index directions must agree after checkpoint, lazy loading and pending writes."""
        path = self.root / "memory.reql"
        with closing(BlockGraphStore(path)) as store:
            store.upsert_node(MemoryNode(id="invoice", type="Function", label="invoice", text="invoice payment"))
            store.upsert_node(MemoryNode(id="obsolete", type="Function", text="obsolete transport"))
            store.checkpoint_if_needed(wal_bytes_threshold=0)
        with closing(BlockGraphStore(path, defer_lexical_index=True)) as store:
            store.update_node_fields("invoice", text="invoice refund")
            store.remove_node("obsolete")
            self.assertEqual([node.id for node, _score in store.lexical_search("refund")], ["invoice"])
            self.assertFalse(store.lexical_search("obsolete"))
            expected = {node_id: {term for term, postings in store._node_terms.items() if node_id in postings}
                        for node_id in store._node_term_index}
            self.assertEqual(store._node_term_index, expected)
            store.checkpoint_if_needed(wal_bytes_threshold=0)
        with closing(BlockGraphStore(path, read_only=True)) as reopened:
            self.assertEqual([node.id for node, _score in reopened.lexical_search("refund")], ["invoice"])
            self.assertFalse(reopened.lexical_search("obsolete"))

    def test_deferred_load_adopts_decoded_postings(self) -> None:
        """A large checkpoint needs one copy of its decoded forward postings."""
        path = self.root / "memory.reql"
        with closing(BlockGraphStore(path)) as store:
            store.upsert_node(MemoryNode(id="invoice", type="Function", text="invoice payment"))
            store.checkpoint_if_needed(wal_bytes_threshold=0)
        with closing(BlockGraphStore(path, read_only=True, defer_lexical_index=True)) as store:
            read_record = store._read_record_at
            decoded = {}

            def capture(block_id: int, frame_offset: int) -> dict:
                record = read_record(block_id, frame_offset)
                if record["kind"] == "lexical_index":
                    decoded.update(record["value"]["node_terms"])
                return record

            with patch.object(store, "_read_record_at", side_effect=capture):
                self.assertEqual([node.id for node, _score in store.lexical_search("payment")], ["invoice"])
            self.assertIs(store._node_terms["payment"], decoded["payment"])

if __name__ == "__main__":
    unittest.main()
