"""Generate REQL skills and rules with ASD-STE100-style prose and unchanged code literals."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillSource:
    name: str
    title: str
    description: str
    summary: str
    workflow_steps: tuple[str, ...]
    rule_points: tuple[str, ...]


@dataclass(frozen=True)
class SkillResource:
    path: str
    content: str


DISCOVERY_POLICY = (
    "Use REQL for repository discovery. Use other discovery tools only if REQL cannot run or reports an operational error. "
    "Zero results do not count as a failure, including a `locate` miss with exit code `1`. "
    "Refine the REQL query or report the gap."
)
_DISCOVERY_COMMANDS = (
    'Use `{command_name} locate PATH` for known paths and '
    '`{command_name} query_context --query "literal terms" --code --brief` for unknown code.'
)

PROJECT_SKILL_SOURCE = SkillSource(
    name="reql-agent",
    title="REQL Coding Workflow",
    description="Find REQL source evidence, coordinate tasks, and resume handoffs in {platform_name}.",
    summary="Reuse context you already have. Verify changes against current source and tests.",
    workflow_steps=(
        f"{DISCOVERY_POLICY} {_DISCOVERY_COMMANDS} Reuse the assigned brief and read only missing evidence.",
        'Use `agent init --next` for a shared queue or `agent init --task ID --query "literal terms"` for an agreed assignment that needs source. Coordinate overlaps before editing.',
        'Save decisions, blockers, and the next action in shared work. Recover a task or checkpoint with `project context --task ID`. See `references/agent-workspace.md` for progress and handoffs.',
        'Run the supplied finish command for saved checks. Wait for its refresh. `edit` refreshes the graph. After direct edits with separate checks, run `project compile` once and use `project diff` to inspect the recorded changes.',
    ),
    rule_points=(
        f"{DISCOVERY_POLICY} {_DISCOVERY_COMMANDS}",
        'Reuse verified excerpts. Use the supplied finish command for a task with saved checks. `{command_name} edit` refreshes the graph. For direct file edits with separate checks, run `{command_name} project compile` once.',
        'Pass the task ID for assigned retrieval. Reuse the brief, batch edits, and preserve encoding and line endings. Use `reql-agent` (/reql) for writes, coordination, or recovery.',
    ),
)

def skill_markdowns(
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> tuple[tuple[str, str], ...]:
    return (
        (
            PROJECT_SKILL_SOURCE.name,
            skill_markdown(
                PROJECT_SKILL_SOURCE,
                platform_name,
                project=project,
                command_name=command_name,
                command_path=command_path,
                fallback_command=fallback_command,
            ),
        ),
    )


def skill_resources(
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> tuple[tuple[str, str, str], ...]:
    project_resources = _project_skill_resources(
        platform_name=platform_name,
        scope=_scope(project),
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )
    return tuple(
        (PROJECT_SKILL_SOURCE.name, item.path, item.content)
        for item in (*project_resources, _agent_workspace_resource(command_name=command_name))
    )


def skill_markdown(
    source: SkillSource,
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> str:
    workflow = _numbered(source.workflow_steps, command_name=command_name)
    reference_routing = _reference_routing()
    return f"""---
name: {source.name}
description: {source.description.format(platform_name=platform_name)}
---

# REQL Coding Workflow

{source.summary}

{workflow}

## References

{reference_routing}

Open a reference only when it answers a missing question.
"""


def _reference_routing() -> str:
    return "\n".join(
        [
            "- For setup, configuration, or assistant installation, read `references/bootstrap.md`.",
            "- For files, source, tests, or memory search, read `references/query.md`.",
            "- For REQL statements, dependency views, or cleanup findings, read `references/query-advanced.md`.",
            "- For graph updates, history, cache, or storage, read `references/graph-updates.md`.",
            "- For architecture, pipeline views, reports, exports, or MCP, read `references/reports-exports.md`.",
            "- For documents, read `references/document-semantics.md`.",
            "- For claiming tasks, parallel coordination, session recovery, or record formats, read `references/agent-workspace.md`.",
        ]
    )


def _project_skill_resources(
    *,
    platform_name: str,
    scope: str,
    command_name: str,
    command_path: Path,
    fallback_command: str,
) -> tuple[SkillResource, ...]:
    usage = _command_usage(command_name=command_name, command_path=command_path, fallback_command=fallback_command)
    openai_yaml = """interface:
  display_name: "REQL Coding Workflow"
  short_description: "Find source context and save shared work."
  default_prompt: "Use $reql-agent to find source context. Check the source and tests. Save important work results. Update the graph after changes."
