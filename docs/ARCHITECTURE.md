# Architecture

## Goal

REQL compiles repository facts into a local property graph and combines bounded
source retrieval with separately stored engineering work. Core compilation,
queries, analysis and storage require no model calls or external graph service.

## Layers

```text
Python API / CLI / MCP
        -> application services
        -> deterministic engines and projections
        -> GraphStore port
        -> local block adapter

Domain models define the records and contracts shared by these layers.
```

| Owner | Responsibility |
| --- | --- |
| `src/config.py` | Release version and sample project configuration |
| `src/config/` | Bundled YAML for core policy and project defaults |
| `src/memory/config` | Configuration loading, models, and override validation |
| `src/reql.py` | Public Python imports, including compatibility aliases for configuration and queries |
| `src/api` | Public `MemoryGraph` facade and exported models |
| `src/memory/services` | Compilation, retrieval and coordination |
| `src/memory/artifacts` | Scanning, compilation, cache, revisions and deltas |
| `src/memory/code_analysis` | Registered Tree-sitter parsers and extractors |
| `src/memory/testing` | Test association evidence and transactional observation import |
| `src/memory/document_ingestion` / `extraction` | Document parsing and deterministic term processing |
| `src/memory/analysis` / `engines` | Communities, hubs, activation and salience |
| `src/memory/explanation` / `pipeline` | Read-only repository and flow projections |
| `src/memory/storage` | Storage port and block adapter |
| `src/mcp` | Configured tool limits, protocol negotiation and stdio/HTTP transport |
| `src/agents` | Client profiles, generated guidance, installation and hooks |

## Storage Boundary

`memory.storage.GraphStore` defines reads, writes, indexed lookups,
neighborhoods, counts and transactions. Compilation uses complete indexed
reads when reconciling persisted records. The bundled `BlockGraphStore` owns
block persistence, transaction journals, lazy loading, checkpoint serialization
and index lifecycle. Its public package exports resolve to the same class.
Stateless lexical scoring lives in `memory.storage.adapters.lexical_index`.
The adapter indexes eligible terms from full node text and searchable metadata.
It records the index policy in each checkpoint and rebuilds postings from
current nodes when the saved policy differs.
The configuration loader owns lexical settings and reads storage policy from
`core.yaml`. The adapter owns the persisted identifiers and reads the page size
from existing checkpoints.

The storage adapter takes a native shared or exclusive lock on a persistent
sidecar before it opens the graph. The lease covers the checkpoint, WAL, and
memory map for the lifetime of the store. The operating system releases the
lock when the process exits. The usage journal has a separate lock for short
appends from concurrent readers. Code acquires the graph lock before the
journal lock.

Public results are independent of mutable store state. Internal read-only stages
may borrow records and copy the bounded result. Full graph reads belong to
exports, reports and explicit administration. See [Storage](STORAGE.md) for
the file format, locking, transactions and adapter contract.

## Compile Flow

The configuration loader merges and validates the packaged defaults from
`core.yaml` and `user.yaml`. Project files, environment variables and caller
overrides can change settings from `user.yaml` and the project setup and
analysis options declared in `core.yaml`.
Configuration produces one immutable
`CompilationOptions` model. The scanner
uses it to select paths and owns filesystem hashes and artifact identity.

`IncrementalCompilationService` owns stage ordering and transaction boundaries.
`ProjectRegistry` registers project structure, `ArtifactCompiler` parses and
persists artifact facts, and cache and revision repositories track successful
compilation provenance. See the [compilation pipeline](PIPELINE.md) for stage
ordering, parsing, relationships, cache recovery and deletions.
The artifact fingerprint combines the release version, options and parser
dependencies. The cache uses that fingerprint to verify completed graph writes.
The compilation service resolves scan and cache defaults for both the Python API
and project reports. The report generator consumes the same scan and diagnostics
without applying its own file policy. The structural linker resolves named
imports for indexing and for the cleanup pass. Cleanup reconciles locally
eligible findings with current graph usage after structural and test links
have refreshed.

