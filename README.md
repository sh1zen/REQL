# REQL

**Searchable code intelligence and shared working memory for coding agents.**

REQL builds a local graph of a project's code, tests, and documentation.
Search by feature or symbol to find the implementation, see the files it
touches, and identify tests to inspect. When work moves to another agent or
session, pass along the decisions, failed approaches, and verification that
would otherwise have to be rediscovered.

**In measured Codex runs, two single-agent tasks used 51% and 78% fewer total
tokens, and a two-agent task used 38% fewer across both workers.**
[Results and measurement details](#measured-token-savings)

[Quick start](#quick-start) | [What you get](#what-you-get) |
[Agent coordination](#coordinate-agents-and-resume-work) |
[Support REQL](#support-reql)

## Quick start

1. With Python 3.10+ and [uv](https://docs.astral.sh/uv/getting-started/installation/),
   install the CLI with `uv tool install reql`.
2. From the root of this checkout, run `reql project compile` to build the index.
3. Run `reql query_context --query "MemoryGraph locate" --code`. The result
   points to `MemoryGraph.locate` and its line range in `src/api/memory_graph.py`.

Use the same workflow in your own project with one of its features or symbols.
If `reql` is missing from your PATH, run `uv tool update-shell` and reopen your
terminal. The quick start works with the published package. To try features
from this checkout, use the [development setup](CONTRIBUTING.md#development-setup).
Dependencies resolve to releases compatible with your Python version. See
[Tree-sitter compatibility](docs/PIPELINE.md#tree-sitter-dependencies).

For Codex, Claude Code, Cursor, and other supported assistants, REQL can install
project instructions that guide agents through discovery, verification, and
handoffs. [Assistant setup](docs/CLI.md#assistant-installs)

Use `reql.conf` for project options such as scan paths and document formats.
`reql config init` creates a short starter file. The bundled YAML
files live in `src/config/`. `user.yaml` defines project defaults.
`core.yaml` defines lexical scoring, extraction, retrieval, graph query,
analysis and storage policy.
Set setup headings and workflow naming conventions in `reql.conf` when your
project uses them. Use `testing.discovery.directories` for custom test
directories and `testing.suggestions.evidence_order` to rank test evidence.
[Configuration guide](docs/CONFIGURATION.md)

## What you get

| If you need toâ€¦ | REQL gives youâ€¦ |
| --- | --- |
| Find where a change belongs | A focused working set with symbol names, file paths, line ranges, and current source excerpts when available. |
| See what an interface change could affect | Views of indexed callers, imports, overrides, public entry points, serialization paths, and documentation references. |
| Choose tests for a refactor | Test associations by file or symbol, with static links, imported per-test coverage, and naming-based candidates identified separately. |
| Get oriented in an unfamiliar codebase | A repository overview with inferred capabilities, architectural layers, workflows, and source-backed starting points for a change. |
| Review a change between runs | Indexed revisions and file and symbol changes, plus a way to check whether the current files still match the index. |
| Explore code structure or look for cleanup opportunities | Static findings, highly connected components, community analysis, and an interactive graph. |

Retrieval explores eligible relationships within the requested depth and can verify returned excerpts against
the indexed file hash. It reports the source revision and freshness, so you
can tell when to refresh after an edit. Static relationships and candidate
tests give you concrete leads to verify in source.

[Search and dependency views](docs/CLI.md#retrieval-commands) |
[Test evidence](docs/CLI.md#test-evidence) |
[Test configuration](docs/CONFIGURATION.md#test-discovery-and-evidence) |
[Repository explanation](docs/CLI.md#repository-explanation) |
[Revisions](docs/CLI.md#project-compilation)

## A real change in this repository

Suppose you want to change how REQL resolves file paths. The quick-start query
returns the Python method in `src/api/memory_graph.py` and also surfaces the
CLI handler in `src/memory/cli.py`. It shows a re-export in
`src/api/__init__.py` and offers test candidates to inspect.

That gives you a starting point for preserving the public lookup behavior while
changing the implementation. If a second agent will finish the work, record
the intended lookup order, the tests already checked, and the next action.
The next session can retrieve that handoff together with the current source
context. [Working with a task brief](docs/COORDINATION.md#retrieval-and-overview)

## Coordinate agents and resume work

**Agent Workspace carries the engineering work forward across sessions.**
A task can link to affected files, decisions, dependencies, and verification
results. A new session can recover that task alongside the current code.
Work context follows the full causal scope by default. Shared work keeps its
prior revisions.
Workspace and coordination operations wait for native store locks.
Use `reql agent wait ID` to wait for a prerequisite after independent work.
Use `reql agent verify --plan PLAN.json` to run an ordered verification plan
and reuse passing results for matching revisions and inputs.
Add `--source` to `reql agent init --json` when the result needs verified
source bodies.

- **Split work with dependencies.** Queue tasks and let agents claim ready work.
  Track which prerequisites are still open, where assigned files overlap, and
  which tasks have blockers. A coordinator can also assign work to agents
  dispatched by the host.
- **Keep the useful parts of an investigation.** Record why a decision was made,
  what a failed approach broke, or which checks already ran. Recover that history
  by task, file, or query when related work starts again.
- **Hand off unfinished work.** Save a checkpoint with evidence paths, progress,
  and next actions. Unfinished claims can return to the queue with their history
  available to the next worker.
- **Verify before closing.** Run the project's checks and summarize their outcome
  in the task handoff. Include any failure and the next action when handing off
  unfinished work.

[Task queues and handoffs](docs/COORDINATION.md#planning-and-continuation) |
[Verification handoff](docs/COORDINATION.md#verification-handoff) |
[Workspace settings](docs/CONFIGURATION.md#storage-and-workspace-policy)

## Reports and visualizations

Use these views when you need to explain the system, review a change, or share
a map with teammates.

| Output | What you can inspect |
| --- | --- |
| **Interactive graph** | Search for a symbol, expand its neighbors, filter by community, and inspect node details. The graph is also available as JSON for other tools. |
| **Project flow** | Follow statically observed routes, commands, or public API entry points through their components and outcomes. Export interactive HTML or Mermaid. |
| **Project reports** | Review indexed languages and artifacts, parser issues, cleanup candidates, graph changes, and cache status in Markdown. |
| **Repository explanation** | Browse inferred capabilities and workflows with links to the source, or use the structured JSON view in an integration. |

[Project flow export](docs/CLI.md#project-pipeline-export) |
[Reports and graph export](docs/CLI.md#reporting-and-graph-export)

Project reports use the same scan and cache policy as compilation and surface
scan failures before writing report files. They compute community and hub
summaries in memory and leave the compiled graph unchanged. Interactive HTML
exports include their viewer and work offline. Use `reporting` in `reql.conf`
to set the report directory and adjust list and graph detail limits.
[Report settings](docs/CONFIGURATION.md#analysis-and-report-policies)

## Measured token savings

Focused queries keep the code context small. Shared tasks and checkpoints let
agents reuse work across sessions, including what was already investigated or
verified. The comparisons below used the same frozen source trees and
acceptance checks with and without REQL. Both runs in each comparison passed.

| Codex task | Without REQL | With REQL | Total tokens saved |
| --- | ---: | ---: | ---: |
| Import per-test coverage with Windows-qualified test IDs | 1,133,481 | 555,869 | **51%** |
| Index JavaScript and TypeScript test modifiers | 2,294,300 | 506,771 | **78%** |
| Coordinate Python, CLI, and MCP changes in one checkout | 1,625,700 | 1,013,598 | **38% across both workers** |

Each row is one paired comparison. Total tokens count input, including cached
input, plus output. The coordination row sums both workers. Results vary by
task and run. The local benchmark report is in `benchmark/BENCHMARKS.md`.

## Local by design

Index and query an ordinary folder, even without Git. REQL parses code locally
with Tree-sitter and uses a local store, with no model calls, API keys, hosted
service, or external graph database required for compilation and retrieval.
Recompiling reuses unchanged files, so you can refresh the index after edits.
Readers can share a local store. Writes require exclusive access. Use
`reql storage locks` to check access when a store is busy.

- **Mixed-language projects.** Index Python, JavaScript, TypeScript, Go, Rust,
  Java, C/C++, and other supported languages in one project, alongside Markdown
  and plain text. Optional PDF support extracts text.
  [Languages](docs/PIPELINE.md#language-support) | [Documents](docs/PIPELINE.md#supported-inputs)
- **Developer tools.** Use REQL from the terminal, embed retrieval in a Python
  tool, or connect an MCP client. MCP has a read-only mode for clients that
  only need to retrieve context.
  [Python API](docs/API.md) | [MCP server](docs/MCP.md)

## Documentation

REQL uses `YY.MM.DD` release versions defined in [src/config.py](src/config.py).

| Guide | Use it to |
| --- | --- |
| [CLI reference](docs/CLI.md) | Compile, search, inspect tests, and export reports. |
| [Agent coordination](docs/COORDINATION.md) | Set up work queues, manage handoffs, and attach checks. |
| [Configuration](docs/CONFIGURATION.md) | Choose what to index and tune project settings. |
| [Architecture](docs/ARCHITECTURE.md) and [compilation pipeline](docs/PIPELINE.md) | Understand source analysis, retrieval, and storage. |
| [Graph query language](docs/REQL.md) | Query graph entities and relationships directly. |

## Support REQL

If REQL saves your team time on a real codebase, [star the project on GitHub](https://github.com/sh1zen/reql)
or share the workflow that benefited. Examples from new languages and agent
setups help prioritize what to improve.

Interested in sponsoring ongoing maintenance or a specific feature? Start a
conversation in [GitHub Discussions](https://github.com/sh1zen/reql/discussions).

[Report an issue](https://github.com/sh1zen/reql/issues) | [Contribute](CONTRIBUTING.md) |
[Changelog](CHANGELOG.md) | [License](LICENSE)