"""
    bootstrap = f"""# REQL reference: graph setup and project status

Read this reference for graph setup, command failures, or compilation problems. Start a task with the lookup itself when the graph already works.

## Command paths

{usage}

## Build or refresh the graph

Run commands from the project root. The default store is `.reql/memory.reql` in that directory. Start discovery with the lookup itself. When the graph is missing or you need to refresh it, run:

```bash
{command_name} project compile
```

To check the last compilation, source changes, graph counts, cache, and imported test evidence, run:

```bash
{command_name} stats
```

If a parser dependency is missing, repair the declared REQL dependencies in the launcher Python environment. Then try compilation again. Do not repeat the same failed compilation without a change. If compilation remains unavailable or fails again, report the problem. Continue with specific source checks. Do not report incomplete results as complete evidence.

## Source selection

Use REQL results to select files, symbols, owners, line ranges, relations, and tests. If information is missing, try one narrower query. For a missing relation, use `query_explore`.

Use text output first. Use `--json` only when structured fields answer a specific question. Read the selected source and the related callers, contracts, and tests. Do not repeatedly compile a usable graph. Do not replace a specific query with a copy of the complete repository.

For relations missing from one result, try an exact symbol, path, or graph query in REQL. Stop when the owner, effects, change locations, and tests are clear.

## Exclusions

Set additional compilation exclusions under `scan.exclude` in the project `reql.conf` file. Use `./` for rules relative to the configuration directory. Omit `./` to match at any depth. Only the final path segment permits the wildcard form `*suffix`. Do not use patterns for the complete workspace, such as `*`, `**`, or `**/*`. Do not exclude source or framework roots that the task needs.

## Configuration

Project commands search for `reql.conf` from the target path through its parent directories. Internal defaults apply first. Project settings, environment overrides, and explicit command overrides follow in that order. Lists merge by default. `scan.ignore_defaults: true` replaces the default scan lists. Check the effective configuration before changes:

```bash
{command_name} config show
{command_name} config init
{command_name} config set KEY VALUE
```

`config init` creates a template if absent. `config set` validates and saves one local option. `REQL_CONFIG` selects a configuration file. `REQL_CONFIG_OVERRIDES` supplies JSON overrides. Keep compilation independent of model providers.

## Assistant installation

Use `install <platform>` to update generated skills, rules, and supported hooks. Use `--user` for a user profile. The default scope is the current project. Use `--dry-run --json` to inspect planned files. `--no-hooks` omits hooks. `uninstall <platform>` removes REQL-owned files and sections. Preserve other instructions. Do not change personal profiles unless requested.

Installation: {platform_name} ({scope}).
"""
    query = f"""# REQL reference: specific queries

REQL matches terms and graph links. It does not interpret requirements. Use short literal identifiers, paths, or error text.

{DISCOVERY_POLICY} For requirements, use `--docs`.

Unknown freshness on an empty result does not require compilation. Current freshness means the result has a saved project revision and its source checks found no changed files.

Select the command for the missing information:

- For a known path, use `{command_name} locate "path/to/file"`.
- For owners, callers, implementation links, and tests, use `query_context --query "<terms>" --code`.
- Use `query_context --code --source` for verified bodies. Read any remaining spans using the returned paths and line ranges.
- For callers and owners, use `query_explore --query "<terms>" --view owners --view callers --view code`.
- For ranked text, use `query_memories --query "<terms>" --limit 8`.
- For custom columns, more views, or unused code, read `query-advanced.md`.

Use `--json` for structured fields: `owner_candidates`, `read_plan`, `contracts`, `impact`, and `test_targets`.

Use dotted field names for cross-layer data traces. The `data_trace` rows show observed field uses and transformations. Check current source to confirm static links. Source confidence and graph revision do not validate attached engineering claims.

Add `--source` to `--code` or `--test` when you need source bodies. This reads current files and checks their hashes against the committed revision. It returns at most three files, six regions, 120 lines, and 6,000 source characters. Overlapping regions are merged. Files larger than 4 MiB are excluded.

Reuse complete verified excerpts. Read only the selected source ranges that need verification. The graph does not refresh during this query.

Keep the selected files with the task. Stop queries when the owner, effects, and tests are clear.

