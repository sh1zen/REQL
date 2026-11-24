# Storage

REQL 0.3.0 continues to read the existing block format. Keep the canonical
graph in place and run `reql project compile` to rebuild derived edges and
record the current file manifest. The compiler invalidates older parser and
resolver fingerprints automatically. Use the same REQL version for all writers
to a graph when upgrading. The separate test evidence sidecar uses the
existing block format and can be recreated by reimporting its reports.

REQL uses a storage-agnostic `memory.storage.GraphStore` contract. The bundled local adapter is
`memory.storage.adapters.BlockGraphStore`, re-exported as
`memory.storage.BlockGraphStore`, a dependency-light graph store that writes
fixed-size block files directly instead of using a database engine.

## Block Adapter

`BlockGraphStore` stores graph data in REQL block files:

- block 0 is a superblock manifest;
- every disk block has a fixed size, 64 KiB by default;
- each block maps cleanly to the in-process page cache;
- records are length-prefixed binary payloads, with compressed administrative indexes;
- node records are packed near ordinary incident relationships to preserve
  graph-neighborhood locality;
- relationships for dense nodes are written as dedicated dense-edge records so
  a high-degree node does not make its node page expensive to load.

The superblock is the durable entry point for validation and recovery. It
records the storage format, manifest version, schema version,
block size, root index offset, data block count, generation id, and a SHA-256
checksum for the data region. Opening a manifested file validates the manifest
checksum first, then validates the data checksum before replaying block records.
The manifest requires an explicit `checksum_algorithm: "sha256"`. Version,
size, offset, block-count and generation fields must be JSON integers; strings,
booleans and floating-point values are rejected. Incomplete manifests require
a rebuild with the current REQL version. Storage opens require store schema
2, root-index version 3, lexical schema 2 and the `binary-v2` record codec.
Writable, read-only and deferred opens use the same format validation; errors
leave the checkpoint untouched.

For the current format, `data_offset` points to the first data block after the
superblock and `root_index_offset` points to the persisted root-index record
inside the data region.

