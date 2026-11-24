# Compilation Pipeline

`project compile` and `MemoryGraph.update_project` use the same
`IncrementalCompilationService`. The scanner reads files without changing them,
hashes their full contents with SHA-256, and verifies size and nanosecond
modification time before and after each read. Unstable files are retried, then
reported as scan errors. The scan has unlimited file size by default and
accepts an explicit positive ceiling through `scan.max_file_size_mb`.
Hashing uses a direct path for zero/one file, two
workers for small scans and at most eight for larger scans, bounded by available CPUs.

The compiler registers changed structure and reconciles deletions rather than
rewriting unchanged project/file metadata. Revision lookups order scalar metadata
before loading manifests. The loader only applies project overrides to options
declared in `user.yaml` before the compiler builds its immutable `CompilationOptions`.
See [Configuration](CONFIGURATION.md#loader).
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
  -> acquire exclusive graph access and validate storage
  -> scan and hash project files
  -> plan changed, deleted and recoverable artifacts
  -> graph transaction:
       register changed structure and archive deleted graph records
       compile changed artifacts and invalidate deleted cache entries
       relink document fragments against active symbols
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
CLI compilation and edit refresh can wait for the graph lease with
`--lock-timeout SECONDS`. The compiler starts after the lease is acquired.
The `query_context` CLI command can use the same option for its shared read lease.
Project reports scan under the same policy and reject incomplete scans before
writing reports. Community detection and hub ranking cover the full project
scope in memory. Hub scoring uses all detected memberships.
Report collection reads all scoped code and state records. Call edge counts
cover the selected project and shared edges. Markdown applies configured
list limits after sorting and reports omitted items. It includes every parser
error for each listed artifact and uses the warnings from hub analysis.

Document-only changes relink their dirty fragments and reconcile only those
artifacts' links. Code changes and deletions relink document fragments against
the updated project symbol set. CSS surface findings reconcile within their
project after changed and deleted artifacts have been processed. Archival,
artifact/cache enumeration, and structural and static test reconciliation read
their complete indexed scopes.

New successful cache entries are written as one disk batch **after** the graph
transaction commits. Deletion handling invalidates existing entries during
archival. The graph and JSON cache are separate persistence boundaries; completed
artifact provenance permits recovery when a cache entry is missing. Mismatched
existing entries require recompilation. Under the exclusive graph lock, a
checkpoint writes to a staged file and closes the active memory map before
publishing the new generation. The adapter clears the WAL after publication.
If replacement fails, the previous checkpoint and its WAL remain available.
Compilation evaluates checkpointing after applying changes. Opening the graph
for compilation can replay the existing WAL without rewriting the checkpoint first.
See [Storage transactions](STORAGE.md#transactions).

Every run that reaches transaction persistence records a `CompilationRun` and
`GraphDelta`, including clean and artifact-failure runs. A scan failure aborts
before artifact registration and run persistence. CLI counters report graph
upserts, while the changed-symbol summary excludes equivalent symbols and
line-only movement; see [Project compilation](CLI.md#project-compilation).

`reql edit` checks target files against the latest compiled project revision
before writing. Plans and source files have no built-in size or edit-count
ceiling. The source editor waits for the native edit lock, then validates file
hashes and applies the plan. The command refreshes the graph after edits and
reports compilation errors alongside the applied paths. It uses the same incremental
compiler, configured scan policy, and graph writer lock as `project compile`.

Agents pass focused checks before running broader checks. They refresh the
graph after direct edits and supply results at handoff.
Agent Workspace checks the active session, and the
coordination transaction checks the claim, task revision, and prerequisites.
Workspace store opens wait for native shared or exclusive locks.
Completion, return of unfinished claims to the queue, and checkpoint publication
commit together before closing the private session. The checkpoint links all
session work and stores pending next actions. A claim is also committed
before optional source retrieval.
The CLI waits for the shared graph lease after that commit.
If retrieval fails, the CLI reports the claim, available
shared context, and a source retry. Claim identity and revision are checked
separately from prerequisite readiness. A claim waiting for prerequisites can
still retrieve source evidence. Work recovery follows the full matched causal
scope before applying any result limit supplied by the caller.
JSON claim results include source locations. Add `--source` to embed verified
bodies. Text follow-ups can use the displayed ranges and hashes from an
earlier brief to avoid repeating source.

For a task query, resolve the project from the indexed paths in the assignment
before ranking. Use the same project for follow-up queries. Omit project source
if several projects are indexed and the assignment does not identify one. A
test-only request uses indexed test source for both ranked results and
assigned-file recovery.

Verification plans run focused checks before broad checks. A failed check ends
the plan. For a check with declared inputs, REQL verifies the revision and
input hashes before reusing a receipt or saving a new pass.

### Workflow projections

Explanation uses the configured hop limit for graph relationships and
corroborating imports. Pipeline and HTML graph exports use the same directed
root selector, including cycles, when a project has no qualifying explicit
trigger. Pipeline also resolves imports supported by available source evidence.
See [Analysis and report policies](CONFIGURATION.md#analysis-and-report-policies).

### Cache and fingerprints

The cache and compiler share an artifact fingerprint policy based on the REQL
release version from `src/config.py`. Cache entries compare the following values.

- artifact SHA-256, size and modification time;
- effective compilation-options hash and active status.

Both cache hits and recovery also require a matching successfully compiled
digest and compiler-contract fingerprint on the active `SourceArtifact`.
The fingerprint includes the REQL release version and the compiler contract version. A changed fingerprint triggers
recompilation and replaces the cache snapshot on disk and in the graph.
Code fingerprints also include the selected grammar, extractor and relevant
Tree-sitter dependency versions. Built-in document fingerprints depend on the
release and compile options, including the core document term, relation and
event policy. Custom compilers and parsers track the full
grammar environment. Graph storage and cache serialization use their own
format identifiers for compatibility.

The primary cache is `<project>/.reql/artifact-cache.json`. Planning builds one
active project map from disk and graph cache records. Missing entries can be
recovered from active compiled artifacts with matching identity, path, content
and compiler provenance. Clearing the cache therefore does not necessarily
recompile unchanged artifacts or reset the graph.

`cache_enabled=False` disables reuse and new successful-entry writes. It still
reconciles deletions and invalidates their existing cache entries. Changing
options without cache writes cannot make an earlier cache entry valid for a
different graph snapshot.

The block adapter builds lexical postings from the complete node text and
searchable metadata. On a policy mismatch, it rebuilds from canonical nodes
and the current WAL overlay. Compilation checkpoints rebuilt postings with
the effective policy key.

Context-scope metadata is written by artifact/code/document producers. Clean
artifacts are not revisited to repair missing fields; recompile an incomplete
artifact so its producer writes the canonical representation. Changes to
`testing.discovery.directories` invalidate cached compilation. Recompilation
updates test scope and test cases, archives removed classifications and
reconciles static `TESTS` edges. The summary gathers direct relationships and matches test
filenames across the project, then adds up to eight lexical matches.

## Graph Records

| Record | Meaning |
| --- | --- |
| `SourceArtifact` | Registered path/content metadata and successful compilation provenance |
| `ArtifactCacheEntry` | Successful fingerprint mirrored for queries, reports and recovery |
| `CompilationRun` | Invocation status, artifact counts, graph-write counts and errors |
| `GraphDelta` | Added, updated and archived graph ids, plus affected nodes/communities |
| `ProjectRevision` | Content-addressed manifest, parent revision and changed file paths |

Successful code compiles reconcile AST-backed `TESTS` edges after cross-file
resolution and deletion handling. Test scope uses configured directory expressions
and built-in filename conventions. The compiler, summary, retrieval, explanation,
pipeline and graph export share the classification. Compilation summaries
limit associated tests to the project that produced the revision. For ECMAScript, literal test
callbacks include `test` and `it`, named imports from supported test runners,
and the `only`, `skip` and `concurrent` modifiers. Test execution observations remain in a
separate store. Reads compare captured hashes against the test and target files
and their compiled provenance. An unrelated revision leaves matching hashes
valid.
For brief queries that name exact test files, retrieval scopes lexical
test candidates to those files before it adds association evidence.
An association supplies the projected test name and source range.
Assigned briefs share one section budget across verified Markdown outlines.

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
remappings from `remappings.txt` and `foundry.toml`; changes to these files
refresh Solidity artifacts. Compilation links resolvable imports to
real `File` nodes and records `EMITS` for events, `USES` for `using` directives,
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
its corresponding grammar through the existing factory registration. Relative
extensionless imports resolve local files or directory indexes across the
supported ECMAScript extensions, including .mts and .cts.

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
fragments. The owning function or method stores unresolved calls in
`unresolved_calls`. The compiler checks the artifact's imported bindings and,
for Python, the runtime's built-in names before recording an unresolved call.
The Python runtime version is part of the compilation fingerprint.
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
as local cleanup candidates rather than whole-program proof. Resolved project
references and named imports suppress these candidates. Unresolved call names
alone do not. A candidate returns when the last resolved use disappears,
including after an incremental update or deletion. Retention keeps the
suppressed candidate and its evidence while its declaration remains active.
Public functions,
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

Application-surface compilation connects case-sensitive CSS class and ID
selectors with literal HTML attributes and JavaScript selector/class APIs.
Selectors in compound DOM queries and multiple literal class-list arguments
contribute usage evidence. It emits `unused_css_class` and
`always_overridden_css_declaration` for identical values repeated in one rule.
Different values or separate rules produce `possibly_overridden_css_declaration`
findings that require validation. PHP `include`/`require` partials resolve static
paths to view files. Controller templates also resolve in sibling
`Views` or `views` directories. Template-render edges expose detected `view_variables`.
Adding or removing a template refreshes sources with recorded literal render
or include references.

Python endpoint extraction uses source-local HTTP framework receiver bindings
and literal decorator arguments. Explicit methods and constructor prefixes
become route facts. The standalone `@route` convention remains supported.
Each handler owns its endpoint identity, including when handlers share a route.
The first compile after the change refreshes cached facts and archives stale
endpoint and CSS findings.

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

Synthetic `CodeSymbol` nodes represent external names from parsed code, such as
decorators, bases and type hints. Imported bindings and Python built-ins are
already known to the compiler. Explicit raises record the exception name,
including built-in and imported exceptions.

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

Parser construction uses `Parser(language)` with Tree-sitter 0.25 through 0.27.
Python 3.10 resolves to a compatible release, such as Tree-sitter 0.26.1 and
language pack 1.23.1. Python 3.11 and later can use Tree-sitter 0.27.0 and
language pack 1.25.2. Grammar providers return a capsule or a constructed
`Language` object. The loader wraps the pointer from older standalone Solidity
wheels in a Tree-sitter capsule. Invalid provider values and constructor errors
propagate.

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
- Use the release version in [config.py](../src/config.py) to invalidate persisted
  facts across code and documents when releasing parser changes.
- Test extracted behavior separately through `MemoryGraph.compile_project` as in
  [test_symbol_resolution.py](../tests/test_symbol_resolution.py). Close the
  graph with `contextlib.closing(MemoryGraph.open(...))` or in `finally`.
- Test release invalidation with fixed source, store and compilation options.
  Compile with the new release version and confirm that the next unchanged
  compile reports zero changes. Grammar-package changes affect the artifacts
  that use the corresponding grammar.
  [test_incremental_integrity.py](../tests/test_incremental_integrity.py)

## Document Ingestion

Artifact ingestion turns registered `SourceArtifact` files into parsed
`SourceFragment` nodes with provenance and document-specific relations.

### Supported Inputs

- Markdown: headings, paragraphs, lists, fenced code blocks, inline links, and
  simple pipe tables.
- Plain text and text-like artifacts: paragraph chunks with preserved line
  ranges and character offsets, including unknown UTF-8 or recognized UTF-16
  text files. Recognized code formats use the code parser.
- PDF: text and page-based fragments when the [optional parser](#pdf-fallback)
  is available.
- Code: registered Tree-sitter parsers create technical graph facts and source
  fragments. See [language support](#language-support) for details.
- Image and video files are skipped during project compilation and are not parsed,
  registered, or compiled.

The scanner recognizes UTF-16 with a byte-order mark or a strong alternating
null-byte pattern. Code compilation, document parsing and source previews use
the same encoding detection. Stylesheets, scripts and supported template
formats share the application scope and surface linker classification regardless
of directory. Markdown in a view directory keeps its documentation scope.

Files with a Markdown extension use the Markdown parser. Extensionless files
and `.txt` files with Markdown markup can use it based on a sample of their
contents. A recognized interpreter shebang takes priority over Markdown
comments in extensionless scripts. Reclassification from code to a document
archives the previous code graph. Markdown extracts ATX and single-line Setext
headings. Fenced blocks close on the same marker character with at least the
opening marker count and each block has its own fragment. Plain-text chunks
retain their own line and character spans, including in CRLF files. Document fragments participate in `query_context --docs`.
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
unknown text in supported encodings) are compiled structurally into fragments and document
relations. Document text remains a lower-level source layer used for
provenance, code linking, and query context around the code graph.

The deterministic processor in `memory.extraction.document_processor` creates ranked `Concept` nodes, `RawEvent`
observations, and term co-occurrence relations. Explicit headings produce stable
concepts that are archived when their headings disappear. The
[document relations](REQL.md#document-relations) connect fragments to their
artifact, sections, links, code blocks, and compiled symbols.

Document-code links use unique indexed names, qualified names, paths and routes.
Use inline code or call syntax to reference a plain identifier such as `api`.
An identifier with internal capitals, digits or underscores can also match
its indexed spelling in prose. The same rules apply to headings. The linker
keeps up to eight targets per fragment.

Parser metadata and parser errors are stored on both the artifact and fragment
properties where relevant. See [failure behavior](#failure-behavior) for
compilation outcomes.

### PDF fallback

With `compile.documents.pdf` enabled, the optional `pypdf` dependency extracts
text from the compiler's verified byte snapshot. A missing `pypdf` package
produces metadata-only fragments with `needs_parser`. Failures importing an
installed package are surfaced as compilation errors.

### Adding a document parser

Add document format parsers under `memory/document_ingestion/formats/` and
inherit `BaseDocumentParser` from `memory.document_ingestion.base`. The common
base handles format support checks, text decoding with replacement, file
metadata, and path-derived titles. Register new parsers in
`default_parser_registry()` when they should participate in project
compilation. Keep optional format dependencies graceful, following the PDF
parser pattern.