`query_context` includes bounded shared work. Use `project context --file PATH` for missing engineering history. Use `project context --task ID` for assigned work. Read marked omissions with `agent show ID --json`.

"""
    query_advanced = f"""# REQL reference: raw queries, graph relations, and unused code

Read this reference for custom rows, missing relations, or unused-code checks. For ordinary queries, read `query.md`.

For project records, use `query "PROJECTS LIMIT 20"` or `query "ARTIFACTS LIMIT 20"`.
For analysis or cache records, use `query "HUBS LIMIT 20"` or `query "CACHE STATUS"`.
`WHERE` permits LIKE, ILIKE, REGEX/MATCHES, BETWEEN, IN, IS NULL, and IS NOT NULL.

## Raw REQL Statements

Use `{command_name} query "..."` for exact ids, custom columns, source locations, graph relations, findings, or cache state. These queries return rows instead of a context summary.

For text search with specific columns, use `RETRIEVE ... RETURN ...`:

```bash
{command_name} query "RETRIEVE '<literal terms>' LIMIT 8 RETURN id,type,text,score,relative_path,line_start,line_end"
{command_name} query "RETRIEVE '<literal terms>' LIMIT 8 RETURN id,type,text,score,source_for,relation,direction,relative_path,line_start"
```

For a known filter, id, file, symbol, or relation, use `FIND`, `SYMBOLS`, `FRAGMENTS`, or `MATCH`:

```bash
{command_name} query "FIND nodes WHERE id IN [document_term:abc, document_term:def] RETURN id,type,label,text"
{command_name} query "SYMBOLS WHERE relative_path = 'path/to/source.py' RETURN type,name,qualified_name,start_line,end_line LIMIT 50"
{command_name} query "FRAGMENTS WHERE relative_path = 'README.md' RETURN id,text,line_start,line_end LIMIT 20"
{command_name} query "MATCH (s)-[:REFERENCES]->(t) WHERE s.relative_path = 'README.md' RETURN s.id,s.text,t.type,t.name LIMIT 20"
```

Set a `LIMIT` in each raw query. Request only the columns that the task needs. For source evidence, include location or relation fields. Examples are `relative_path`, `line_start`, `line_end`, `source_for`, `relation`, and `direction`.

Other statement forms provide focused graph operations:

- `SEARCH` returns ranked nodes. `RETRIEVE` also returns connected source text.
- `ACTIVATE` follows weighted links from known node ids.
- `PATH FROM ... TO ... DEPTH N LIMIT N` finds bounded paths.
- `EXPLAIN NODE` shows neighbors. `EXPLAIN SEARCH` shows ranking evidence.
- `STATS` and `STATS TYPE` return graph counts.

Use `INCLUDE ARCHIVED` only when historical source records are needed. Filters, types, columns, and limits depend on the statement.

First, use `query_context`, `query_explore`, or `query_memories` to get ids or file paths. Do not use raw queries to scan the complete repository.

## Dependencies

For dependencies around a known target, use `query_explore`:

```bash
{command_name} query_explore --query "<terms from user request>" --view owners --view code
{command_name} query_explore --query "<terms from user request>" --view callers
{command_name} query_explore --query "<terms from user request>" --view serialization_paths
{command_name} query_explore --query "<template terms>" --view structural_duplicates
{command_name} query_explore --query "<terms from user request>" --view owners --view callers --view public_surface
```

Select the view for the required evidence:

- Use `owners` for the implementation location.
- Use `callers` for affected consumers.
- Before symbol removal or name changes, use `public_surface` for exported symbols.
- Before model or storage field changes, use `serialization_paths`.
- Use `docs_mentions` for documentation and examples.
- Use `structural_duplicates` for repeated template markup.
- Use `code` for file selection and source line ranges.

## Unused code

For unused-code tasks, start with REQL findings. Do not create a second repository scanner. Check each possible removal in the current source. Public APIs, callbacks, entry points, reflection targets, and plugin hooks can have uses outside the graph.

Use these steps:

1. Check that compilation completed.
2. Run `{command_name} query_context --query "unused code dead code cleanup" --cleanup`.
3. List the findings:

```bash
{command_name} query "FINDINGS WHERE finding_type IN ['unused_variable','unused_import','possibly_unused_function','possibly_unused_method','possibly_unused_class','possibly_orphan_directory'] RETURN finding_type,severity,cleanup_priority,symbol_type,symbol_name,qualified_name,relative_path,directory,file_count,files,line_start,reason,evidence_scope,confidence ORDER BY cleanup_priority LIMIT 100"
```

