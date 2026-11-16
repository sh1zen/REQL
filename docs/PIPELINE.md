# Compilation Pipeline

`project compile` and `MemoryGraph.update_project` use the same
`IncrementalCompilationService`. The scanner reads files without changing them,
hashes their full contents, and verifies size and nanosecond modification time
before and after each read. Unstable files are retried, then reported as scan
errors. Hashing uses a direct path for zero/one file, two workers for small scans
and at most eight workers for larger scans, bounded by available CPUs.

The compiler registers changed structure and reconciles deletions rather than
rewriting unchanged project/file metadata. Revision lookups order scalar metadata
before loading manifests. Effective policy is one immutable
`CompilationOptions` model; see [Configuration](CONFIGURATION.md#loader).
The scanner and parser contracts govern refresh of cached classifications and
fragments. See [code analysis](#code-analysis) and [document ingestion](#document-ingestion)
for their outputs and parser requirements.

- [Flow and cache](#flow)
- [Graph records, deletions, and failures](#graph-records)
- [Code analysis and Tree-sitter](#code-analysis)
- [Document ingestion and parser extensions](#document-ingestion)

## Flow

```text
project compile
  -> open and validate graph storage
  -> scan and hash project files
  -> plan changed, deleted and recoverable artifacts
  -> graph transaction:
       register changed structure and compile artifacts
       relink document fragments
       archive deleted artifacts and invalidate their cache entries
       reconcile project-wide structural and test links against active artifacts
       persist CompilationRun and GraphDelta
       record ProjectRevision when the run succeeds and its manifest changes
  -> persist new successful disk-cache entries
  -> prune eligible history after a new successful revision
  -> checkpoint storage when needed
```

Each artifact compiles inside a nested graph transaction. An artifact exception
rolls back its writes while other artifacts can still succeed. Project-wide
structural/unused/orphan analysis runs only at the stages whose error guards
allow it; a failed run does not publish a committed project revision.

Document-only changes relink their dirty fragments. A code change relinks
document fragments against the updated project symbol set. CSS surface findings
refresh once after changed/deleted artifacts have been processed.

New successful cache entries are written as one disk batch **after** the graph
transaction commits. Deletion handling invalidates existing entries during
archival. The graph and JSON cache are separate persistence boundaries; completed
artifact provenance permits recovery when a cache entry is missing. Mismatched
existing entries require recompilation. A checkpoint writes to a staged file and
clears the WAL after the new generation replaces the old one.
Compilation, including the refresh during task completion, evaluates checkpointing
after applying changes. Opening the graph for compilation can replay the
existing WAL without rewriting the checkpoint first.
See [Storage transactions](STORAGE.md#transactions).

Every run that reaches transaction persistence records a `CompilationRun` and
`GraphDelta`, including clean and artifact-failure runs. A scan failure aborts
before artifact registration and run persistence. CLI counters report graph
upserts, while the changed-symbol summary excludes equivalent symbols and
line-only movement; see [Project compilation](CLI.md#project-compilation).

`reql edit` checks target files against the latest compiled project revision
before writing. It refreshes the graph after edits and reports compilation
errors alongside the applied paths. Assigned `reql agent finish` refreshes
after saved checks pass with stable inputs. For tasks with a saved source check,
edit files directly so finish can check them before refreshing.
Both commands use the same incremental compiler, configured scan policy, and
graph writer lock as `project compile`.

Agent Workspace checks claim ownership before verification. After checks
and refresh, it checks the active session and the coordination transaction
checks the task revision and prerequisites. Completion, return of unfinished
claims to the queue, and checkpoint publication commit together before closing
the private session. A verification or refresh failure leaves the session and
claim active. A claim is also committed before optional source retrieval.
If retrieval fails after that commit, the CLI reports the claim, available
shared context, and a source retry. Claim identity and revision are checked
separately from prerequisite readiness. A claim waiting for prerequisites can
still retrieve source evidence.

### Cache and fingerprints

The cache and compiler share an artifact fingerprint policy. An explicit parser
or chunking version applies project-wide, including when compilers use different
options on the same service. Cache entries compare:

- artifact SHA-256, size and modification time;
- parser and chunking versions;
- effective compilation-options hash and active status.

Both cache hits and recovery also require a matching successfully compiled
digest and compiler-contract fingerprint on the active `SourceArtifact`.
Scan-time metadata alone cannot validate compiled spans or failed work.
For code, the fingerprint includes the grammar selected by the parser catalog,
the Tree-sitter runtime and grammar package versions, and the extractor's
inherited revisions.
Built-in document parsers use a separate contract without grammar dependencies.
Custom compilers and parsers use a conservative contract that tracks the full
grammar environment. Entries with an earlier fingerprint are rebuilt once at
the next successful compilation. The graph and disk-cache formats stay the same.

The primary cache is `<project>/.reql/artifact-cache.json`. Planning builds one
active project map from disk and graph cache records. Missing entries can be
recovered from active compiled artifacts with matching identity, path, content
and compiler provenance. Clearing the cache therefore does not necessarily
recompile unchanged artifacts or reset the graph.

`cache_enabled=False` disables reuse and new successful-entry writes. It still
reconciles deletions and invalidates their existing cache entries. Changing
options without cache writes cannot make an earlier cache entry valid for a
different graph snapshot.

Context-scope metadata is written by artifact/code/document producers. Clean
artifacts are not revisited to repair missing fields; recompile an incomplete
artifact so its producer writes the canonical representation.

## Graph Records

| Record | Meaning |
| --- | --- |
| `SourceArtifact` | Registered path/content metadata and successful compilation provenance |
| `ArtifactCacheEntry` | Successful fingerprint mirrored for queries, reports and recovery |
| `CompilationRun` | Invocation status, artifact counts, graph-write counts and errors |
| `GraphDelta` | Added, updated and archived graph ids, plus affected nodes/communities |
| `ProjectRevision` | Content-addressed manifest, parent revision and changed file paths |

Successful code compiles reconcile AST-backed `TESTS` edges after cross-file
resolution and deletion handling. Test execution observations remain in a
separate store. Reads compare captured hashes against the test and target files
and their compiled provenance. An unrelated revision leaves matching hashes
valid.

Change collections use sets internally and ordered lists at persistence/public
boundaries. Edges use the persisted ids returned by upserts, including when a
canonical key resolves to an existing node. Each artifact phase returns an
`ArtifactCompilationResult` for the orchestration service to merge. See the
[graph schema](REQL.md#graph-schema) for record fields and
[extracted graph](#extracted-graph) for code-specific behavior.

A successful changed manifest is a REQL commit. Clean and failed runs do not
advance the commit-retention window. Project history, retained run/delta records,
archived graph records and usage events follow the
[configured retention policy](CONFIGURATION.md#behavior); physical cleanup is
described in [Storage maintenance](STORAGE.md#inspection-compaction-and-clean-rebuilds).

## Deletions

A later scan archives missing `SourceArtifact` and `File` nodes with their
related fragments/code records. Deleted cache entries are invalidated in disk
and graph metadata so later compiles remain idempotent. This also applies with
cache reuse disabled and when an artifact's first compilation failed.
An incomplete scan cannot establish deletions.

## Failure Behavior

Unreadable directories/files or repeatedly changing scan content raise
`StorageError` with affected paths before compilation writes. Existing committed
graph facts and cache entries remain available; resolve the error and retry.

An artifact exception preserves that artifact's prior graph facts and skips its
cache upsert. Returned parser errors also mark the run failed. Other successful
artifacts and run/delta records can commit; a failed run is not a whole-project
rollback and does not create a project revision. Source verification still
requires committed revision provenance.

If new disk-cache persistence fails after graph commit, the committed graph is
not rolled back. After repairing cache storage, subsequent compilations validate
entries against successful graph provenance, using graph cache records or
recompiling where needed. Nested rollback and WAL behavior belong to the
[storage transaction contract](STORAGE.md#transactions).

## Current Limitations

Compilation leaves `affected_community_ids` empty. Run
[graph analysis](REQL.md#graph-analysis) to refresh persisted community results.

## Code Analysis

Code analysis is the `compile` mode for projects. It builds a deterministic
technical graph from compile-time scanning and AST/static analysis.

Projects may contain multiple programming languages in the same compile run.
Language detection, Tree-sitter grammar selection, and extractor dispatch happen
per `SourceArtifact`, so a Python file, TypeScript file, Solidity contract, and
JavaScript file in the same project each produce their own language-specific
module, symbols, imports, and calls.

### Language Support

REQL recognizes code artifacts for common programming languages including
Python, TypeScript/JavaScript, Solidity, Go, Rust, Java, C/C++, Ruby, C#, Kotlin, Scala,
PHP, Swift, Lua, Zig, PowerShell, Elixir, Julia, Verilog, Fortran, Bash, SQL,
Terraform, Apex, Pascal, Razor, and related file extensions. Recognition means
the scanner registers the file as a `code` artifact with a normalized language
instead of treating it as plain text or unknown content.

### Parsers

Recognized code languages use Tree-sitter AST grammars exclusively. Python,
JavaScript, TypeScript, and Solidity use language-specific adapters, extracting imports,
class/function/method/meaningful-variable symbols, comments, docstrings for
Python, decorators/modifiers, type hints where available, and call targets from the AST,
including the nearest function or method owner for each call. Solidity extraction
emits contract, interface, library, struct, enum, event, constructor, modifier,
receive/fallback, and function symbols; resolves relative imports and Foundry
remappings from `remappings.txt` and `foundry.toml`; links resolvable imports to
real `File` nodes; records `EMITS` for events, `USES` for `using` directives,
`INSTANTIATES` for `new` expressions, and filters Solidity builtins such as
`require`, `assert`, `revert`, `msg`, `abi`, `block`, and `tx`.
Other recognized languages use the reusable Tree-sitter profile extractor with
a profile declared on each language class. The generic engine owns only the
traversal and result assembly; language-specific AST node names, import forms,
call forms, builtins, variables, and macro-style declarations live in the
corresponding `memory.code_analysis.languages.<language>` module.
These profiles emit modules, classes/types, functions, methods, local variables
where the grammar exposes clear writes, imports/includes and idiomatic import
calls such as `require`, `source`, `Import-Module`, and `@import`, comments,
source fragments, call targets, and read/write/return/raise references. This
keeps each non-Python/Solidity language independently extensible while sharing
the common Tree-sitter graph assembly code.

Code parsers return a `CodeParseResult.symbols` collection with derived
`classes`, `functions`, and `methods` views. Compiled symbols carry
[graph source spans](REQL.md#node-records) that
[context retrieval](CLI.md#retrieval-commands) can verify against current source.

The Tree-sitter extraction layer is structured directly under
`memory.code_analysis`. `TreeSitterCodeParser` and shared AST primitives live in
`memory.code_analysis.base`, while
language cataloging, extension detection, aliases, and Tree-sitter grammar
loading live in `memory.code_analysis.catalog`. `TreeSitterExtractorBase` owns shared state,
result assembly and common call/reference recording, but no language walkers or fallback AST
profiles. Language-specific classes live one file per language under
`memory.code_analysis.languages` and are registered in the extractor
factory. Languages without a handwritten walker still have their own class and
declare a language-specific `AstProfile` in that file while inheriting the
reusable `GenericProfileTreeSitterExtractor`. Classes for languages with direct
Tree-sitter wheels declare the corresponding grammar module.

`JavaScriptTreeSitterExtractor` owns the shared ECMAScript walker. TypeScript
inherits those extraction rules, and TSX inherits TypeScript; each class selects
its corresponding grammar through the existing factory registration.

Tree-sitter is a mandatory runtime dependency. REQL does not fall back to the
standard-library Python `ast` parser, regex parsing, or any other code parser.
Syntax errors are reported as compile errors for the artifact instead of being
reparsed by a secondary parser.

The JavaScript syntax walker, shared by TypeScript and TSX, records functions
assigned to static bindings such as `app.handle = function (...) { ... }` and
`exports.settle = async (...) => ...`. The binding name (for example,
`app.handle`) owns the function's arguments, source span and calls. Computed
receivers such as `factory().handle` or `items[index]` do not establish a static
binding. Compiler fingerprints invalidate older cached extraction results.

### Extracted Graph

The [graph schema](REQL.md#graph-schema) lists the stored node types and
relations. The parser symbol table resolves local lexical and receiver
bindings. Structural linking reconciles active artifacts after parsing.

Code artifacts also produce `SourceFragment` nodes for major supported-language
symbols. Code block fragment IDs are based on the artifact and symbol qualified
name rather than line offsets, so moving a function or class does not create a
new fragment for the same symbol. Repeated compiles do not duplicate symbols or
fragments. Unresolved calls do not create standalone call-site nodes;
non-builtin unresolved call targets are stored as `unresolved_calls`
summaries on the owning function or method. Language builtins and calls through
names imported by the artifact, such as `os.getcwd` after `import os`, are
filtered out as low-signal graph noise.
Computed receivers such as `super().execute()`, `factory().execute()` and
`items[0].execute()` do not establish a local target by method name alone. Their
full targets remain in unresolved-call summaries, including builtin receiver
expressions; REQL does not fabricate same-named local call/wrapper links or infer
dynamic dispatch. Existing indexed override/inheritance links remain separate evidence.

Compile also records deterministic local static-analysis findings for dead-code
signals that are useful during repository queries. These findings are stored as
`StaticAnalysisFinding` nodes with `finding_type`, `severity`, `symbol_type`,
`symbol_name`, `qualified_name`, `relative_path`, line properties,
`evidence_scope`, `confidence`, `cleanup_priority`, and numeric
`cleanup_rank`. Findings also include cleanup rationale fields:
`removal_safety` (`safe`, `validate`, or `risky`), `removal_reason`,
`validation_reason`, and `blocking_signals`. Current finding types include `unused_variable`,
`unused_import`,
`possibly_unused_function`, `possibly_unused_method`, and
`possibly_unused_class`. Function, method, and class findings are intentionally
scoped to references detected in the compiled artifact, so they should be read
as local cleanup candidates rather than whole-program proof. Public functions,
classes, and methods are marked as API-risk candidates with lower confidence and
`cleanup_priority=low`, and test artifacts do not emit `possibly_unused_*`
findings for test classes or test methods. Test-local unused imports and
variables use `evidence_scope=test_local_artifact` and low cleanup priority so
product-code cleanup queries are not dominated by test fixtures. Local variable
findings are limited to variables written inside functions or methods, so
dataclass fields, schema attributes, and module-level model fields do not
dominate cleanup queries.
Imports used only by type annotations, decorators, `__future__` features, or
package `__init__.py` re-export surfaces are filtered out. Imports and symbols
used only in Python default arguments, class bases, and exception handlers are
also treated as real usage. Local variables that are read only inside their
owner are summarized instead of persisted as standalone graph nodes; unused
local variables remain persisted so cleanup queries can target them.

See [cleanup retrieval](CLI.md#retrieval-commands) for the safe-remove
projection and [finding queries](REQL.md#code-graph-examples) for inspecting
candidates that require validation.

Application-surface compilation connects CSS selectors with matching template
attributes and JavaScript selector/class APIs. It also emits
`unused_css_class` and high-confidence `always_overridden_css_declaration`
findings. PHP `include`/`require` partials resolve static paths to view files.
Template-render edges expose detected `view_variables`.

Package `__init__.py` imports are marked on `Import` nodes with
`is_re_export=true`. A project-wide post-parse pass resolves them to real public
symbols (including public names from wildcard imports) and links those targets
from the initializer module with `RE_EXPORTS`. Initializer modules and files carry
the searchable roles `package-initializer` and, when applicable, `re-export`.
The same pass resolves ancestors across files, emits `OVERRIDES` for inherited
same-name methods, and resolves conservative Python one-statement delegates as
`WRAPS`. Methods and callables expose searchable `override` and `wrapper` roles,
so lexical and graph queries can seed these structures directly instead of
depending on incidental docstrings or symbol names. Derived links are refreshed
after changed artifacts and stale links are deactivated.

Unrecognized code-like artifacts register project structure (`Project`,
`Directory`, `File`, `SourceArtifact`, and `CONTAINS`) without parser errors.

External or synthetic `CodeSymbol` nodes are only created for meaningful names
such as unresolved decorators, base classes, or type hints. Null-like names,
builtins, names already represented by imports, and builtin generic forms such
as `list[str]` are ignored, and external symbols are excluded from default hub
rankings and top-symbol report sections so they do not drown out real project
functions, classes, and methods.

### C syntax recovery

C headers often contain storage/export macros and conditional `extern "C"`
linkage that Tree-sitter cannot fully resolve without preprocessing. A syntax
error in those constructs no longer erases every symbol in the file. The C
extractor traverses translation-unit, declaration-list, linkage and preprocessor
containers and retains intact declarations. A function with header recovery is
accepted only when its declarator and body are intact and all error nodes occur
before the declarator. Parameter names come from their declarators rather than
from typedef names in the parameter types.

The module keeps `parser_status: partial`, `tree_sitter_has_error` and its warning.
Recovered function metadata includes `syntax_recovered: true`; an ambiguous
return type remains unknown (`returns: null`). Missing/error nodes, broken function declarators and
damaged bodies do not produce functions or calls. No macro values, active
conditional branches or hidden function scopes are guessed, and no C compiler
or preprocessor is executed. Other language extractors keep their previous
whole-file syntax-error policy. The compiler fingerprint invalidates older
cached results once.

### Tree-sitter Dependencies

The package declares Tree-sitter, direct grammar packages for the recognized
languages that publish Python wheels, and `tree-sitter-language-pack` as a
fallback for grammars that do not have a stable standalone wheel. A source
checkout can install them with:

```bash
uv venv
uv pip install -e .
```

Parser construction uses the declared Tree-sitter 0.25 API: `Parser(language)`.
Grammar providers return a capsule or a constructed `Language` object. Invalid
provider values and constructor errors propagate.

Compilation never downloads fallback grammars. Language-pack versions can fetch
missing parsers from GitHub on `get_language`; REQL checks their local inventory
first. A missing local grammar yields a module with `parser_status: skipped` and
an actionable `parser_warning`, preserving file discovery without inventing
symbols. Direct grammar wheels work offline immediately after installation.

For a language without a direct wheel, explicitly prepare its grammar while
online, using Python from the environment containing REQL. For example:

```bash
.venv/bin/python -c "import tree_sitter_language_pack as p; p.download(['dockerfile'])"
```

On Windows use `.venv/Scripts/python.exe`. Keep the language-pack version and
cache available when moving to an offline machine. These explicit downloads are
setup actions; parser construction and normal compilation remain local.
Skipped code-parser results cannot establish a reusable cache entry. The next
compilation retries them, so preparing a grammar takes effect without editing
source or clearing the graph.

### Changing an extractor

- Edit the shared walker when a change applies to JavaScript, TypeScript, and
  TSX. They all use [javascript.py](../src/memory/code_analysis/languages/javascript.py).
- Advance `facts_revision` on the extractor whose persisted facts change
  (start at 1). Revisions accumulate through inheritance, so changing the
  shared JavaScript walker invalidates JavaScript, TypeScript, and TSX.
- Advance `PARSER_FACTS_VERSION` in
  [fingerprint.py](../src/memory/artifacts/fingerprint.py) for project-wide
  changes to persisted compiler facts.
- Test extracted behavior separately through `MemoryGraph.compile_project` as in
  [test_symbol_resolution.py](../tests/test_symbol_resolution.py). Close the
  graph with `contextlib.closing(MemoryGraph.open(...))` or in `finally`.
- To test scoped invalidation in process, hold source, store and compilation
  options fixed. Change the relevant extractor revision and use a new
  `IncrementalCompilationService`. Check which files were rebuilt and confirm
  the next unchanged compile reports zero changes. Explicit `parser_version`
  values override the project-wide contract. See
  [test_incremental_integrity.py](../tests/test_incremental_integrity.py).
  To test migration from the previous extracted facts, seed the index with the
  previous extractor in a separate checkout. The version override changes cache
  identity.

## Document Ingestion

Artifact ingestion turns registered `SourceArtifact` files into parsed
`SourceFragment` nodes with provenance and document-specific relations.

### Supported Inputs

- Markdown: headings, paragraphs, lists, fenced code blocks, inline links, and
  simple pipe tables.
- Plain text and text-like artifacts: paragraph chunks with preserved line
  ranges and character offsets, including unknown UTF-8 text files. Recognized
  code formats use the code parser instead.
- PDF: text and page-based fragments when the [optional parser](#pdf-fallback)
  is available.
- Code: registered Tree-sitter parsers create technical graph facts and source
  fragments. See [language support](#language-support) for details.
- Image and video files are skipped during project compilation and are not parsed,
  registered, or compiled.

Files with a Markdown extension use the Markdown parser. Extensionless files
and `.txt` files with Markdown markup can use it based on a sample of their
contents. Plain text is indexed in paragraph chunks when document ingestion
is enabled. Document fragments participate in `query_context --docs`.
Configured formats and document policy are described in
[Configuration](CONFIGURATION.md#behavior).

### Parser Interface

Concrete parsers live under `memory.document_ingestion.formats` and inherit
`BaseDocumentParser`, the common document parser layer. Format-specific modules
are organized by parser name, for example
`memory.document_ingestion.formats.markdown.MarkdownParser`.

Parsers implement:

```text
supports(artifact) -> bool
parse(artifact, content) -> DocumentParseResult
```

`DocumentParseResult` includes title, parser metadata, fragments, discovered
links, tables, parser errors, parser name, and parser version. Links and tables
remain `DocumentLink` and `DocumentTable` objects through parsing and graph
compilation; they become dictionaries only at serialization boundaries.

`DocumentFragment` records:

- fragment type;
- text;
- line and byte-offset ranges;
- page number when relevant;
- section path;
- content hash;
- confidence;
- parser metadata.

### Graph Output

The compiler creates or updates deterministic `SourceFragment` nodes. Fragments
are deduplicated by artifact and structural hash, so changed text updates an
existing same-position fragment instead of duplicating it.

Text documents in compile mode (`markdown`, `text`, `config`, `data`, and
unknown UTF-8 text) are compiled structurally into fragments and document
relations. Document text remains a lower-level source layer used for
provenance, code linking, and query context around the code graph.

The deterministic processor in `memory.extraction.document_processor` creates ranked `Concept` nodes, `RawEvent`
observations, and term co-occurrence relations. Explicit headings produce stable
concepts that are archived when their headings disappear. The
[document relations](REQL.md#document-relations) connect fragments to their
artifact, sections, links, code blocks, and compiled symbols.

Document-code links require an explicit high-signal mention, such as a qualified
module, function, class, method, route, endpoint, schema, filename, or path.
The linker caps results at eight targets per fragment and skips generic headings
and short terms such as `usage`, `config`, `api`, `project`, and `architecture`.

Parser metadata and parser errors are stored on both the artifact and fragment
properties where relevant. See [failure behavior](#failure-behavior) for
compilation outcomes.

### PDF fallback

With `compile.documents.pdf` enabled, the optional `pypdf` dependency extracts
text. Missing dependencies produce metadata-only fragments and artifact
properties such as `needs_parser` or `partially_readable` where appropriate.

### Adding a document parser

Add document format parsers under `memory/document_ingestion/formats/` and
inherit `BaseDocumentParser` from `memory.document_ingestion.base`. The common
base handles format support checks, UTF-8 decoding with replacement, file
metadata, and path-derived titles. Register new parsers in
`default_parser_registry()` when they should participate in project
compilation. Keep optional format dependencies graceful, following the PDF
parser pattern.
