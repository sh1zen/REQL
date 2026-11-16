# Agent token benchmarks

Run the same seven coding tasks with and without the current REQL checkout.
Both arms start from the same frozen source tree and use the same Codex model,
reasoning effort, edit scope, and independent acceptance checks.

| Case | Task |
| --- | --- |
| `locate` | Preserve exact-path precedence in `MemoryGraph.locate` |
| `mpmc` | Add capacity and closed-state queries to Crossync MPMC |
| `coordination` | Coordinate Python, CLI, and MCP changes across two workers in one checkout |
| `coverage` | Import Coverage.py contexts with Windows-qualified pytest node IDs |
| `ecmascript` | Index JavaScript and TypeScript tests that use `concurrent` and `skip` |
| `barrier` | Add nonblocking arrival to Crossync Barrier |
| `focused-classifier` | Recognize `__specs__` in the test-path classifier |

## Run

Use the development Python environment, Git, and an authenticated Codex CLI
that supports `codex exec --json`. Rust cases also need Cargo and the frozen
dependencies in its offline cache.

The manifest pins the original source snapshots and acceptance files in the
benchmark archive. The runner defaults to the existing archive under `.tmp`.
On another checkout, copy that archive and pass `--archive-root PATH`. Keep
the manifest's relative paths, Git objects, and acceptance files. Source
snapshots are read from the pinned commit, so archived working-tree edits
have no effect on a new trial.

```bash
python -m benchmarks.token --list
python -m benchmarks.token
python -m benchmarks.token --case coordination --prepare-only
python -m benchmarks.token --case coordination --mode with-reql
```

Omit `--mode` to run both arms. Use `--mode without-reql` for just the control,
or `--mode with-reql` to collect a new REQL run against an existing control.
Use `--case` more than once to select a subset. `--prepare-only` checks fixture
setup without starting an agent. `--codex` selects the runner executable.
`--model` and `--effort` apply to both arms.

## Read the results

The runner writes `results.json` to a fresh directory under `.tmp/token-bench`
by default. Each arm saves the Codex traces, independent acceptance log, and
verified patch. Both modes require a handoff. The REQL arm uses shared work
records and a checkpoint. The control writes a plain handoff file. The
coordination case runs both workers concurrently inside the same checkout
and counts both workers' tokens.

The report shows input, cached input, output, total tokens, and input served
without cache plus output. Cached input is part of total input. The report
compares only two arms that passed the acceptance checks and handoff.
A single run per arm is one sample.