4. For unclear rows, get source evidence:

```bash
{command_name} query "VERIFY FINDING FINDING_ID"
{command_name} query "MATCH (s)-[:HAS_FINDING]->(f:StaticAnalysisFinding) RETURN s.type,s.name,s.qualified_name,f.finding_type,f.relative_path,f.line_start,f.reason LIMIT 100"
```

`VERIFY FINDING` returns compiled snippets, known uses, checked scopes, and risks. It does not read current files.

5. Check the selected files and their callers and importers. If necessary, search for entry points, tests, public exports, callbacks, reflection, and documentation examples.
6. Separate safe removals, possible public API risks, directory checks, and incorrect findings.

The `possibly_unused_function`, `possibly_unused_method`, and `possibly_unused_class` findings do not prove that code has no external users. First, check high-priority `unused_variable` and `unused_import` findings.

The `possibly_orphan_directory` finding groups isolated code files under one directory. Its `file_count` and `files` fields identify those files. Before directory removal, check entry points, plugins, scripts, dynamic imports, and external users. Get stronger evidence before removal of public symbols, generated adapters, CLI/MCP tools, or framework lifecycle methods.

"""
    graph_updates = f"""# REQL reference: graph updates and maintenance

Read this reference to update the graph, inspect compilation history, or check the cache and storage.

## Graph updates

The supplied finish command refreshes the graph after saved checks pass. If you
ran project checks separately, refresh once:

```bash
{command_name} project compile
```

Use `cache clear` only when cache removal is authorized. It is not a normal refresh step.

## Cache and deltas

Check the cache and recent compilation changes:

```bash
{command_name} stats
{command_name} project history --limit 10
{command_name} project diff
{command_name} query "DELTAS LIMIT 10"
{command_name} query "DELTAS WHERE id = 'delta:...' LIMIT 1" --json
```

Use `{command_name} project compile` for initial compilation and later graph updates.

`project history` lists retained revisions. `project diff --revision ID` shows the saved delta for one revision. It does not show uncompiled source edits.

## Storage

Use `storage inspect` for block and index details. Use `storage locks` for current owners. `storage locks --recover-stale` removes only locks proven stale. Use it only for authorized lock recovery.

`storage compact` rewrites storage without discarding graph history. It is maintenance, not a required refresh step. `storage clear` rebuilds the current project and discards historical graph state. Use it only for an authorized rebuild. Use `--help` for each operation before maintenance.
"""
    reports_exports = f"""# REQL reference: reports, exports, and MCP

Read this reference for project reports, graph analysis, exports, or MCP setup.

## Project views

```bash
{command_name} project explain
{command_name} project pipeline --out reports/pipeline.html
{command_name} project pipeline --code --out reports/pipeline.mmd
{command_name} stats
```

`project explain` returns source-backed architecture and capabilities. `project overview` also includes shared engineering work. `project pipeline` writes observed execution flows as HTML or Mermaid. It requires a compiled project and does not infer runtime-only calls. `stats` reports graph counts.

## Reports

Write the project reports:

```bash
{command_name} project report --output reports/
```

The output includes `GRAPH_REPORT.md`, `GRAPH_DELTAS.md`, and `CACHE_REPORT.md`. Use these reports for project summaries, cache status, deltas, symbols, communities, and hubs.

## Analysis commands

```bash
{command_name} query "COMMUNITIES LIMIT 20"
{command_name} query "HUBS LIMIT 20"
{command_name} query "HUBS TYPE Function,Class LIMIT 10"
{command_name} query "EXPLAIN HUB 'NODE_ID'" --json
```

The `COMMUNITIES` and `HUBS` commands save analysis records. Apply the same approval rules as for other write commands.

## Exports

```bash
{command_name} export --json --out reql-graph-out
{command_name} export --html --out graph.html
{command_name} export --html --json --out reql-graph-out
```

HTML export creates `graph.html` with embedded data, search, filters, and node details. JSON export writes `graph.json`.

## MCP

Start the optional MCP server only when the client needs live tool access:

```bash
reql-mcp --read-only
reql-mcp --config reql.conf --set project.id=agent-a --read-only
```

If the package is installed but `reql-mcp` is unavailable, use `python -m mcp.server --read-only`. If necessary, use the same `--config` or `--set` options.