The artifact scope classifier matches configured directory expressions and
built-in test filenames. Compilation persists test scope and case markers for
the static linker and retrieval. Repository explanation, pipeline and HTML
export share an entrypoint classifier that reads the same scope.

Compilation summaries and test lookups share built-in filename matching.
Test lookups use the configured evidence order within each freshness tier.
The coverage adapter resolves report identities to indexed cases before storing
observations. Agents run project checks and record the results in shared work.

## Retrieval

```text
QueryContextRequest -> QueryContextService -> RetrievalEngine
  -> lexical seeds -> expansion within request depth -> ranking
  -> code / general / cleanup projection -> Markdown or structured output
```

`memory.services.query_context.QueryContextService` is the common Python, CLI,
and MCP boundary. It owns request conversion, confidence, revision and freshness
metadata, engineering-context attachment and `ContextResult` serialization.
Providers submit typed requests. The service resolves the assignment project
before retrieval. Assigned text briefs prefer exact symbol ranges
in declared files. The application service budgets embedded source across the
brief and retains verified locations for larger bodies. Follow-up briefs use
the same source selection and prioritize an explicitly requested file. The
follow-up request can carry previously displayed ranges and file hashes.

Within `memory.services.retrieval`:

- `search.py` selects and scores lexical candidates. Exact paths remain
  available before ranking limits apply. Exact symbol lookup uses indexed names
  and Unicode canonical equivalence.
- `expansion.py` examines eligible neighbors using request depth, scope and
  path coverage scores.
- `context/service.py` coordinates projection. `context/projections/` selects
  code, general and cleanup evidence using the indexed scopes and relationships.
- `context/source.py` confines file capture to the registered project. It
  checks current hashes against the committed manifest and reuses request-local
  snapshots across verified excerpts and indexed Markdown sections. It can
  verify indexed ranges by hash without decoding the file for an excerpt.
- `context/models.py` defines projection contracts.
  `context/renderers/` formats the evidence.

