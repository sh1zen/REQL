# Configuration

REQL ships [core.yaml](../src/config/core.yaml) for internal policy.
Project configuration can override settings in [user.yaml](../src/config/user.yaml),
the project setup lists and the project-specific analysis settings described
below. The same rule
applies to `reql config set`, environment overrides and caller overrides.
`reql config init` creates a short `reql.conf` with a scan exclude list and a
commented example. Add the overrides your project needs. Omitted settings
use the built-in defaults. By default, `scan.include` and `scan.exclude` join
the default lists without duplicates. Set `scan.ignore_defaults: true` to
replace the lists.
Implicit lookup selects the nearest enclosing `reql.conf`. Set `REQL_CONFIG`
to select a specific file.

Create a sample file:

```bash
reql config init
```

Inspect the effective configuration:

```bash
reql config show
```

Add or update one validated option in the local `./reql.conf`:

```bash
reql config set retention.agent_sessions 30
reql config set scan.exclude '["generated/"]'
reql config set compile.documents.markdown true
```

Values accept plain text, numbers, booleans, or JSON lists and objects. The
command creates `./reql.conf` when it does not exist.

## Example

```yaml
scan:
  exclude: ["generated/", ".tmp/"]

compile:
  documents:
    markdown: true
```

To upgrade a configuration that used the former Git integration, remove
`scan.use_gitignore` and `workspace.mode` from project files and overrides.
Put project-specific excludes in `scan.exclude`. To read a graph previously
stored in a branch-specific directory, open its file explicitly with
`MemoryGraph.open(path)`.
Remove the former `retrieval.ranking_profile` setting and any core extraction
or scoring options copied from older full templates.

## Extraction, retrieval, and query policies

Use `reql config show` to inspect the effective configuration. `core.yaml`
defines lexical and extraction policy, retrieval scoring and thresholds,
graph query columns and scoring, and project setup defaults. Ordered policy
lists replace their defaults. Mapping overrides merge by key.

The lexical index reads complete node text and searchable metadata. Its
scoring weights are defined in `core.yaml`. Searches score all eligible
candidates before applying the requested result count.