Use read-only mode for context queries. Apply CLI approval rules to write tools, such as compilation and hubs.

Use the MCP tool schemas for arguments, limits, and available operations:

- Use `query_context`, `query_explore`, `query_graph`, and `query_memories` for source context.
- Set `include_source: true` with code or test scopes in `query_context` for verified source. Use `reql_query` for bounded REQL rows.
- Use `reql_project_status` for compilation status.
- Use `reql_work_context` for a known task, checkpoint, or file. Use `reql_work_overview` to find active work and checkpoints.
- Use `reql_work_claim_next` for a shared queue when writes are enabled. Keep the host's agent and session ids for retries.
- Use `reql_work_record` to save decisions, failures, and results with explicit agent and session ids.
- Use `reql_compile_project` and `reql_hubs` only for authorized writes.

Read-only mode hides write tools. MCP-only clients save checkpoints with `reql_work_record` and reopen their unfinished claims with the current revision, status `open`, and a handoff reason. CLI finish handles this when closing a CLI session.
"""
    document_semantics = f"""# REQL reference: document structure

Read this reference for document compilation or changes to document parsing and links.

## Default behavior

Project compilation is deterministic. REQL parses supported code. Enable required document formats with `compile.ingest_documents` and `compile.documents` in `reql.conf`. Use `config show` to check active formats. Document ingestion creates source fragments. PDF text extraction requires the optional PDF dependency.

Compile the project:

```bash
{command_name} project compile
```

Each document fragment links to its source artifact. A local document processor creates ranked terms, raw observation events, and term co-occurrence edges. It creates code links when the text names code symbols.

## Deterministic document processor

The document layer does not require a specific language or document structure. It splits Unicode text into tokens locally. It ranks useful terms and short phrases. It records `RawEvent` observations below each term. It creates `CO_OCCURS_WITH` relations for terms in the same fragment.

The processor writes:

- It writes `Concept` nodes with `extractor: document_processor`, `rank`, `term_frequency`, `fragment_count`, and `raw_event_count`.
- It writes `RawEvent` nodes with source fragments, terms, occurrence counts, ranks, line ranges, and evidence text.
- It writes `MENTIONS`, `EVIDENCED_BY`, `DERIVED_FROM`, and `CO_OCCURS_WITH` edges with source evidence.
- It writes `REFERENCES` edges from document terms to code symbols that the same compiled fragment names.

Do not add manual document import steps. Keep compilation deterministic and local.
"""
    return (
        SkillResource("agents/openai.yaml", openai_yaml),
        SkillResource("references/bootstrap.md", bootstrap),
        SkillResource("references/query.md", query),
        SkillResource("references/query-advanced.md", query_advanced),
        SkillResource("references/graph-updates.md", graph_updates),
        SkillResource("references/reports-exports.md", reports_exports),
        SkillResource("references/document-semantics.md", document_semantics),
    )


def _agent_workspace_resource(
    *,
    command_name: str,
) -> SkillResource:
    agent_workspace = f"""# REQL reference: Agent Workspace

## Assigned work

Start with `agent init --task TASK_ID --query "Owner.method other_symbol"`.
Use `agent init --next` for the ready queue. Use a distinct activity for each
worker and retain it when restarting an active session.

1. Use the brief's verified bodies and indexed line ranges. Retrieve only a
   missing symbol or read only its indicated lines. Save the complete task
   brief for the session instead of reloading it.
2. Keep to your edit scope. For overlapping files, agree on distinct changes
   before edits. Read a prerequisite's current status with `agent show ID`.
   Work on independent files while completion waits for dependencies.
3. Review your scoped diff once after editing. Run the supplied finish command
   for saved checks and wait for it. Otherwise run focused checks before finish.
   Give the outcome and any blocker in the summary.

```bash
{command_name} query_context --task TASK_ID --follow-up --query "missing_symbol" --code --brief
{command_name} agent task update --id TASK_ID --why "Contract ready" --next "Check consumers"
{command_name} agent finish "Consumer checks passed" --task TASK_ID
```

Publish a shared contract once in the producer's update or a linked decision.
Refer to the saved decision if the same contract is needed elsewhere.
For incomplete fields or revision history, use `agent show ID --json`.

## Planning and changes in direction

Start a write session with `agent init --name NAME`. Put acceptance criteria
in the task or a linked constraint. Give workers their file scope and exact
owner names. Reuse keys for retries. Use `--parent` to group work,
`--depends-on` for prerequisites, and `--workstream` to select a queue lane.