The facade passes effective lexical and retrieval policies to the engine.
Ranking prioritizes exact identifiers and resolved relationships across the full
candidate set. The code working set uses normalized query
terms and exact name matches. Projections use explicit query modes and indexed
document and test scope. Field traces use declarations, references and graph
relationships. Setup uses project-configured and verified Markdown headings.
Structural duplicates compare parsed indexed markup within the same project. Source-backed context includes owner, test
and implementation links. See [retrieval commands](CLI.md#retrieval-commands)
for budgets, freshness, read plans and output fields.

`memory.query` executes graph statements against the same store. It reads
complete candidate scopes and applies explicit row limits after ranking or
ordering. `PATH` uses indexed adjacency in both directions and ranks simple
paths by edge strength and node salience.

## Engineering Work and Agent Sessions

| State | Owner and default location |
| --- | --- |
| Repository facts | Canonical project graph, `.reql/memory.reql` |
| Shared engineering work | `CoordinationStore`, `.reql/agent-dashboard.reql` |
| Private session scratch | `AgentWorkspace`, `.reql/agents/` |
| Imported test observations | `TestEvidenceStore`, `.reql/test-evidence.reql` |

The workspace policy resolves the shared and private store paths for CLI, API,
and MCP. `CoordinationStore` owns full traversal of the matched causal scope,
shared work records, and revision history. Callers can request a result limit.

Workspace and coordination reads wait for shared native locks. Writes wait for
exclusive locks before loading the store and rechecking record revisions.
An explicit status wait reads one shared record at a time and releases its
lease between reads. The caller sets the wait deadline.
Lifecycle operations acquire the project lifecycle lock first. Initialization
then takes the private initialization lock. These operations open one graph at
a time. Retention holds the dashboard graph lock and only tries the private
writer lock once. It defers cleanup of any busy store.

Work records link to source using evidence paths and keep their own revisions.
`CoordinationStore` owns validation, atomic revision-checked writes and
read projections for readiness and task overlap. Task claims store their
agent and session independently of the latest write provenance.
`AgentWorkspace` owns private identity and lifecycle. The shared store
commits a claim before optional source recovery. A single shared finish
transaction checks the observed revision, completes the selected task when
applicable, releases other unfinished claims, and saves a checkpoint.
Private session close follows that commit. A retry uses the saved checkpoint
to finish closing without repeating shared writes.

The CLI waits for the graph lease after the claim commits. Prerequisite waits
probe the checkpoint and WAL under short shared leases. They read the record
again when either file changes.

The verification service runs an explicit plan from the project root. It
keys passing receipts by the compiled revision, command, execution environment
and live input hashes. A native lease for each receipt coordinates identical
checks. The service releases graph leases before running commands.

The standalone edit command reads the project's current revision from `RevisionRepository`.
The source editor takes a native workspace edit lock after releasing the graph
read lock. It validates file hashes and applies edits under the edit lock. The
command releases that lock before it opens the graph for refresh.

The failure service ranks causal, file, verified-symbol and lexical matches.
Source retrieval attaches matching work and can reference an already included
failure record. See [Engineering coordination](COORDINATION.md)
for handoffs, verification and retention.

`src/agents/gen-skill.py` generates installed skills and platform rules.
`src/agents/clients.py` owns client profiles for detection and managed paths.
`src/agents/install.py` uses those paths for installation and hook management.
The MCP transport resolves listener settings and tool limits at startup and
passes one limit policy to both tool descriptors and execution.

## Analysis and Projections

Activation and salience rank graph records using structural and usage signals.
The loader validates scoring and analysis policy from `core.yaml`. The facade
passes the effective policy to engines, analysis services and projections.
Community label propagation runs until convergence. Hub and bridge analysis
rank the complete eligible scope before applying the requested result count.
[Community and hub analysis](REQL.md#graph-analysis) can persist results through
the storage port. Project reports compute both in memory and pass the current
community assignments directly to hub scoring. The hub analyzer supplies the
generic hub warnings for the Markdown report.

`RepositoryExplanationService` and `ProjectPipelineService` build read-only
views of the compiled graph. The former follows complete corroborated workflows
and groups ownership. The latter groups reachable code into components and
marks outcomes and feedback cycles. Their typed results retain source
locations through `MemoryGraph.explain_project` and `MemoryGraph.project_pipeline`.
The workflow module supplies indexed entrypoint evidence, project-configured
entry conventions and directed root selection to the pipeline and graph HTML
export. The reporting layer owns display budgets
and embeds its bundled browser assets at export time.
See [repository explanation](CLI.md#repository-explanation) and
[pipeline export](CLI.md#project-pipeline-export).

## Extension Points

Use the [storage adapter contract](STORAGE.md#storage-adapters) for a new
backend. The compilation pipeline documents how to
[change a code extractor](PIPELINE.md#changing-an-extractor) or
[add a document parser](PIPELINE.md#adding-a-document-parser).

### Query seed extractors

Implement `SemanticExtractor` from `memory.storage.extractor`.

The extractor should return `ExtractionResult` objects and is injected through
`MemoryGraph(store, extractor=extractor)` or `MemoryGraph.open(path, extractor=extractor)`.
It supplies query seed discovery. Core compile paths use
deterministic local processing through the
[document processor](PIPELINE.md#graph-output).

### New engines

New engines should be placed under `memory/engines/` when they operate on
low-level graph state in the same style as the current activation and salience
engines. Keep project orchestration under `memory/services/`, and keep graph
analysis algorithms under `memory/analysis/`.

Existing examples:

- `memory.engines.activation.ActivationEngine`
- `memory.engines.salience.SalienceEngine`
- `memory.analysis.communities.CommunityDetector`
- `memory.analysis.hubs.HubAnalyzer`

### New node or edge types

Add constants in `memory/domain/constants.py`, then update compiler, retrieval,
reporting, services, engines, or analysis code only where the new type needs
dedicated behavior. The storage layer accepts arbitrary `type` strings, so
schema evolution is mainly a domain and service concern.
Add integration tests when a type changes retrieval, salience, or analysis.
