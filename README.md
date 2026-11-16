# REQL

REQL is a local repository context and working-memory layer for coding agents
and developer tools. It compiles source files and supported documents into a
property graph, then returns bounded context about code, tests, dependencies,
findings and provenance.

REQL runs locally on ordinary folders without version control, model calls,
accounts, hosted services or an external graph database. The compiled graph
holds repository facts. Agent Workspace holds engineering intent, decisions
and handoffs.

REQL can reduce token consumption by up to 50% in individual agent runs by
retrieving focused repository context. Shared decisions, verified findings and
checkpoints significantly improve coordination across agents and sessions.
Reusing that work reduces repeated discovery and further lowers overall token
consumption. See the [agent token benchmarks](benchmarks/README.md).

## Quick Start

REQL requires Python 3.10 or newer. Install the CLI in an isolated environment
with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
uv tool install reql
uv tool update-shell
```

Restart your shell after `uv tool update-shell` if `reql` is not on `PATH`.
No virtual-environment activation is needed. Upgrade with `uv tool upgrade reql`.
For optional PDF support, install `"reql[pdf]"` instead.

To install with pip, create a virtual environment first. On Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install reql
```

On Windows PowerShell:

```powershell
py -m venv .venv
.venv/Scripts/python.exe -m pip install reql
```

With pip, use the `reql` executable in that environment for the commands below.
See [development setup](CONTRIBUTING.md#development-setup) for a checkout.

From the repository root, install the assistant instructions, build the graph,
and retrieve a working set:

```bash
reql install codex
reql project compile
reql query_context --query "payment service" --code --brief
```

Replace `codex` with another [supported agent platform](docs/CLI.md#assistant-installs)
or run `reql install` to detect one. Use `reql locate src/service.py` for a known
path and [`query_context`](docs/CLI.md#retrieval-commands) for unknown code.
Use `reql agent init --next` from a distinct activity for each queue worker.
Track progress with `reql agent task update` and save decisions with
`reql agent record`. Plain `reql agent finish SUMMARY` saves a checkpoint and
returns unfinished claims to the queue. Use `reql project context --task ID`
to recover a task or checkpoint. See the
[parallel work and handoff guide](docs/COORDINATION.md#planning-and-continuation).

After changes, run the project's tests and refresh with `reql project compile`.
Use [`reql stats`](docs/CLI.md#project-and-graph-statistics) to check source freshness
and `reql project diff` to review changes in the latest REQL revision. For
assigned tasks with saved checks, apply edits before the
[finish command](docs/COORDINATION.md#verification-handoff), which runs those
checks and refreshes the graph. Agent commands send progress to stderr.
To apply a line-edit plan, use `reql edit --plan -` with
[verified excerpts](docs/CLI.md#source-edits).

## Features

- Incremental code and document compilation with hashes, source spans and provenance.
- Bounded lexical and graph retrieval through Python, the CLI and MCP. The CLI
  offers focused dependency views with `query_explore`.
- Static and observed test evidence with candidate matches labeled separately.
- Content-addressed project revisions and live source comparison.
- Static-analysis findings, structural code links, communities and hub analysis.
- Revision-checked shared work, private session scratch and recoverable checkpoints.
- Local block storage with transactions, locking, compaction and retention.
- Markdown reports, JSON and HTML graph export, and project pipeline views.

See the [compilation pipeline](docs/PIPELINE.md) for supported languages,
document formats and parser requirements.

## Runtime Model

REQL is alpha software. Python owns orchestration, graph operations and storage.
Tree-sitter provides native parsing.

```text
project files -> scan and compile -> canonical graph -> bounded source context
agent outcomes -> shared work records -> task recovery and project overview
```

The graph defaults to `.reql/memory.reql`. Set project options in
[`reql.conf`](docs/CONFIGURATION.md).

## Interactive Launcher

From a development checkout:

```bash
uv run --no-sync python launcher.py
uv run --no-sync python launcher.py --storage .reql/memory.reql
```

The launcher offers a guided menu for project commands. See
[CLI entry points](docs/CLI.md#entry-points) for scripting and
[MCP setup](docs/MCP.md#start-the-server) for tool clients.

## Documentation

| Guide | Contents |
| --- | --- |
| [CLI](docs/CLI.md) | Commands, retrieval, test evidence, reports and exports |
| [Configuration](docs/CONFIGURATION.md) | Defaults, `reql.conf`, overrides and scan rules |
| [Engineering coordination](docs/COORDINATION.md) | Work records, sessions and verification handoffs |
| [MCP server](docs/MCP.md) | Transports, tools and client configuration |
| [REQL queries and graph](docs/REQL.md) | Query syntax, graph schema, communities and hubs |
| [Architecture](docs/ARCHITECTURE.md) | Components, ownership and extension points |
| [Compilation pipeline](docs/PIPELINE.md) | Stages, cache, code parsers and document ingestion |
| [Storage](docs/STORAGE.md) | Block format, transactions, locking and maintenance |

## Public API

Add `reql` to your application's Python environment, for example with
`uv add reql`. The package exports `MemoryGraph`, `BlockGraphStore` and typed
query-context models.

```python
from reql import MemoryGraph, QueryContextRequest, QueryMode, RetrievalBudget

graph = MemoryGraph.open(".reql/memory.reql")
try:
    graph.compile_project(".")
    request = QueryContextRequest(
        text="payment service",
        mode=QueryMode.INFORMATIVE,
        scopes=frozenset({"code"}),
        budget=RetrievalBudget(top_k=20, max_depth=3, max_items=20),
    )
    result = graph.query_context_result(request)
    print(result.to_dict())
finally:
    graph.close()
```

The [MemoryGraph facade](src/api/memory_graph.py) exposes compilation, retrieval,
REQL queries, reporting and analysis. Python compilation overrides
use `CompilationOptions` from `memory.artifacts`; see
[typed configuration](docs/CONFIGURATION.md#loader). Context requests return
schema-version-2 envelopes with source revision, freshness, confidence and a
nested payload; see [retrieval contracts](docs/CLI.md#retrieval-commands).

Use `graph.find_tests_for(symbol_id=...)` to retrieve test associations,
`graph.failure_context(query, files=[...])` to recover failed approaches, and
`graph.workspace_status(path)` to compare the current files with the last REQL
revision. See [test evidence](docs/CLI.md#test-evidence) and [project source
status](docs/CLI.md#project-and-graph-statistics).

## License

MIT. See [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests and pull request
expectations. Generated stores and caches stay local.

See [CHANGELOG.md](CHANGELOG.md) for release notes and
[Release Expectations](CONTRIBUTING.md#release-expectations) for tagging and
creating a GitHub release.