Record encoding, compression and multi-block records are described under
[Compression and dynamic records](#compression-and-dynamic-records).

Writable sessions append canonical graph changes to a sidecar WAL,
`<store>.wal`, after the initial checkpoint exists. Opening a store validates
the checkpoint, loads the persisted root index, replays the WAL if present, and
avoids materializing graph records until an indexed lookup needs them. WAL replay
coalesces repeated node/edge frames before updating indexes, and lexical
reindexing is bounded per node so a WAL-only recovery does not become quadratic
on large source fragments. `close()` does not rewrite the full graph; it only
ensures pending WAL frames are durable. Compile/update and normal CLI opens
checkpoint automatically when the WAL is large or the base checkpoint is
missing, so `reql storage compact` is maintenance, not a required post-compile
step. Checkpoint flushes encode each graph record once and write completed
pages to a temporary file while computing their checksum. The lexical snapshot
borrows the existing postings. Root and lexical indexes stream sorted JSON in
bounded batches into zlib using the existing compressed-record flag. The root
index estimates its own page use by packing only the final pages. The writer
adds the superblock after the checksum is complete and atomically replaces the
checkpoint before removing the WAL. Graph state, record locations, and the
current compressed record still require memory.

Query usage signals are stored separately in `<store>.usage.jsonl`. Retrieval
can update usage while the graph is opened read-only, and ranking loads that
overlay on open without rewriting canonical node or edge records.

Each checkpoint also stores a `root_index` record. It captures record locations,
canonical node keys, edge patterns, incoming/outgoing adjacency, type/status
buckets, selected property indexes, counters, and a block space map. Lexical
postings live in a separate `lexical_index` record referenced by root-index
version 3. Read-only CLI/MCP sessions load the lexical record on first search;
metadata reads need only structural indexes. Compilation sessions defer the
lexical record and append changed nodes to the WAL, so a small graph delta does
not pay the full lexical-index startup cost. This also applies to a newly created
store. Repeated updates to the same node are coalesced before the first query or
checkpoint builds the postings. The deferred session tracks an exact
old/new lexical overlay: an in-process query applies it before searching, rollback
restores it transactionally, and an automatic checkpoint loads and folds it into
the persisted postings once the WAL threshold is reached. Deferral therefore
does not create stale same-process queries or an indefinitely growing WAL.

Loading the split lexical record builds both term-to-node and node-to-term indexes
together. Neither direction is published after a conversion failure; pending edits
remain available for retry.

`top_nodes_by_degree` preserves defensive copies by default and copies only the
selected rows after ranking. Internal read-only projections may pass
`clone=False`, matching other borrowed store reads; borrowed nodes must not be
mutated or returned as public mutable state.

The adapter keeps operational indexes available for deterministic graph access:

- node id;
- `(type, canonical_key)`;
- edge id;
- `(from_id, to_id, type)`;
- incoming and outgoing adjacency lists;
- deterministic lexical terms by term and node;
- type/status buckets;
- selected scalar property indexes for artifact, cache, source, and project
  query fields.

This keeps retrieval, activation, cache inspection, project compilation, and
common REQL query paths bounded without relying on SQL or a graph database
service.

The block adapter owns the lexical index and uses stateless term scoring
helpers in `memory.storage.adapters.lexical_index`. Import the store
from `memory.storage`, `memory.storage.adapters`, or the public `reql` package.
See [clean rebuilds](#inspection-compaction-and-clean-rebuilds) for explicit
project storage maintenance.

Indexing and retrieval share immutable short-text analysis. Its process-local
cache retains at most 1,024 strings of up to 2,048 characters; larger text uses
the same uncached algorithm. Public token lists remain independent of cached values.

Lexical postings cover complete node text, labels, canonical keys and supported
searchable properties. They include identifier components from camelCase,
snake_case, hyphenated names and paths, plus conservative singular variants.
Searches apply the final score, including salience, to every eligible match.
When a caller sets `top_k`, the adapter retains the highest-scoring rows.

## Operational settings

`core.yaml` defines the block size for new stores, page cache size, dense-node
layout, and WAL checkpoint threshold. Each existing checkpoint supplies its
own block size. The block, manifest, record, and WAL identifiers are
persistence contracts. See [configuration](CONFIGURATION.md#storage-and-workspace-policy).

## Data Locality

During flush, the store writes node records ordered by type, creation time, and
id. For ordinary nodes, incident edges are emitted immediately after
the node when possible. This means loading a block usually brings a useful
neighborhood into memory together: the node, its properties, and nearby
relationships.

Dense nodes are detected by degree. When a node's incident relationship count
is at or above the dense-node threshold, its edges are emitted in dense-edge
records after the node section. The node remains small and cheap to load, while
its large relationship set remains indexed and queryable through adjacency
indexes.

## Compression And Dynamic Records

Fixed node and edge fields are encoded as typed binary values; dynamic
`properties` and administrative records use deterministic JSON inside the binary
frame. The writer compresses root and lexical index records while encoding them,
without first creating a complete uncompressed JSON string or UTF-8 copy.
Node/edge records and smaller administrative kinds retain their existing
uncompressed encoding. The reader continues to accept both compressed and
uncompressed records in the current format; no schema or codec version changes.
If an encoded logical record is larger than one block, the checkpoint writes it
as consecutive record-part frames and reconstructs it when the store opens.

## Inspection, Compaction, And Clean Rebuilds

Use `reql storage inspect` to print block-level diagnostics for the selected
storage file. The report includes block count, record count by kind,
compressed and uncompressed payload bytes, compression ratio, dense-node count,
manifest metadata, WAL status, root-index counts, space-map free bytes, indexed
record counts, and loaded cache counts. Pass `--json` for a structured payload
suitable for monitoring or regression tests.

Use `reql storage compact` after large maintenance runs or many archive
operations when you want an explicit storage rewrite or to reclaim checkpoint
space immediately. The graph is already query-ready before compaction because
automatic checkpoints and bounded WAL replay are part of normal operation. The
command reloads the logical graph, writes a fresh compact generation, and
reports generation id, block count, record count, and byte size before and after
compaction. It does not itself delete archived graph records. Successful
compilation operations that create a changed-manifest `ProjectRevision`
enforce the graph-level `retention.commits` policy and invoke compaction only
when they actually remove graph records.
History selection applies limits after ordering by committed revision sequence
or delta creation time. Retention boundaries use committed history timestamps;
later activation, usage, or analysis updates cannot change the retained revisions.

Use `reql storage clear` when archived records, compilation history, and
other state that no longer belongs to the current project tree must be removed.
REQL compiles `PATH` into a temporary block store while holding the destination
writer lock, compacts and validates that clean generation, and atomically
replaces the old store only after a successful compile. The artifact cache is
rebuilt in the same maintenance window; failures restore the previous cache and
leave the old store untouched. A successful clear also removes the old WAL and
usage journal.

## Agent Memory Isolation

Private stores under `.reql/agents/` and the shared `agent-dashboard.reql` use
the block adapter but contain operational records rather than project graph
replicas. Agent initialization and shared-work retrieval do not open the
canonical graph. Lifecycle cleanup locks private stores and defers busy stores;
the last active session releases shared private scratch.

See [Engineering coordination](COORDINATION.md) for checkpoint creation,
session/public-message retention and independent shared-work retention.

## Reader/Writer Locking

The block adapter uses native locks on the local filesystem. Windows uses
[`LockFileEx`](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex).
POSIX uses [`flock`](https://docs.python.org/3/library/fcntl.html#fcntl.flock).
The adapter locks a stable file at
`<store>.lock/access`. Keep this file and its directory in place while REQL
processes can access the store. A process can open the file while the lock is
free or busy. The operating system determines access.

- Read-only opens take a shared lock. Multiple readers can use the same store.
- Writable opens and maintenance take an exclusive lock. A writer excludes
  every other opener until the writer closes the store.
- A default open returns a busy error immediately if the requested lock is
  unavailable. Pass `wait_for_lock=True` to `MemoryGraph.open` or `BlockGraphStore`
  for a native blocking acquisition. `lock_timeout_seconds` sets a bounded wait
  for callers that need one.
- Workspace and coordination store opens use native blocking acquisition.
  Retention tries the private writer lock once and defers work if it is busy.
- The operating system releases the lock when its process exits. The next
  writer opens the checkpoint and replays the WAL.

Open each store in the process that uses it. If workers use `fork`, start
them before opening the store to avoid inheriting its file descriptor.

The graph lock covers the checkpoint, WAL, and memory map for the lifetime of
the open store. Checkpoint publication runs under the exclusive lock. The
adapter closes the current memory map before replacing the checkpoint. If an
external process blocks the replacement, REQL reports the filesystem error
and keeps the previous checkpoint and WAL available.

Read-only CLI and MCP commands can record query usage in
`<store>.usage.jsonl`. The journal has an independent native lock under
`<store>.usage.jsonl.lock/access`. Journal readers take a shared lock for
the read. Appends and maintenance take an exclusive lock. Acquire the graph
lock before the journal lock. The standard read-only `MemoryGraph.open`
requires an existing checkpoint or WAL payload.

```bash
reql storage locks
reql storage locks --json
```

This command probes the lock without opening the graph. The JSON `mode`
is `read` for shared access, `write` for exclusive access, and `null` when free.
The result describes the state at the time of the probe.

## Transactions

The adapter exposes `store.transaction()` through the `GraphStore` contract.

- each transaction journals touched nodes, edges, pending WAL state, and small
  mutable side structures;
- inner rollback restores its journal while preserving outer work;
- rollback restores touched records and rebuilds indexes, including deferred
  lexical postings;
- the outer commit appends pending WAL records once;
- `storage compact` is the explicit full-checkpoint path.

Project graph updates, cache-entry nodes and run/delta persistence share this
transaction boundary. The [compilation flow](PIPELINE.md#flow) documents the
separate disk-cache writes and failure recovery.

## Batch And Bounded Operations

Bulk reads and neighbor traversal clone records by default. Internal stages
may request borrowed views but must not mutate them or return them as public
mutable state. Final bounded public results are copied.

The port includes helpers for storage-efficient operations:

- `batch_upsert_nodes`;
- `batch_upsert_edges`;
- `find_nodes_by_property`;
- `find_edges_by_property`;
- `incident_edges`;
- `count_nodes`;
- `count_edges`;
- `node_type_counts`;
- `top_nodes_by_degree`;
- `archive_nodes_by_artifact`;
- `bounded_neighborhood`;
- `update_node_metrics`;
- `persist_analysis_results`.

These methods keep routine project compilation, artifact cache inspection, and
activation/retrieval operations bounded instead of loading the whole graph.
Query, analysis, and reporting paths should prefer these APIs for pagination,
counts, and ranked candidates; `all_nodes` and `all_edges` should be reserved
for intentional full exports or explicit administrative scans.

## Storage adapters

Implement `GraphStore` from `memory.storage.graph_store`.

A backend must support:

- node upsert;
- edge upsert;
- id and canonical-key lookup;
- adjacency queries;
- lexical seed search;
- degree calculation;
- export.

The service layer should not assume any database-engine-specific behaviour.
Pass the backend to `MemoryGraph(store)`; the bundled block adapter implements
the same port.

Adapters implement `lexical_search(..., project_id=..., clone=...)` and
`find_nodes_by_types(..., clone=...)` as part of the storage port. When set,
`project_id` restricts lexical candidates before ranking and applying the limit.
`clone=False` returns borrowed records for internal read-only stages. Adapter
errors propagate to callers.
`count_nodes` and `count_edges` are also required port operations. `STATS` calls
them directly; adapters must implement their filters and return integer counts.