```bash
{command_name} agent task create "Update serializer" --key serializer --file src/serializer.py --depends-on DECISION_ID --next "Check legacy input"
{command_name} agent record decision "Preserve field order" --key field-order --why "Consumers depend on it"
{command_name} agent record failure "Cache parsed payloads" --why "Absent field test failed" --parent TASK_ID --file src/serializer.py
```

Use `agent task update` for meaningful progress or a blocker. Use `--next=`
to clear the next step. Update a shared record by ID and observed revision.
Use `--supersedes` with a reason when replacing a decision. Use `agent note`
for private scratch. Keep source dumps and test logs in their original files.

## Handoff and recovery

Plain `agent finish SUMMARY` saves a checkpoint and returns unfinished claims
to the queue. Blocked work keeps its status. If close is interrupted after the
checkpoint commits, repeat the same finish from the same activity.

```bash
{command_name} agent task update --id TASK_ID --status open --why "Agreed handoff"
{command_name} project context --task WORK_ID
```

Release a live claim after the current owner stops editing. Use
`project context --task WORK_ID` for current task or checkpoint evidence and
`project overview` to locate unknown work. Shared reads can run before creating
a private session. Resume an active claim with the same agent and activity.
Use `agent dashboard`, `agent status` and `agent --help` for administration.
See `reports-exports.md` for MCP.
"""
    return SkillResource("references/agent-workspace.md", agent_workspace)


def instruction_section(
    platform_name: str,
    *,
    project: bool,
    command_name: str,
    command_path: Path,
    fallback_command: str,
    supported_clients: str,
    section_start: str,
    section_end: str,
) -> str:
    body = _bullets(
        PROJECT_SKILL_SOURCE.rule_points,
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )
    return f"""{section_start}
## REQL

{body}
{section_end}
"""


def cursor_rule(*, command_name: str, command_path: Path, fallback_command: str, section_start: str, section_end: str) -> str:
    body = _cursor_body(command_name=command_name, command_path=command_path, fallback_command=fallback_command)
    return f"""---
description: Use REQL to find source context and save shared work.
alwaysApply: true
---

{section_start}
# REQL

{body}
{section_end}
"""


def vscode_copilot_rule(*, command_name: str, command_path: Path, fallback_command: str, section_start: str, section_end: str) -> str:
    body = shared_rule_body(
        "GitHub Copilot CLI and VS Code Copilot Chat",
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        section_start=section_start,
        section_end=section_end,
    )
    return f"""---
applyTo: "**"
---

{body}
"""


def markdown_rule(
    client_name: str,
    *,
    command_name: str,
    command_path: Path,
    fallback_command: str,
    section_start: str,
    section_end: str,
) -> str:
    return shared_rule_body(
        client_name,
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
        section_start=section_start,
        section_end=section_end,
    )


def shared_rule_body(
    client_name: str,
    *,
    command_name: str,
    command_path: Path,
    fallback_command: str,
    section_start: str,
    section_end: str,
) -> str:
    body = _bullets(
        PROJECT_SKILL_SOURCE.rule_points,
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )
    return f"""{section_start}
# REQL

{body}
{section_end}
"""


def _scope(project: bool) -> str:
    return "project-local" if project else "user-level"


def _command_usage(*, command_name: str, command_path: Path, fallback_command: str) -> str:
    return (
        f"Use `{command_name}` for REQL commands. If it is not on `PATH`, use the installed launcher at "
        f"`{command_path}`. If that path is unavailable, use `{fallback_command}`.\n\n"
        "In PowerShell, use `&` before a quoted executable path. If both saved paths are obsolete, use Python from the active REQL environment. "
        "Run that Python with `-m memory.cli` or an absolute checkout `cli.py` path. "
        "Keep the working directory at the project root."
    )


def _numbered(items: tuple[str, ...], **values: object) -> str:
    return "\n".join(f"{index}. {item.format(**values)}" for index, item in enumerate(items, start=1))


def _bullets(items: tuple[str, ...], **values: object) -> str:
    return "\n".join(f"- {item.format(**values)}" for item in items)


def _cursor_body(*, command_name: str, command_path: Path, fallback_command: str) -> str:
    return _bullets(
        PROJECT_SKILL_SOURCE.rule_points,
        command_name=command_name,
        command_path=command_path,
        fallback_command=fallback_command,
    )
