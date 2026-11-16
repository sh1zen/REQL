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
| `src/api` | Public `MemoryGraph` facade and exported models |
| `src/memory/services` | Compilation, retrieval and coordination |
| `src/memory/artifacts` | Scanning, compilation, cache, revisions and deltas |
| `src/memory/code_analysis` | Registered Tree-sitter parsers and extractors |
| `src/memory/testing` | Test association evidence and transactional observation import |
| `src/memory/document_ingestion` / `extraction` | Document parsing and deterministic term processing |
| `src/memory/analysis` / `engines` | Communities, hubs, activation and salience |
| `src/memory/explanation` / `pipeline` | Read-only repository and flow projections |
| `src/memory/storage` | Storage port and block adapter |
| `src/mcp` | Tool handlers and stdio/HTTP transport |
| `src/agents` | Generated assistant guidance, installation and hooks |

## Storage Boundary

`memory.storage.GraphStore` defines reads, writes, indexed lookups,
neighborhoods, counts and transactions. The bundled `BlockGraphStore` owns
block persistence, transaction journals, lazy loading, checkpoint serialization
and index lifecycle. Its public package exports resolve to the same class.
Stateless token selection lives in `memory.storage.adapters.lexical_index`.

Public results are independent of mutable store state. Internal read-only stages
may borrow records and copy the bounded result. Full graph reads belong to
exports, reports and explicit administration. See [Storage](STORAGE.md) for
the file format, locking, transactions and adapter contract.

## Compile Flow

Configuration produces one immutable `CompilationOptions` model. The scanner
uses it to select paths and owns filesystem hashes and artifact identity.
Verification reuses the walk and hashing code, including for declared inputs
outside scan selection.

`IncrementalCompilationService` owns stage ordering and transaction boundaries.
`ProjectRegistry` registers project structure, `ArtifactCompiler` parses and
persists artifact facts, and cache and revision repositories track successful
compilation provenance. See the [compilation pipeline](PIPELINE.md) for stage
ordering, parsing, relationships, cache recovery and deletions.

## Retrieval

```text
QueryContextRequest -> QueryContextService -> RetrievalEngine
  -> lexical seeds -> bounded expansion -> ranking
  -> code / general / cleanup projection -> Markdown or structured output
```

`memory.services.query_context.QueryContextService` is the common Python, CLI,
and MCP boundary. It owns request conversion, confidence, revision and freshness
metadata, engineering-context attachment and `ContextResult` serialization.
Providers submit typed requests. Assigned text briefs can focus on exact names
in the task's declared files. Indexed ranges remain available when the source
body exceeds the brief budget. Follow-up briefs share that source selection.

Within `memory.services.retrieval`:

- `search.py` selects and scores lexical candidates. Exact indexed paths use
  property lookups. Other searches share identifier and path tokenization.
- `expansion.py` traverses bounded neighborhoods using request scopes and path
  coverage scores.
- `context/service.py` coordinates projection. `context/projections/` selects
  code, general and cleanup evidence using the indexed scopes and relationships.
- `context/source.py` confines file capture to the registered project. It
  checks current hashes against the committed manifest and reuses request-local
  snapshots across previews, verified excerpts and indexed Markdown sections.
- `context/models.py` defines projection contracts.
  `context/renderers/` formats the evidence.

Ranking prioritizes exact identifiers and resolved relationships, then
diversifies symbols across files. Source-backed context includes owner, test
and implementation links. See [retrieval commands](CLI.md#retrieval-commands)
for budgets, freshness, read plans and output fields.

## Engineering Work and Agent Sessions

| State | Owner and default location |
| --- | --- |
| Repository facts | Canonical project graph, `.reql/memory.reql` |
| Shared engineering work | `CoordinationStore`, `.reql/agent-dashboard.reql` |
| Private session scratch | `AgentWorkspace`, `.reql/agents/` |
| Test observations and verification receipts | `TestEvidenceStore`, `.reql/test-evidence.reql` |

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

`TestEvidenceStore` owns saved check identities and receipts. Assigned finish
captures the current check selection, verifies it under the receipt writer
lease, and holds an evidence read lease through task completion. The standalone
edit command reads the project's current revision from `RevisionRepository`.
The source editor validates file hashes and coordinates its writes under the
workspace edit lease.

The failure service ranks causal, file, verified-symbol and lexical matches.
Source retrieval attaches work in a bounded section and can reference an
already included failure record. See [Engineering coordination](COORDINATION.md)
for handoffs, verification and retention.

`src/agents/gen-skill.py` generates installed skills and platform rules.
`src/agents/install.py` owns installation and guidance hooks.

## Analysis and Projections

Activation and salience rank graph records using structural and usage signals.
[Community and hub analysis](REQL.md#graph-analysis) can persist results through
the storage port.

`RepositoryExplanationService` and `ProjectPipelineService` build read-only
views of the compiled graph. The former groups ownership and corroborated
workflows. The latter groups reachable code into architectural components and
marks outcomes and feedback cycles. Their typed results retain source
locations through `MemoryGraph.explain_project` and `MemoryGraph.project_pipeline`.
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
