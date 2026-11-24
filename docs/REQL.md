# REQL Queries and Graph

Use `reql query "STATEMENT"` to inspect property-graph records or run graph
analysis. Add `--json` for structured output.

- [Query syntax and examples](#first-class-graph-commands)
- [Graph schema](#graph-schema)
- [Graph analysis](#graph-analysis)

## First-Class Graph Commands

Project and artifact records:

```text
PROJECTS
ARTIFACTS LIMIT 20
ARTIFACTS WHERE artifact_type = "code" LIMIT 20
FRAGMENTS WHERE fragment_type = "heading" LIMIT 20
```

Code graph records:

```text
SYMBOLS TYPE Function WHERE name CONTAINS "compile" LIMIT 20
SYMBOLS TYPE Class,Method ORDER BY relative_path ASC LIMIT 50
FINDINGS WHERE finding_type = "unused_variable" LIMIT 20
```

For communities and hubs, see [graph analysis](#graph-analysis).

Incremental compilation records:

```text
DELTAS LIMIT 10
CACHE STATUS
```

These commands support `WHERE`, `LIMIT`, and `ORDER BY` where applicable. Fields
resolve against node properties first-class, so artifact fields such as
`artifact_type` and symbol fields such as `name` can be filtered directly.

`FIND`, typed listings, and `MATCH` apply predicates and ordering to the complete
eligible type scope before `LIMIT`. Omit the clause to return all rows.
Type filters and equality comparisons are case-insensitive.
Native fields, custom or nested properties, and source-span aliases share the
same field resolution. Explicit `SYMBOLS TYPE` lists retain every requested type.
`STATS TYPE` uses the same type matching.

`LIMIT`, `TOP`, and `DEPTH` require non-negative integers. `LIMIT 0`
returns no rows. Numeric comparisons and ordering
preserve integer precision. Mixed sort values order nulls before numbers and
numbers before text in ascending order.

`WHERE` supports boolean composition with `AND`, `OR`, and `NOT`, comparison
operators (`=`, `!=`, `>`, `>=`, `<`, `<=`), list membership with `IN`, text
operators (`CONTAINS`, `STARTS WITH`, `ENDS WITH`, `LIKE`, `ILIKE`, `REGEX` or
`MATCHES`), ranges with `BETWEEN ... AND ...`, and null checks with `IS NULL`
or `IS NOT NULL`.

```text
FIND nodes TYPE Function WHERE text ILIKE "%payment%" LIMIT 10
FIND nodes TYPE Function WHERE name REGEX "^compile_.*" RETURN name,relative_path
FIND nodes WHERE salience BETWEEN 0.5 AND 1.0 AND source_url IS NULL LIMIT 20
FIND edges WHERE type IN ["CALLS","REFERENCES"] RETURN from_id,to_id,type LIMIT 50
```

## Code Graph Examples

List functions defined by source artifacts:

```text
MATCH (a:SourceArtifact)-[:DEFINES]->(f:Function)
RETURN a.path,f.name,f.start_line
```

List resolved function calls:

```text
MATCH (f:Function)-[:CALLS]->(g)
RETURN f.name,g.name
```

`MATCH` aliases are case-insensitive and bind only to their declared pattern
roles. Without `RETURN`, column names use those aliases. Anonymous roles receive
distinct aliases. Repeating a node alias requires the same node at both ends;
an edge alias cannot also name a node. Undirected self-loops produce one row.

Find imported modules:

```text
FIND nodes TYPE Import RETURN relative_path,module,name,line LIMIT 50
```

Find classes and methods:

```text
FIND nodes TYPES Class,Method RETURN name,qualified_name,start_line,end_line LIMIT 50
```

Although results can use either set of [source-span fields](#node-records),
including `relative_path` lets callers resolve paths within the project.

```text
MATCH (file:File)-[:DEFINES]->(fn:Function)
RETURN file.relative_path,fn.name
```

Find compile-time static analysis findings:

```text
FINDINGS WHERE finding_type = "unused_import"
RETURN symbol_name,relative_path,line_start,cleanup_priority,reason
```

```text
MATCH (s:Variable)-[:HAS_FINDING]->(f:StaticAnalysisFinding)
RETURN s.name,f.finding_type,f.relative_path,f.line_start
```

`FINDINGS` sorts active findings by numeric `cleanup_rank` by default, so direct
cleanup candidates appear before low-confidence or test-local noise. Explicit
`ORDER BY cleanup_priority` uses the same high/medium/low rank.

Generate a deterministic verification bundle for a specific finding before
opening files:

```text
VERIFY FINDING static-analysis-finding:abc123
```

The bundle returns the finding summary, a minimal graph-backed snippet, all
usage edges found for the associated symbol, checked scopes, risks, and a
recommended action. It uses compiled graph nodes and relations only; it does
not read source files on demand.

## Retrieval Query Examples

For the `query_context`, `query_explore`, and `query_memories` CLI commands,
including scopes, budgets, cleanup mode, and structured envelopes, see
[Retrieval commands](CLI.md#retrieval-commands).

Use raw `reql query "..."` statements when you need deterministic rows instead
of a synthesized context block. `RETRIEVE` is the raw query form for ranked
source/code rows: it runs deterministic retrieval, includes connected source
texts by default, and returns rows that can be consumed by agents or scripts.
Ask for only the columns needed for the next decision, include `LIMIT`, and add
`relative_path`, `line_start`, `line_end`, `source_for`, `relation`, or
`direction` when provenance matters.
Omit `TOP`, `DEPTH`, `LIMIT`, and `MAX TEXT` to use the complete
ranked scope and return the full text of each matching row.

```text
RETRIEVE "payment validation" LIMIT 8
RETRIEVE "compile document fragments" TOP 20 DEPTH 2 LIMIT 10
RETRIEVE "payment workflow" TYPE Function,Method NO SOURCES RETURN id,type,label,text,score
RETRIEVE "source provenance" RETURN id,type,text,score,source_for,relation,direction,relative_path,line_start,line_end
```

## Path Example

Find a bounded path between text-selected concepts:

```text
PATH FROM TEXT "incremental compiler" TO TEXT "activation engine" DEPTH 5
```

`PATH` follows relationships in both directions and returns paths that visit
each node at most once. Parallel relationships produce distinct paths. Add
`DEPTH` to set the maximum number of edges in a path. Add `LIMIT` to return
the highest-scoring paths from the full search. On a dense graph, the number
of simple paths can grow exponentially. Use `VIA` to select relationship types.
The bundled graph query policy defines the scoring weights and default columns.
[Configuration guide](CONFIGURATION.md#extraction-retrieval-and-query-policies)

## Graph Schema

REQL's active schema is a code graph. Nodes describe projects, source
artifacts, source fragments, code symbols, deterministic document terms and
events, static-analysis findings, graph-analysis records, cache entries,
compilation runs, and deltas. Edges describe containment, definitions,
references, calls, imports, inheritance, configuration, tests, provenance,
document co-occurrence, cleanup findings, and graph-analysis membership.

### Node Records

Each `MemoryNode` has:

- `id`
- `type`
- `label`
- `text`
- `canonical_key`
- `properties`
- status
- salience, activation, confidence
- evidence and usage counters
- timestamps

Compiled symbols expose interchangeable `start_line/end_line` and
`line_start/line_end` fields, plus `source_path` and `relative_path`. `SYMBOLS`,
`FIND`, `MATCH`, and `RETRIEVE` accept either span spelling. Free-search
retrieval ranks active records with lexical coverage, graph context, salience,
and usage signals.

### Edge Records

Each `MemoryEdge` has:

- `id`
- `from_id`
- `to_id`
- `type`
- weight
- confidence
- polarity
- origin
- `properties`
- timestamps

Edges are directed. `from_id` is the outgoing/source endpoint and `to_id` is
the incoming/target endpoint. Query and context payloads may expose aliases such
as `source_id`, `target_id`, `incoming`, and `outgoing`, but the stored
canonical edge record remains `from_id -> to_id`.

Deterministic compile-time technical edges carry `confidence=1.0` and store
`source_id`, `target_id`, `type`, `confidence`, `source_file`, `line_start`,
`line_end`, `extractor`, `evidence`, `created_at`, `updated_at`,
`mode=compile`, `is_semantic=false`, and `is_technical=true` in their
properties.

### Main Node Types

- `Project`: scanned project root.
- `Directory`: deterministic project directory.
- `File`: deterministic project file.
- `SourceArtifact`: registered source, document, data, config, or binary file.
- `SourceFragment`: deterministic fragment derived from a source artifact.
- `Module`, `Package`, `Class`, `Interface`, `Function`, `Method`,
  `Variable`, `CodeSymbol`: compile-mode code symbols and containers.
- `Import`, `Dependency`, `Endpoint`, `Schema`, `Config`, `Test`, `Comment`,
  `Docstring`: compile-mode technical graph records.
- `StaticAnalysisFinding`: deterministic static-analysis or cleanup candidate.
- `Concept`: deterministic document term or explicit heading concept.
- `RawEvent`: underlying document observation used as evidence for terms.
- `ArtifactCacheEntry`: successful artifact compilation fingerprint.
- `CompilationRun`: one incremental compilation invocation.
- `GraphDelta`: change trace for a compilation run.
- `ProjectRevision`: immutable content-addressed project tree metadata with its
  parent id, manifest hashes, and changed paths.
- `Community`: topological cluster written by deterministic community
  detection.

Retrieval usage is stored in the sidecar `<store>.usage.jsonl` journal rather
than as graph nodes.

### Main Relations

```text
Project/Directory -CONTAINS-> Directory/File
File/SourceArtifact -DEFINES-> Module/Class/Interface/Function/Method/Variable
Module -CONTAINS-> Function/Class/Interface/Method/Variable
Class/Interface -METHOD-> Method
SourceFragment -REFERENCES-> code symbol
Function/Method -CALLS-> Function/Method
File/Module -IMPORTS-> Import
File -DEPENDS_ON-> Dependency
Module -IMPORTS_FROM-> Dependency
Module -RE_EXPORTS-> Symbol/Module/File
Class/Interface -INHERITS-> Class/CodeSymbol
Method -OVERRIDES-> Method
Function/Method -WRAPS-> Function/Method/Class
ProjectRevision -PARENT_REVISION-> ProjectRevision
ProjectRevision -DERIVED_FROM-> CompilationRun
ProjectRevision -CHANGES-> SourceArtifact
Class -IMPLEMENTS-> Interface
Function/Method -READS/WRITES/RETURNS/RAISES-> code node
Function/Method -INSTANTIATES-> Class/Interface
Function/Method -EMITS-> event symbol
Class/Interface/Module -USES-> code symbol
Module/Function/Class/Interface/Method -EVIDENCED_BY-> SourceFragment
Function/Class/Method -HAS_DOCSTRING-> Docstring
Module/Function/Class/Method -HAS_COMMENT-> Comment
Function/Class/Method -DECORATED_BY-> CodeSymbol
Function/Method -HANDLES_ROUTE-> Endpoint
Test -TESTS-> code symbol
SourceArtifact/Symbol/Import -HAS_FINDING-> StaticAnalysisFinding
graph member -BELONGS_TO_COMMUNITY-> Community
bridging node -BRIDGES_COMMUNITY-> neighboring Community
```

The schema also defines `CONFIGURES` for configuration relations.

### Document Relations

```text
SourceArtifact -CONTAINS_FRAGMENT/HAS_SECTION-> SourceFragment
SourceFragment -LINKS_TO-> URI
SourceFragment -HAS_CODE_BLOCK-> SourceFragment
SourceFragment -DERIVED_FROM-> SourceArtifact
SourceFragment -REFERENCES-> compiled code symbol
SourceFragment -MENTIONS-> Concept
SourceArtifact/SourceFragment -CONTAINS-> heading Concept
Concept -EVIDENCED_BY-> RawEvent
Concept -CO_OCCURS_WITH-> Concept
```

See the [compilation pipeline](PIPELINE.md#document-ingestion) for how REQL
builds document fragments and [code analysis](PIPELINE.md#extracted-graph) for
static findings and unresolved calls.

Shared `work_record` nodes belong to a separate store. Their fields, relations
and lifecycle are defined in [Engineering coordination](COORDINATION.md#work-records).

## Graph Analysis

REQL includes deterministic graph analysis for communities, hubs, and bridge
signals. The implementation is dependency-light and does not require NetworkX,
Leiden, or an LLM.

### Communities

Community detection propagates labels over eligible nodes until they settle.
Each community takes its name from a representative member. The detector
writes `Community` nodes with the following fields.

- `id`
- `project_id`
- `label`
- `size`
- `density`
- `salience`
- `status`
- `created_at`
- `updated_at`

Members are connected with `BELONGS_TO_COMMUNITY`. Nodes that touch neighboring
communities receive `BRIDGES_COMMUNITY` edges. Python callers can set
`max_iterations` in the detection options for a partial result.

### Hub Analysis

Hub analysis detects high-degree graph nodes, but REQL avoids
promoting generic high-degree nodes by requiring both structural centrality and
semantic specificity.

The score is:

```text
hub_score =
    0.20 * normalized_degree
  + 0.15 * weighted_degree
  + 0.15 * activation_frequency
  + 0.15 * retrieval_usefulness
  + 0.10 * salience
  + 0.10 * community_bridge_score
  + 0.10 * specificity
  + 0.05 * confidence
  - 0.20 * generic_penalty
```

If activation or retrieval usefulness is not present on a node, the analyzer
uses zero. It does not synthesize usage data.

The specificity scorer is deterministic. It rewards concise topical labels,
specific node types, focused neighborhoods, and known successful usage. It
penalizes stopword-heavy or generic vocabulary, generic node types, high degree
without topical focus, unknown/binary artifacts, and high entropy across
neighbor types.

Hub analysis updates node properties:

- `hub_score`
- `centrality_score`
- `specificity_score`
- `community_bridge_score`
- `is_hub`
- `hub_rank`
- `hub_reason`

`HUBS` scores all eligible nodes and uses their incident relationships and
stored `BELONGS_TO_COMMUNITY` edges. Run `COMMUNITIES` to refresh community
assignments before hub analysis. `LIMIT` controls the rows returned after
filtering and ordering. `ACTIVATE` processes eligible neighbors and updates
activation and usage in the graph. Bridge detection uses the strongest evidence
from each neighboring community and all eligible cross-community connections.

### Commands

```bash
reql query "COMMUNITIES LIMIT 20"
reql query "HUBS LIMIT 20"
reql query "HUBS TYPE Topic,Concept,Function LIMIT 10"
reql query "EXPLAIN HUB 'node_id'" --json
```

Use `--json` on `reql query` for structured output.

### Limitations

The community detector is designed for local graphs with thousands of nodes. It
is deterministic and fast, but it is not a full modularity optimizer. Hub and
bridge analysis are based on current graph structure and stored usage signals;
absent usage signals remain absent.