Use `LIMIT`, `TOP`, `DEPTH`, and `MAX TEXT` in
[graph query statements](REQL.md#first-class-graph-commands) where supported.

The core document extraction policy is part of the compilation fingerprint. The block store rebuilds derived
lexical postings when the checkpoint has a different indexing policy or lacks
policy metadata. Read-only queries can rebuild in memory. A compile or storage
compaction can persist the rebuilt index.

Retrieval ranks eligible candidates across the requested depth. Request
parameters control the number of results. Source capture checks selected files
against the committed manifest.

Set `retrieval.source.brief_max_source_chars` to cap embedded source in each
code brief. The default is 12,000 Unicode code points. Use `0` to show verified
locations only. REQL checks the file hash and loads the body only where needed
for the selected evidence. `max_items` limits the number of headings in the
outline of an assigned document. Explicit `--source` requests use their
retrieval limits.

Set `retrieval.source.setup_headings` to an ordered list of complete headings.
Matching ignores case and repeated whitespace. The brief checks
`setup_preferred_paths` first, then looks for matching indexed headings elsewhere
in the project. Preferred paths use repository-relative POSIX syntax. Only
current, hash-verified section locations are shown. Both lists are empty by
default. Set at least one heading to enable setup suggestions.

```yaml
retrieval:
  source:
    setup_headings: ["Avvio locale", "Installation"]
    setup_preferred_paths: ["guide/onboarding.md"]
```

## Storage and workspace policy

The `storage` section in `core.yaml` defines internal storage policy.
Use `reql.conf` for project settings in `workspace`.

- `storage.block_size_bytes` applies when creating a store. Existing checkpoints
  use their recorded block size. The defaults are 65,536 bytes per block,
  128 cached blocks (`page_cache_blocks`), a dense-node threshold of 1,024
  (`dense_node_threshold`) and an 8 MiB WAL checkpoint threshold
  (`checkpoint_wal_bytes`). Zero checkpoints after each compile evaluation.
- The block adapter uses [native reader and writer locks](STORAGE.md#readerwriter-locking).
- `workspace` controls `default_agent_id`, `private_store_name`,
  `dashboard_store_name`, and `agents_dir`. Store and directory names each take
  a single filesystem name. Update the paths when moving existing workspace data.

## Behavior

- `cache.enabled` controls whether compilation reuses and writes artifact cache
  entries. File fingerprints use SHA-256.
- `mcp.host` and `mcp.port` set the HTTP bind address and port. Defaults are
  `127.0.0.1` and `8765`. `reql-mcp --host` and `--port` override them.
  Use `--transport http` to select HTTP when configuring the address in a file.
- MCP resolves tool limits at startup for both transports. `mcp.max_top_k`,
  `mcp.max_depth` and `mcp.max_items` use the shared retrieval ranges
  `1-50`, `0-5` and `1-50`. `mcp.max_query_rows`, `mcp.max_nodes`,
  `mcp.max_edges` and `mcp.max_text_chars` default to `200`, `200`, `400`
  and `2000`. Their minimums are `0`, `1`, `0` and `80`, respectively.
  See [MCP tools](MCP.md#tools) for scope and precedence.
- `retention.agent_sessions` is the maximum number of completed agent sessions
  retained on the shared dashboard across the project. Completed private stores
  are removed regardless of this limit.
- `scan.max_file_size_mb` defaults to `0` for unlimited file size. Set a positive
  value to opt into a scan ceiling. `project compile`, `stats` and `project report`
  share this policy with the scan include and exclude lists. `scan.include` retains
  glob matching. `scan.exclude` uses one strict, scope-aware grammar: a literal
  relative path, optionally prefixed with `./` and optionally followed by `/`;
  `*suffix` is allowed only as the final path segment. A rule prefixed with
  `./` is anchored to the directory containing that `reql.conf`; an unprefixed
  rule can match at any depth below it. Thus `./dir`, `./file.py`, and
  `./*.generated` match only directly in the config directory, while `dir`,
  `file.py`, and `*.generated` match at every depth. `dir/*.generated` matches
  files anywhere inside every directory named `dir`; `./dir/*generated`
  matches anywhere inside only the config directory's direct `dir` child. A trailing
  slash does not change matching. Absolute paths, `..`, empty segments,
  backslashes, and every other glob form are configuration errors.
- `scan.ignore_defaults` controls list merging. With `false` (the default),
  project `include` and `exclude` entries are appended to the internal lists
  and deduplicated. With `true`, the project lists replace the built-in rules.
  In this mode, add operational paths such as `.reql/` explicitly
  when they must remain excluded.
- Default exclusions skip generated REQL bundles at `skills/reql-agent/` at
  any depth across supported agent profiles. Other skills and project
  instructions remain eligible for indexing.
- `compile.ingest_documents` is the global document-ingestion switch.
  `compile.documents` maps format names to booleans; every built-in format is
  disabled by default, and a project `reql.conf` only needs to list the formats
  it wants to change. The internal `compile.document_formats` registry in the
  built-in configuration maps each format to its `extensions` and optional
  `filenames`. Projects may override these definitions too. Tree-sitter is
  always used for supported code parsing. PDF
  parsing requires `compile.documents.pdf`. For optional dependencies and graph
  output, see [document ingestion](PIPELINE.md#document-ingestion).
  The effective compile and scan settings are represented internally by one
  immutable `CompilationOptions` object, including normalized typed
  `DocumentPolicy` entries. The same object drives scanning, parsing, and
  the cache options hash.
- `cache.enabled = false` disables cache reuse and new successful-entry
  writes. Compilation still reconciles deleted artifacts and invalidates their
  existing cache entries; it can read and update cache metadata for that cleanup.
- `analysis.enable_hubs` and `analysis.enable_communities` control automatic
  analysis in Markdown reports. The MCP `reql_hubs` tool also respects the hub
  switch. REQL statements such as `HUBS` and `COMMUNITIES` run when invoked.
- `reporting.output_dir` is used by `project report` when `--output` is not
  provided.
- `diagnostics.enabled` controls structured JSONL performance logging.
  `diagnostics.path` is required when diagnostics are enabled.
- `retention.commits` is the positive number of meaningful project versions to
  retain. A REQL commit is a successful compilation that changes the project
  manifest and creates a `ProjectRevision`; a clean compile does not count.
  The default is `20`. When a new commit exceeds the limit, older project
  history, archived graph records, and project-owned usage events become
  eligible for removal.
- `retention.agent_sessions` is the non-negative number of completed agent
  sessions retained on the shared public dashboard across the project. The default
  is `20`; `0` discards completed public history immediately. Identity, finish
  message, and message records owned by older completed sessions are removed
  together; active sessions are never removed. Init, finish, and terminate enforce
  this limit and remove completed private stores independently of it.

Install parser dependencies before compilation; see
[Tree-sitter dependencies](PIPELINE.md#tree-sitter-dependencies).

## Test discovery and evidence

Set test directories and evidence order in `reql.conf`. A supplied list replaces
the default list for that setting.

```yaml
testing:
  discovery:
    directories: ["tests?", "specs?", "__tests__", "quality/cases-[0-9]+"]
  suggestions:
    evidence_order: ["static", "observed", "candidate"]
```

| Setting | Behavior |
| --- | --- |
| `testing.discovery.directories` | Match each regular expression against a complete directory name or a complete project-relative directory path. Matching ignores case and uses forward slashes. A matching directory includes its subdirectories. The default names are `test`, `tests`, `spec`, `specs`, and `__tests__`. Use `[]` to rely on built-in filename detection alone. |
| `testing.suggestions.evidence_order` | List `observed`, `static`, and `candidate` once each. The list orders evidence of equal freshness. The default order is `observed`, `static`, `candidate`. |

REQL also detects filenames with stems that start with `test_` or end with
`_test`, and ECMAScript stems that end with `.test` or `.spec`. In test files,
it recognizes conventional case names and supported ECMAScript registrations.
Candidate suggestions use conventional filename and symbol matches.

Run `reql project compile` after editing discovery conventions so the new scope
and case markers are available to queries and coverage import.

## Analysis and report policies

Use `reql config show` to inspect the effective settings. `core.yaml` defines
analysis weights, thresholds and eligible types. Activation, hubs, bridges,
centrality and specificity use all eligible relationships in their scope.
Label propagation runs until the community labels settle.

| Project option | Settings |
| --- | --- |
| `analysis.enable_hubs`, `analysis.enable_communities` | Enable automatic analysis in project reports. |
| `analysis.architecture` | Source roots, optional project-specific name filters, symbol suffixes, layer vocabulary, precedence and fallback. |
| `analysis.workflow` | Entry names and roles, shared interface path tokens, optional project-specific term filters and root inference. |
| `reporting.output_dir` | Default output directory for `reql project report`. |
| `reporting.markdown` | Display limits for sections, deltas, cache rows, communities and hubs. |
| `reporting.html` | Graph and pipeline search limits, plus graph text and detail limits. |

Project policy mappings accept partial overrides. A list replaces its default
in the supplied order. Use lower-case path and role vocabulary and the original
case for symbol suffixes.
Set source roots, entry names, layer terms and symbol suffixes to match your
project's naming conventions. These lists are empty by default.
For example, configure `models` as a domain term and give `domain` precedence
over `interface` to classify `src/api/models/widget.py` as domain code.

Set the Markdown `section_limit`, `delta_limit`, or `cache_limit` to `0` to
display the corresponding list without that limit. The `analysis_limit`
limits community and hub rows after complete results are ranked.
Markdown shows every parser error for each listed artifact. HTML search,
edge list, and property item limits also accept `0` for an unlimited list.
Graph HTML shows all inferred roots, and pipeline HTML shows all distinct
outcomes for the selected node and workflow. Both HTML exports open offline.

```yaml
analysis:
  architecture:
    layer_precedence: ["domain", "interface", "application", "infrastructure"]
    domain_tokens: ["models"]
  workflow:
    entry_names: ["launch", "main"]

reporting:
  markdown:
    delta_limit: 0
  html:
    graph_search_limit: 50
```

## Overrides

Project commands always use the current working directory and search upward
from it for the nearest `reql.conf`. Cache commands can still accept their own
optional path. If no `reql.conf` exists, REQL starts from its built-in defaults.

When a parent project is compiled, REQL also discovers `reql.conf` files in
visited subdirectories. Their `scan.exclude` entries are added only for that
subtree and are resolved relative to the directory containing the nested file.
Directories already excluded by a parent rule are not visited. Other settings
in a nested config are validated, but affect a command only when that subpath is
itself used as the command root.

For example, a subdirectory can contain only the exclusions it needs:

```yaml
# services/search/reql.conf
scan:
  exclude:
    - ./generated/
    - fixtures/*.snapshot
```

Precedence is:

1. Bundled values from `core.yaml` and `user.yaml`, merged by option.
2. An explicit config path, otherwise `REQL_CONFIG`, otherwise the nearest project
   `reql.conf`.
3. `REQL_CONFIG_OVERRIDES`.
4. Explicit caller overrides such as MCP `config_overrides`.

At every merge step, scalar values use the later value. `scan.include` and
`scan.exclude` are joined in order and deduplicated unless
`scan.ignore_defaults` is `true`, in which case they are replaced.
`compile.documents` is merged by format, so a local file can toggle one entry
without repeating the others. `compile.document_formats` is the separate,
overridable format-to-extension registry. `compile.ingest_documents` is
directly overridable.

Environment overrides:

```bash
REQL_CONFIG=./reql.conf reql config show
REQL_CONFIG_OVERRIDES='{"project": {"id": "agent-a"}, "cache.enabled": false}' reql config show
REQL_CONFIG_OVERRIDES='project.id=agent-a; scan.max_file_size_mb=2' reql config show
```

MCP tools accept `config_path` and `config_overrides`, and `reql-mcp` also
accepts `--config` and repeated `--set` flags at startup.

## Loader

```python
from memory.config import load_config, load_effective_config

config = load_config("reql.conf")
effective = load_effective_config("reql.conf", overrides={"scan.max_file_size_mb": 2})
```

REQL parses the bundled YAML files and `reql.conf` with its built-in parser.

Invalid sections, unknown options, or wrong value types raise a clear
`ConfigError`.
Quoted values preserve `#` and escaped quotes. Comments start at a separated
`#` outside quotes. Unterminated quotes and duplicate mapping keys report the
configuration path and line instead of silently changing a setting.
Scan size limits must be finite and non-negative. Zero leaves file size
unlimited. Nested and dotted caller
overrides are merged without mutating the supplied mappings.

Python compilation overrides accept `CompilationOptions` or `None`.
Build options with `CompilationOptions.from_config(config)` or
construct typed `DocumentPolicy` entries. CLI and MCP still load `reql.conf`
through the configuration owner.

Shared work keeps its records and revisions. Session retention governs public
messages and roster entries. [Engineering coordination](COORDINATION.md#retention)
