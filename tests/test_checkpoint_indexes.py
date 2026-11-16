"""Persist large administrative indexes with the existing compressed record codec."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, BinaryIO, Iterable
import unittest
from unittest.mock import patch

from memory.storage.adapters import block_store
from reql import BlockGraphStore, MemoryEdge, MemoryNode


class CheckpointIndexTests(unittest.TestCase):
    """Protect lookup results, Unicode values and recoverability across checkpoints."""

    def test_compressed_indexes_preserve_wal_replay_and_both_open_modes(self) -> None:
        """Multi-page indexes retain exact records and lexical lookup after reopening."""
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "memory.reql"
            with closing(BlockGraphStore(path, block_size=4096)) as store:
                with store.transaction():
                    for index in range(240):
                        store.upsert_node(MemoryNode(id=f"n:{index}", type="Function", text="invoice payment sharedword",
                            properties={"relative_path": f"src/invoice_{index}.py", "description": "caffè 東京", "index": index}))
                    store.upsert_edge(MemoryEdge(id="call", from_id="n:0", to_id="n:239", type="CALLS"))
                store.checkpoint_if_needed(wal_bytes_threshold=0)
                report = store.inspect_storage()
                self.assertGreater(report["compression"]["space_saved_ratio"], 0)
                store.update_node_fields("n:0", text="refund replacementword")
            for read_only in (True, False):
                with self.subTest(read_only=read_only), closing(BlockGraphStore(path, read_only=read_only)) as reopened:
                    self.assertEqual(reopened.count_nodes(), 240)
                    self.assertEqual(reopened.get_node("n:239").properties["description"], "caffè 東京")
                    self.assertEqual([n.id for n, _score in reopened.lexical_search("replacementword")], ["n:0"])
                    self.assertNotIn("n:0", {n.id for n, _score in reopened.lexical_search("sharedword", top_k=300)})
                    self.assertEqual(reopened.get_edge("call").to_id, "n:239")

    def test_compaction_writes_data_pages_before_encoding_the_root(self) -> None:
        """Keep completed data pages on disk while computing the root index."""
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "memory.reql"
            with closing(BlockGraphStore(path, block_size=4096)) as store:
                with store.transaction():
                    for index in range(16):
                        store.upsert_node(MemoryNode(id=f"n:{index}", type="Function", text="largeword " * 600))
                original_encode = block_store._encode_record
                encoded: Counter[str] = Counter()

                def encode(record: dict[str, Any]) -> bytes:
                    kind = record["kind"]
                    encoded[kind] += 1
                    if kind == "root_index":
                        self.assertTrue(writer.called)
                        stream = writer.call_args.args[0]
                        self.assertGreater(stream.tell(), store.block_size)
                    return original_encode(record)

                with patch.object(store, "_write_data_pages", wraps=store._write_data_pages) as writer:
                    with patch.object(block_store, "_encode_record", side_effect=encode):
                        report = store.compact_storage()
                self.assertEqual(encoded["node"], 16)
                self.assertEqual(encoded["lexical_index"], 1)
                self.assertEqual(report["records_after"], 19)

            with closing(BlockGraphStore(path, read_only=True)) as reopened:
                self.assertEqual(reopened.count_nodes(), 16)
                self.assertEqual(len(reopened.lexical_search("largeword", top_k=16)), 16)

    def test_failed_checkpoint_keeps_previous_file_and_durable_wal(self) -> None:
        """A streamed-write failure leaves the preceding checkpoint recoverable."""
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "memory.reql"
            with closing(BlockGraphStore(path, block_size=4096)) as store:
                store.upsert_node(MemoryNode(id="original", type="Function", text="originalword"))
                store.checkpoint_if_needed(wal_bytes_threshold=0)
                before = path.read_bytes()
                manifest = store.storage_manifest()
                store.upsert_node(MemoryNode(id="pending", type="Function", text="pendingword " + "padding " * 1200))
                wal = path.with_suffix(".reql.wal")
                before_wal = wal.read_bytes()

                def fail_after_page(stream: BinaryIO, pages: Iterable[bytes]) -> None:
                    stream.write(next(iter(pages)))
                    raise OSError("disk full")

                with patch.object(store, "_write_data_pages", side_effect=fail_after_page):
                    with self.assertRaisesRegex(OSError, "disk full"):
                        store.checkpoint_if_needed(wal_bytes_threshold=0)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(wal.read_bytes(), before_wal)
                self.assertEqual(store.storage_manifest(), manifest)
                self.assertFalse(path.with_suffix(".reql.tmp").exists())
            with closing(BlockGraphStore(path, read_only=True)) as reopened:
                self.assertEqual({n.id for n in reopened.all_nodes()}, {"original", "pending"})
                self.assertEqual([n.id for n, _score in reopened.lexical_search("pendingword")], ["pending"])
