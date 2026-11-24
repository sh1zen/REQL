# CLI

Use this reference for the `reql` CLI and the repository launcher. See
[MCP](MCP.md) for server options, client setup and tool schemas.

- [Entry points](#entry-points) and [assistant installs](#assistant-installs)
- [Compilation and history](#project-compilation) and [statistics](#project-and-graph-statistics)
- [Retrieval](#retrieval-commands), [test evidence](#test-evidence), and [source edits](#source-edits)
- [Agent Workspace](#agent-workspace)
- [Repository explanation](#repository-explanation), [pipeline export](#project-pipeline-export), and [reports](#reporting-and-graph-export)
- [Configuration](#configuration) and [storage maintenance](#storage-maintenance)

## Entry Points

Use `reql ...` for normal command-line automation. From a source checkout,
`python cli.py ...` exposes the same command surface and adds `src` to Python's
import path automatically, so it does not require an editable install or manual
`PYTHONPATH` changes.

Use `python launcher.py` for an interactive menu that can open graph storage,
compile projects, retrieve context, run REQL queries, generate reports, export
graphs, and print MCP configuration snippets. `launcher.py` intentionally does
not mirror every CLI flag; use `reql`, `python cli.py`, or `reql-mcp` for
scripts and agent-facing command execution.

## Storage, Output, and Help

Project and cache commands default to `<build path>/.reql/memory.reql`.
Other commands default to `./.reql/memory.reql` from the current working
directory. The CLI always uses these project-local locations.

Use `--json` on supported commands for machine-readable output. Top-level help
lists canonical commands alphabetically, and nested command groups summarize
their subcommands:

```bash
reql --help
reql project --help
reql query_context --help
```

### Declarative command registration

New leaf commands should be described by a `CommandSpec` in
`src/memory/cli.py`. The spec supplies the command path, access mode, parser
configurator, help text, and handler. `build_parser()` installs
registered specs into their parent parser group, while execution uses the same
selected spec to choose read-only versus mutating storage access and invoke the
handler.

All commands that open `MemoryGraph` are registered this way. Commands with a
different lifecycle, such as installation, configuration bootstrap, agent
workspaces and direct block-storage inspection, keep their
dedicated execution paths without duplicating graph access classification.

## Assistant Installs

```bash
reql install codex
reql install claude
reql install
reql install codex,claude --dry-run
reql install claude --no-hooks
reql install codex --command-dir ~/.local/bin
reql install codex --user
reql uninstall
reql uninstall codex,claude
```

`reql install` and `reql uninstall` auto-detect supported coding-agent profiles
by default and write or remove assistant-facing REQL instructions for
deterministic project memory.
Supported platforms are `codex`, `claude`, `opencode`, `kilo`, `cursor`,
`gemini`, `copilot`, `openclaw`, `hermes`, `kimi`, `antigravity`, and
`agents`; use explicit canonical platform names, `all`, or `--all` to override
auto-detection. Auto-detection only uses real assistant profile signals or
assistant commands whose install targets are known. Generic root directories
such as `.codex`, `.github`, and `.agents` are ignored. Codex detection requires
paths such as `.codex/skills` or `.codex/hooks.json`. Claude Code uses
`CLAUDE_CONFIG_DIR` for both detection and user-level installation when set.
Copilot detection requires
Copilot-specific paths such as `~/.copilot/skills`,
`.github/copilot-instructions.md`, or `.github/instructions`. Generic
Agent-Skills detection uses `~/.agents/skills` or `./.agents/skills`. Project
installs only inspect project-local signals, and user installs only inspect
home-scope signals and user commands, so a profile under `C:\Users\...` cannot
cause a project install or uninstall under another drive. When no profile is
detected in an interactive terminal, REQL prints the available disks and asks
for the coding-agent profile disk or path. It retries auto-detection there
first; only if that path has no supported profile does it ask which platform to
install or uninstall. The project is resolved later from the directory where
the agent is launched. In non-interactive scripts or CI, it exits and asks for
an explicit platform such as `reql install codex --user` or
`reql uninstall codex --user`.

By default, installs write project-local files such as
`.codex/skills/reql-agent/SKILL.md`, `.claude/CLAUDE.md`, `AGENTS.md`,
`GEMINI.md`, `.cursor/rules/reql.mdc`, `.kilocode/rules/reql.md`,
`.agents/skills/reql-agent/SKILL.md`, and agent-specific skill/rule
directories. `reql-agent` uses the project graph to bound repository discovery,
then directs the coding agent to verify the current source, callers, contracts,
and tests. For multi-step or resumed work it also covers Agent Workspace
commands described in [Engineering coordination](COORDINATION.md). Small
self-contained tasks skip Agent Workspace.
Pass `--project-dir` to target another project root. Pass
`--user` to write to matching assistant profiles under the home directory.

Generated `SKILL.md` files keep a concise fast path for status, bounded
retrieval, source verification, edits, tests, refresh, and routing. Agents reuse
the graph-defined working set and avoid repeated full-project scans. Bootstrap,
query variants,
graph refresh, reports, documents, and Agent Workspace guidance live only in
routed `references/` files and are loaded when that situation occurs. Platform rules
for Cursor, Copilot, Kilo, and shared instruction files are rendered from the
same canonical rule set so their behavior does not drift or repeat.

The installer also writes a REQL-owned `reql` command shim; use `--command-dir`
to select the shim directory. Claude Code and Gemini CLI hooks are installed by
default and can be skipped with `--no-hooks`. They emit a short reminder on
session startup in a REQL project. `reql uninstall` removes REQL-owned skill
files, version stamps, managed instruction sections, and startup hooks while
preserving unrelated content in shared files. The shared command shim is kept
by default because other project or user integrations may still use it. Pass
the same explicit `--command-dir` used during installation when the shim itself
should also be removed.

## Project Compilation

```bash
reql project compile
reql project history --limit 10
reql project diff
```

Run project commands from the project root. Compilation and
[statistics](#project-and-graph-statistics) share the configured scan policy,
including `scan.max_file_size_mb`. See [Configuration](CONFIGURATION.md#behavior)
for scan patterns and nested scope, and the [compilation pipeline](PIPELINE.md)
for supported inputs, cache fingerprints, stage order and failures.
Use `reql project compile --lock-timeout SECONDS` to wait for graph access
when another process is using the store.

A successful manifest change creates an immutable `ProjectRevision` with a tree
hash, parent revision and file transitions containing old and new SHA-256
values. Use `project history` for the newest-first chain and `project diff`
to inspect the latest change, or add `--revision ID` to inspect an earlier one.

Compile output includes a bounded summary of changed files, added, updated or
archived symbols, and associated tests. Test associations come from compiled
relationships and the conventional `tests/test_<module>.py` path when present.
The full summary is `CompileProjectResult.to_dict()["summary"]`. After a failed
compile, check [project statistics](#project-and-graph-statistics) before relying
on the graph.

### Incremental compilation

`cache clear` archives cache metadata. Matching successful compilation
provenance can recover missing entries. See [cache and fingerprints](PIPELINE.md#cache-and-fingerprints)
for reuse rules and [storage maintenance](STORAGE.md#inspection-compaction-and-clean-rebuilds)
for a clean rebuild.

## Project and graph statistics

```bash
reql stats
reql stats --json
```

Run `stats` from the project root. It reports graph counts, registered artifacts,
the latest compilation and its errors, the source comparison, incremental cache
counts, and imported test evidence. The source comparison and cache report use
one scan with the effective project settings.

JSON keeps `nodes`, `edges`, and `node_types` at the top level and includes
`project`, `workspace`, `cache`, and `tests` sections. `workspace` shows files
added, changed, or deleted compared with the last committed REQL revision.
Source freshness is unknown without a revision or when the scan fails. An
incomplete scan includes errors in `workspace` and leaves `cache` null.

The command opens an existing store read-only. With no store it reports zero
graph counts and null detail sections without creating the store. A failed
compilation is reported under `project.last_compilation`.

## Retrieval Commands

When the project-relative path is already known, bypass semantic retrieval and
graph expansion with `locate`:

```bash
reql locate "docs/setup"
reql locate "docs/setup.md" --json
```

The lookup is case-insensitive and index-backed. If the input has no extension,
REQL checks the exact path and the known documentation suffixes `.md`,
`.markdown`, `.txt`, and `.rst`.
When there are no matches, `locate` exits with code `1` and reports
`Path not found` in text mode or an empty `matches` array in JSON mode.
This outcome does not authorize discovery with other tools.

`query_context` composes a deterministic agent-ready context block:

```bash
reql query_context --query "payment service"
reql query_context --query "payment service" --code --json
reql query_context --query "payment service" --code --source
reql query_context --query "readme FAQ"
reql query_context --query "unused imports" --cleanup
reql query "FINDINGS WHERE finding_type = 'possibly_orphan_directory' RETURN relative_path,file_count,files,cleanup_priority"
```

It is informative by default and returns matching nodes, file/line references,
source links, owner candidates, cleanup candidates, working-set records, and
targeted reads. Use `--code`, `--docs`, or `--test` to limit context to a
scope. Default code text lists files, owners, associated tests and the
source previews already retrieved. Its bounded **Change boundaries** section shows
indexed `WRAPS`, `OVERRIDES`, `INHERITS`, caller and public-surface relations near
the selected owners. `payload.impact.implementation_links` retains source/target
references and edge ids/types in JSON. These are static relationships to verify
in current source, not instructions to create another wrapper or override. Missing
static callers do not prove that a symbol has no users.
When a source path is displayed, symbol labels omit its exact dotted module
prefix. Class/method names and file locations remain visible; JSON keeps full
qualified names. Labels without that path context remain fully qualified.

Previews preserve indentation and show the selected source range.
`requested_line_end`, `truncated` and `next_line` identify incomplete ranges.
Graph fallback text is labeled as indexed graph evidence and never counts as
complete current source. Previews and verified excerpts reuse one file snapshot
per request. JSON retains the full
`read_plan` with file paths and line ranges for targeted reads.

Add `--source` with `--code` or `--test` to embed current source bodies directly.
Python and MCP expose the same opt-in `include_source=True` field. This requires
explicit code or test scopes. REQL merges overlapping spans and reads each
selected file once.
Source paths must resolve inside their registered project root,
even when the graph store or current directory is elsewhere.

JSON `snippets` report `source_status` (`verified`, `stale`, or `unavailable`).
Verified bodies match both artifact metadata and a committed revision manifest.
`line_end` describes the actual excerpt; `truncated`, `requested_line_end`, and
`next_line` identify incomplete spans. `read_plan.snippet_embedded` is true only
when verified text covers the requested span. Reuse complete excerpts; read
uncovered ranges directly. Changed or missing files, uncommitted metadata, and
read failures return a reason without falling back to old graph text. Detected
source changes mark graph freshness stale unless a refresh is already running.
Queries do not compile or mutate the graph. Files can change after capture.
Use `query_context --lock-timeout SECONDS` to wait for a shared graph lease
when another worker is writing. A query without this option reports a busy
graph immediately.

Use `--cleanup` for dead-code and unused-symbol cleanup. Cleanup output
is always conservative and shows safe-remove findings plus medium-priority
directory aggregate findings. `possibly_orphan_directory` groups multiple
isolated code files under one containing directory with `file_count` and
`files`, so cleanup review does not start from one suggestion per file.
Validation-required findings remain available through explicit `FINDINGS`
REQL statements rather than the removal-oriented context builder.

CLI, Python, and MCP use the same `QueryContextRequest` contract and canonical
budget defaults (`top_k=20`, `max_depth=3`, `max_items=20`). JSON output is the
canonical `ContextResult` envelope: `schema_version`, `graph_revision`,
`source_revision`, `freshness` and `confidence` are top-level fields. Trace
metadata and projected context are nested under `payload`.
The `payload.ranking_profile` field reports the REQL release that ranked the results.
In text, the displayed file path supplies its module identity; `(module)` avoids
repeating the qualified module name. Tests already shown in `Files` are identified
as such in `Associated tests`, without repeating their file rows.
When a brief lists exact test files under `Requested files`, lexical test
candidates come from those files. Indexed test associations remain available.

For an unscoped informative query, REQL relates documentation and implementation
files in the same project when indexed evidence shares query terms. Structured
JSON exposes those records as `related_files`. A translation catalog
(`.pot` or `.po`) can appear as a review candidate when its own indexed evidence
matches at least one shared term.

`query_explore` returns dependency slices for concrete code targets:

```bash
reql query_explore --query "payment service serialization" --view owners --view callers --json
reql query_explore --query "payment service serialization" --view serialization_paths
reql query_explore --query "profile template" --view structural_duplicates
```

Views include `owners`, `callers`, `public_surface`, `serialization_paths`,
`docs_mentions`, `structural_duplicates`, and `code`. The structural duplicate
view compares template markup hierarchy, parent-child relations, attributes,
and tag sequences rather than lexical file relevance. It is opt-in by default;
use repeated `--view` flags or shortcut flags to keep output small.

`query_memories` returns compact ranked text rows:

```bash
reql query_memories --query "payment service" --limit 8 --json
reql query_memories --query "payment service" --no-sources
```

It uses the same seed search and request depth as the retrieval
pipeline. JSON output includes trace id, seed ids, ranked nodes, nodes, edges,
sources, parameters, and counts.

For coding agents, `query_context --code --brief` renders owners, spans, boundaries,
associated tests and matched work. A high-confidence literal symbol or strong
owners for a focused query can return complete, hash-verified bodies. A compound query can
include the best separated owner. Complete, verified bodies appear first when
they fit the source budget. A larger body keeps its verified line range. When
a verified body names an indexed project-relative Markdown section such as
`docs/guide.md#setup`, brief context can show its verified line range for a
targeted read. Reuse displayed source and read missing spans only. Brief output
keeps linked tests, groups calls to the same target within one file while
retaining caller spans. Without `--task`, matching observations and checkpoints appear as short
summaries with IDs. Use `reql agent show ID --json` for their details. Decisions,
constraints, and failures retain their evidence within the brief budget.
Broader queries retain the working set and ambiguous matches retain source
locations. Stale and unavailable source carries a reason. Use `--source` to
request verified bodies for the selected reads. Use `--json` for the structured envelope, including exact revisions
and source spans. `--brief` applies only to text output. Add
`--task WORK_ID` to recover a known task or checkpoint and its causal work in
the same response. This is a read-only alternative to a separate work-context
lookup when the agent also needs source.
JSON output also includes `read_plan` and `change_chain` so another
agent can follow the intended intervention path without opening whole files or
patching around missing project context.

Dotted field queries such as `Field.type` can return an end-to-end
`data_trace`. It starts with indexed field declarations and literal references,
then shows related locations in order of graph distance. Labels come from the
recorded relationships and indexed test scope. The rendered context shows the
same records under `End-to-end data trace`. The `serialization_paths` explore
view follows `READS`, `WRITES`, `RETURNS`, `RAISES`, `REFERENCES`, and source
evidence for the requested depth instead of stopping after the first adjacent
edge.

Storage failures return exit code `1`. If the store is busy, retry after the
active command finishes. Use `reql storage locks` to check the current lock mode.
See [storage locking](STORAGE.md#readerwriter-locking).

## Test evidence

Run project tests directly and update the graph with `reql project compile`.
Include the outcome in the task's [verification handoff](COORDINATION.md#verification-handoff).

```bash
reql tests for --symbol SYMBOL_ID --json
reql tests for --file src/auth.py --include-candidates
reql tests import --format coverage-json --input coverage.json
```

`reql stats` reports the number of observed associations and how many are
verified against current files.

Lookups return up to 20 associations by default and support limits from 1 to
100. Each result identifies the test, target, evidence type, provenance and
freshness. A `static` association cites a resolved AST relation. An `observed`
association cites per-test coverage. A `candidate` matches a convention and is
always unverified. The lookup commands read evidence without executing tests.

The coverage.py adapter reads reports generated with `coverage json
--show-contexts`. It resolves `path::case` and `path::Class::case` IDs
against indexed test cases, including parameterized IDs and the pytest-cov
`|run` suffix. Project-local absolute paths and Windows separators are
supported. For producers with other context formats, supply a
`reql.test_contexts` object that maps each context string to
`{"path": "quality/checks.py", "name": "Check.test_invoice"}`. The name
can also be the indexed fully qualified case name. Only executed lines with
a resolved case contribute evidence. Setup and teardown phases are ignored.
The report and all referenced paths must resolve inside the compiled project.

See [test configuration](CONFIGURATION.md#test-discovery-and-evidence)
for test directory expressions and evidence order.

To verify source freshness, include a capture-time `reql.source_hashes` object
in the report. Map project-relative source and test paths to their SHA-256
hex digests at coverage collection time. Observations from standard reports
without those hashes remain unverified. If a captured digest disagrees with
the compiled bytes at import, REQL skips that mapping. An unrelated file
change leaves verified observations current.

Imports serialize on the test evidence store. Each import runs as a transaction,
and an identical report creates no duplicate observations. The default sidecar
is `.reql/test-evidence.reql`.

## Source Edits

`reql edit --plan edits.json` applies line edits to UTF-8 and recognized UTF-16
files against the project's latest compiled revision. Run `reql project compile` to create
the first revision. Use `--plan -` for stdin. Pass a JSON array and copy unique
complete lines from a verified excerpt into `find`. Preserve indentation
and use LF in the JSON strings. Plans accept any number of edits and have
no built-in file or plan size ceiling.

```json
[{"path":"README.md","find":"Old usage.","text":"Updated usage."}]
```

A plan can also use `start_line` and `end_line` from the latest compiled
revision, and can mix both forms.

```json
[{"path":"README.md","start_line":12,"end_line":12,"text":"Updated usage."}]
```

REQL resolves all matches against the original file. A missing, repeated or
overlapping match rejects the plan before writing.

Ranges are one-based and inclusive. Set `end_line` to `start_line - 1` to
insert before a line, or use an empty `text` to delete the selected lines.
Use LF-separated replacement text. REQL checks the revision's file hash and
preserves the encoding marker, existing breaks outside the edit, and the
local line-ending style. Each file replacement is atomic. REQL validates all
plan entries before replacing files and reports any previously replaced paths
if an error interrupts the batch. After a successful write, the command
refreshes the graph through the incremental compiler. A plan that changes no
files skips compilation.
Use `--lock-timeout SECONDS` to set the wait for each graph open during edit and
refresh.

With `--json`, the output includes `changed`, `unchanged` and a `refresh`
result with status `completed`, `skipped` or `failed`. A refresh
failure returns exit code 1 and reports the applied paths and the error.
The writes remain on disk. After resolving the error, run `reql project compile`
to refresh the graph.

## Agent Workspace

The [coordination guide](COORDINATION.md) covers work records, claim and
completion behavior, handoffs and retention. This reference lists
the CLI forms and return fields.

```bash
reql project overview
reql project overview --details --limit 10
reql project context --task WORK_ID --json
reql project context --file src/module.py --query "contract"
reql agent init --name "Contract update"
reql agent task create "Validate consumers" --file src/module.py --next "Run focused checks"
reql agent init --task TASK_ID
reql agent task update --id TASK_ID --why "Unit checks passed" --next "Check consumers"
reql agent show TASK_ID --json
reql agent finish "Consumer checks passed" --task TASK_ID
```

Replace ids with returned values. Task updates require `--id` and take optional
replacement content. REQL reads the current revision and rejects changes that
occur before the write. Use `agent record KIND CONTENT`
to create other work records and `agent record --id ID --revision N` to update
them. Omitted content stays as saved. Use `--key` at creation to deduplicate
within a kind and workstream. Shared write fields include `--status`, `--why`,
repeated `--file`, `--workstream`, `--parent`, repeated `--depends-on`,
`--contradicts`, `--supersedes`, `--summarizes`, `--next` and `--importance 1..3`.

`project context` accepts `--query`, repeated `--file`, `--workstream`, `--task`,
`--history`, `--limit N` and `--json`. Both `project context` and `project overview`
return all matching work by default. Set `--limit N` to limit rows per overview
section or context result. See [retrieval and overview](COORDINATION.md#retrieval-and-overview)
for evidence, `--details`, and marked omissions.

`agent init --next` claims ready work. Add `--workstream NAME` to select a
queue or `--query "literal terms"` to override the task title as the source query.
JSON includes `assignment_status` and `claimed_task`, with
`source_context_retry` when a committed claim needs a separate source lookup.
Use `--source` to include current source bodies in the JSON result.

`agent init --task ID` claims an agreed assignment and shows shared work and
any file overlaps. Add `--query "literal terms"` to retrieve code with the
claim. On success, JSON includes `source_context` with source locations.
Add `--source` with `--query` to embed verified bodies. If graph
storage is busy, the command waits after committing the claim. Use
`--lock-timeout SECONDS` to limit this wait. If graph retrieval fails after
the claim, exit status 2 comes with the committed claim,
available shared work, `source_context_error`, and `source_context_retry`.
Task JSON includes `claim` with the owning agent and session, or null for
unclaimed work. The top-level agent and session describe the latest writer.
Updating progress preserves the claim. To transfer a claim while the old
session is open or unavailable, verify that the owner has stopped editing and
release it with `agent task update --id ID --status open --why "handoff reason"`.
For an assigned brief, use exact symbols in the task's edit files. REQL prefers
their verified bodies and shows indexed ranges when the bodies exceed the
brief. A claim with a pending prerequisite still returns source evidence and
identifies what must finish first.
When assigned Markdown files have no selected passage, the brief shows
verified headings and line ranges within one shared section budget.

After the brief, use a follow-up for missing evidence. Pass the task ID and
use brief text output.

```bash
reql query_context --task ID --follow-up --query "SYMBOL_OR_SOURCE_PATH" --code --test --brief
reql query_context --task ID --follow-up --query "DOCUMENT_PATH_OR_HEADING" --docs --brief
```

Reuse the brief's `--seen-source PATH#LSTART-LEND@SHA256` options for code
and test ranges you already saw. REQL skips these bodies while the hashes
match. A literal file name or project-relative path takes precedence over
related symbols. If the reply gives only an indexed range, read those lines.
Use a regular task query to recover a changed assignment or to restore
context in a new session.

For bounded line edits, use [the standalone edit command](#source-edits).
Refresh the graph after direct edits. Use `agent verify --plan FILE` to run
focused checks before broad checks. Pass `-` as the plan path to read JSON
from standard input. Exit status 0 means all checks passed. Exit status 1
means a check failed. Exit status 2 reports a plan or input error.
Use
`agent finish SUMMARY --task ID` to record the result and complete the task.
See [verification handoff](COORDINATION.md#verification-handoff).

Plain `agent finish SUMMARY` saves a checkpoint and releases unfinished
claims without completing them. Assigned finish also releases any other claims
in the closing session. Blocked work keeps its status. Finish receipts report
the checkpoint id and revision and the released claim count. Recover with
`project context --task CHECKPOINT_ID`. Plain writes return compact receipts.
Use `--json` for structured results and `agent show ID --json` for history.

Use `agent wait ID --timeout SECONDS` after independent work to wait for a
prerequisite. It waits up to 60 seconds by default for `done` or `resolved`.
Repeat `--status` to select other statuses. Exit code 1 means no selected
status was reached. JSON reports `matched` and the latest available `node`.

Identity selection prefers `--agent`/`REQL_AGENT_ID` over activity-derived
identity (`--activity`, `REQL_AGENT_ACTIVITY_ID`, `CODEX_THREAD_ID`).
For explicit scoping, use `reql agent --agent AGENT_ID --activity ACTIVITY_ID init --name NAME`.
Use the host's activity ID for `ACTIVITY_ID`. Ambiguous
private selection fails with guidance. `agent dashboard` shows owned work and
private notes. The dashboard shows five rows per section by default. Use
`agent dashboard --limit N` to set a positive row count. Use `agent task list --all`
or `agent list --all` to include retained completed entries.

`agent note` is private scratch; `note --agent ID` is directed scratch and
`note --public` publishes a dashboard message. `agent search QUERY` searches
work and messages with attribution. Use `--limit` to bound matches and
`--json` for structured results.
`agent status` shows the selected private identity and current session.
Use `agent task done ID MESSAGE` to record a reviewed completion. Use
assigned finish to complete the task and close the current activity.
`agent terminate ID` closes a stale agent's activities. Reconcile and release
its claims explicitly. `agent reset` discards private scratch and preserves
shared work. Save a checkpoint with `agent finish` before a deliberate close.

Long-running agent calls report progress to stderr. JSON goes to stdout. See the
[coordination guide](COORDINATION.md#planning-and-continuation) for managing
parallel work and for claim and finish workflows.

## Repository Explanation

```bash
reql project explain
reql project explain --focus "payment retries" --max-capabilities 8 --max-workflows 4 --json
```

Compile first. `project explain` projects the graph into capabilities,
architectural layers, semantic workflows and change starting points.
It computes this view on demand without persisting inferred entities or
changing graph metrics. The default output is Markdown. `--json` returns the
structured projection. To combine it with operational work, use
[`project overview`](COORDINATION.md#retrieval-and-overview).

The Python facade exposes the same projection.

```python
from reql import MemoryGraph

graph = MemoryGraph.open(".reql/memory.reql", read_only=True)
try:
    explanation = graph.explain_project(
        ".",
        focus="order refund",
        max_capabilities=12,
        max_workflows=8,
    )
    print(explanation.to_markdown())
    payload = explanation.to_dict()
finally:
    graph.close()
```

### Projection Model

The output contains:

- `project`: the compiled project identity and root;
- `basis`: counts and explicit guarantees about the deterministic, read-only
  derivation;
- `layers`: architectural roles and their capability ids;
- `capabilities`: purpose, responsibilities, primary paths, dependencies,
  owners, tests, and ranking score;
- `workflows`: schema-version-2 semantic entities with name, intent, trigger,
  inputs, outputs, indexed exception conditions, participants, and source-backed evidence;
- `change_guide`: focus-ranked starting points and verification paths.

Every code evidence record contains a graph node id, node type, label, relative
path, and available line range. JSON consumers can therefore move from the
business view back to exact code.

Workflow participants are explicit `implemented_by` relations whose targets are
code evidence records. A workflow does not expose an ordered `steps` path:
technical edges support membership, but their traversal order is not presented
as business behavior.

### Deterministic Inference

The service derives the view without model calls:

1. Resolve the registered `Project` and query its active artifacts, modules,
   public code symbols, and indexed graph metrics.
2. Remove test modules from capability ownership while retaining test paths for
   verification guidance.
3. Detect a dominant source package and group code below that boundary into
   stable capability areas.
4. Rank owners with graph degree and optional lexical focus matches.
5. Assign architectural layers from indexed roles and project configuration.
6. Resolve inter-capability dependencies from technical graph edges and local
   resolved imports.
7. Select workflow triggers from indexed endpoints, explicit entrypoint roles
   and project-configured entry conventions. Focus adjusts workflow ranking.
8. Aggregate implementing participants from all outgoing `CALLS`,
   `HANDLES_ROUTE`, `INSTANTIATES`, and `WRAPS` evidence, then corroborate them
   with semantically aligned imported symbols referenced in implementation
   bodies, capability boundaries, signatures, docstrings, and ingested
   documentation when available. Type-only imports and unrelated utilities are
   not promoted to workflow participants.
9. Rank change starting points with the existing lexical index and attach tests
   whose paths match the capability's responsibility vocabulary.

Stable ids for capabilities and workflows are derived from the project and
their graph-backed members. The projection itself is not stored, so running it
does not change node counts, edge counts, salience, or usage.

The projection evaluates every matching active node, lexical match and relevant
relationship in the selected project. Workflow membership follows eligible
indexed calls and corroborated imports through the reachable graph.
Configure workflow names and layer precedence in [analysis settings](CONFIGURATION.md#analysis-and-report-policies).
`max_capabilities` and `max_workflows` only control the size of the rendered
result after the complete graph evidence has been evaluated.

### Interpretation and Limits

The layer describes what the compiled code supports, not product intent that
has no representation in source or ingested documents. Dynamic dispatch,
reflection, dependency injection, generated code, and runtime configuration
can hide relationships from static analysis. Such gaps reduce workflow detail
but do not produce fabricated edges.

Tests, modules, constructors, focus-only matches, `METHOD`, `READS`, and inverse
ownership edges cannot create workflow entities. This prevents structural
paths such as test fixture setup or class-to-module ownership from being
presented as domain behavior.

`--focus` is recommended for modification planning. It ranks the most relevant
symbols and capabilities, but the returned tests and dependencies remain
evidence for investigation rather than a guarantee of complete impact coverage.

## Project Pipeline Export

```bash
reql project pipeline
reql project pipeline --code
reql project pipeline --out reports/pipeline.html
reql project pipeline --code --out reports/
```

`project pipeline` reads an already compiled project and writes a deterministic
high-level flow view. It admits explicit routes, handlers, commands, public API
boundaries, and conventional process entrypoints. When no explicit entrypoint
exists, the export marks public roots as inferred triggers, including one
representative for each independent cyclic flow with a public symbol. Archived
relationships are excluded. Calls, route handling,
instantiation, wrappers, resolved imports, writes, returns, emissions, and
raises provide the evidence. Test-local symbols are excluded, while private
symbols needed to connect an observed flow remain available in component
details.

The default format is interactive HTML and the default destination is
`pipeline.html` in the registered project root. `--code` selects Mermaid and
writes `pipeline.mmd`. An `--out` directory receives the default filename, or
you may pass a matching `.html`/`.htm` or `.mmd`/`.mermaid` file. Relative
explicit output paths are resolved from the current directory. Existing files
are replaced atomically and the command prints only the absolute output path.

The HTML view embeds the projection data and the same vis-network 10.1.5
runtime as the generic graph export. It opens offline and supports pan/zoom,
fit/reset, search, workflow and architectural-layer filters, and source-symbol
inspection. Dense stages use taller columns, and long pipelines continue on
additional rows. Mermaid output is a `flowchart LR` with shared components, observed
outcomes, source-location comments, and dashed feedback edges. The HTML details
panel lists all distinct results for the selected outcome. Runtime-only or
dynamic paths are never invented; an observed terminal means only that static
evidence ends there. The command does not compile missing projects or open a
browser automatically.

## Reporting and Graph Export

```bash
reql project report --output reports/
reql project report --output reports/ --json
```

Reports read the graph and project-local cache metadata. The generator computes
community and hub analysis in memory. On an uncompiled project, the report shows
scan and cache diagnostics with empty graph sections. With `--json` the command prints the generated paths as
`graph_report`, `graph_deltas` and `cache_report`.

### Graph report

`GRAPH_REPORT.md` includes:

- project id, root path, artifact counts, artifact type distribution, and
  language distribution;
- latest compilation run summary, including changed, skipped, deleted files,
  graph updates, and errors;
- cache summary, dirty artifact count, parser versions, chunking versions, and
  skipped file count;
- artifact ingestion details, including top artifacts by size, parser used,
  parser errors, partially readable artifacts, needs OCR, and needs parser;
- code graph summary for modules, classes, functions, methods, variables,
  imports, static-analysis findings, call edges, top symbols, and top cleanup
  candidates;
- communities with size, density, and salience when community data is available;
- `God nodes / hubs` with ranked hub score and reasons when hub analysis returns
  results;
- generic hub warnings when analysis detects overly generic high-degree nodes;
- memory health counts for stale and archived nodes, plus recommended actions.

### Graph deltas

`GRAPH_DELTAS.md` lists recent `GraphDelta` records, including
run id, artifact id, created time, added/updated/archived node counts,
added/updated/archived edge counts, and affected node ids.

### Cache report

`CACHE_REPORT.md` includes:

- project id;
- cached, dirty, deleted, and skipped artifact counts;
- cache entries sorted by relative path, including status and the time each
  artifact was compiled. The default display limit is 50.

The report reads current cache metadata. Markdown lists state how many items
they omit. Use [report settings](CONFIGURATION.md#analysis-and-report-policies)
to raise or remove display limits.

### HTML and JSON graph export

```bash
reql export --out graph.json
reql export --json --out reql-json
reql export --html --out graph.html
reql export --html --json --out reql-graph-out
```

`export --html` writes a standalone browser view. A directory or a path
without an `.html` suffix receives `graph.html`. Add `--json` to write
`graph.json` next to it, or use `--json` alone to write the complete graph to
disk. The machine-readable export includes test nodes.

The browser opens on detected entrypoints and uses the pipeline's root
selection for inferred entrypoints on stored workflow relationships. When no
workflow entrypoints are available, the browser opens on the graph itself.
Each click expands or collapses
one depth of connected nodes. A collapsed branch remains visible where it is
reachable through another expanded path. Test-local nodes are omitted.
Controls include reset, fit, search across the graph, cluster filters and node
details. Search and detail budgets use the project's HTML report settings.
See [project pipeline export](#project-pipeline-export) for the entrypoint-based
view grouped by component.

## REQL

`reql query` executes query-language statements. See
[REQL queries and graph](REQL.md) for syntax, fields, examples and graph analysis.
Use `RETRIEVE ... RETURN ...` to select columns such as source paths, line
ranges or scores.

## Configuration

```bash
reql config init
reql config show
```

`config init` creates a short project override file. `config show` prints all
effective settings. `reql config set compile.documents.markdown true` updates
a validated local option.

See [Configuration](CONFIGURATION.md) for the settings schema, scan patterns,
document policies, nested configuration, environment overrides and precedence.

## Diagnostics

Set `diagnostics.enabled` to `true` and `diagnostics.path` to
`".reql/profile.jsonl"` under `diagnostics` in `reql.conf` to append structured
performance events for commands that run compile or retrieval work. The JSONL
log includes phases such as `compile.scan`, `compile.plan`,
`compile.artifact`, `compile.transaction`, `retrieval.lexical_search`,
`retrieval.expand`, `query.parse`, `query.evaluate`, and `graph.close`, with
durations and relevant counters.

## Storage Maintenance

```bash
reql storage inspect --json
reql storage locks
reql storage compact
reql storage clear --json
```

`storage clear [PATH]` uses the current directory when `PATH` is omitted.
See [storage maintenance](STORAGE.md#inspection-compaction-and-clean-rebuilds)
and [locking](STORAGE.md#readerwriter-locking) for what these commands read and change.

## MCP Server

Start the optional server with `reql-mcp --read-only`. The
[MCP guide](MCP.md) covers stdio and HTTP transport, tools and client configuration.
