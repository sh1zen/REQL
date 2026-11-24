"""Exercise native storage locking across process and recovery boundaries."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from queue import Queue
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from memory.domain.exceptions import StorageBusyError, StorageError
from memory.storage import BlockGraphStore, exclusive_store_lock, inspect_store_locks
from reql import MemoryGraph, MemoryNode


_CONTENDER = """
from contextlib import closing
import sys
from memory.domain.exceptions import StorageBusyError
from memory.storage import BlockGraphStore

try:
    with closing(BlockGraphStore(sys.argv[1], read_only=sys.argv[2] == "read")):
        print("open")
except StorageBusyError:
    print("busy")
"""

_CRASHING_WRITER = """
import sys
from memory.domain.models import MemoryNode
from memory.storage import BlockGraphStore

store = BlockGraphStore(sys.argv[1])
store.upsert_node(MemoryNode(id="committed", type="Concept", label="Durable WAL"))
print("ready", flush=True)
sys.stdin.read()
"""

_USAGE_WRITER = """
from contextlib import closing
import sys
from memory.storage import BlockGraphStore

with closing(BlockGraphStore(sys.argv[1], read_only=True)) as store:
    sys.stdin.read(1)
    for _ in range(30):
        store.record_usage_event("child", [{"id": "result"}])
"""


class StoreLockTests(unittest.TestCase):
    """Check real handles and filesystem state through the storage entry point."""

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.target = Path(directory.name) / "graph.reql"

    def _contend(self, mode: str) -> str:
        """Try a default store open from a separate interpreter."""
        return subprocess.run(
            [sys.executable, "-c", _CONTENDER, str(self.target), mode],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()

    def _contend_locally(self, mode: str) -> str:
        """Try a separate handle without waiting for the current handle."""
        try:
            with closing(BlockGraphStore(self.target, read_only=mode == "read", lock_timeout_seconds=0)):
                return "open"
        except StorageBusyError:
            return "busy"

    def test_opens_enforce_shared_and_exclusive_access(self) -> None:
        self.assertFalse(inspect_store_locks(self.target)["locked"])
        self.assertEqual(list(self.target.parent.iterdir()), [])
        for contend in (self._contend_locally, self._contend):
            with self.subTest(contender=contend.__name__):
                with closing(BlockGraphStore(self.target)):
                    self.assertEqual(inspect_store_locks(self.target)["mode"], "write")
                    self.assertEqual(contend("read"), "busy")
                    self.assertEqual(contend("write"), "busy")
                with closing(BlockGraphStore(self.target, read_only=True)):
                    self.assertEqual(inspect_store_locks(self.target)["mode"], "read")
                    self.assertEqual(contend("read"), "open")
                    self.assertEqual(contend("write"), "busy")
                self.assertEqual(contend("write"), "open")
                self.assertFalse(inspect_store_locks(self.target)["locked"])

    def test_killed_writer_releases_its_lock_and_reopens_from_wal(self) -> None:
        with subprocess.Popen(
            [sys.executable, "-c", _CRASHING_WRITER, str(self.target)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        ) as process:
            self.assertIsNotNone(process.stdout)
            ready = Queue()
            reader = Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
            reader.start()
            try:
                self.assertEqual(ready.get(timeout=10).strip(), "ready")
                self.assertEqual(inspect_store_locks(self.target)["mode"], "write")
            finally:
                process.kill()
                process.wait(timeout=10)
                reader.join(timeout=10)
        self.assertFalse(inspect_store_locks(self.target)["locked"])
        with closing(BlockGraphStore(self.target)) as reopened:
            self.assertEqual(reopened.get_node("committed").label, "Durable WAL")

    def test_read_only_facade_observes_unpublished_writer_lock(self) -> None:
        with exclusive_store_lock(self.target):
            with self.assertRaises(StorageBusyError):
                MemoryGraph.open(self.target, read_only=True)
        with self.assertRaisesRegex(StorageError, "Cannot open missing REQL storage"):
            MemoryGraph.open(self.target, read_only=True)

    def test_closed_writer_cannot_mutate_a_store_held_by_its_successor(self) -> None:
        store = BlockGraphStore(self.target)
        store.close()
        with closing(BlockGraphStore(self.target)) as successor:
            with self.assertRaisesRegex(StorageError, "store is closed"):
                store.upsert_node(MemoryNode(id="unleased", type="Concept"))
            self.assertEqual(successor.count_nodes(), 0)

    def test_shared_graph_readers_serialize_usage_appends(self) -> None:
        BlockGraphStore(self.target).close()
        with (
            closing(BlockGraphStore(self.target, read_only=True)) as store,
            subprocess.Popen(
                [sys.executable, "-c", _USAGE_WRITER, str(self.target)],
                stdin=subprocess.PIPE, text=True,
            ) as process,
        ):
            self.assertIsNotNone(process.stdin)
            try:
                process.stdin.write("1")
                process.stdin.flush()
                for _ in range(30):
                    store.record_usage_event("parent", [{"id": "result"}])
                self.assertEqual(process.wait(timeout=10), 0)
            finally:
                if process.poll() is None:
                    process.kill()
        with closing(BlockGraphStore(self.target, read_only=True)) as reader:
            self.assertEqual(reader.usage_for_node("result")["usage_count"], 60)

if __name__ == "__main__":
    unittest.main()
