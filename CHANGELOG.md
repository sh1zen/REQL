# Changelog

## [Unreleased]

- Support Tree-sitter through 0.27 and update grammar, PDF, and development dependencies. Select language pack and coverage releases compatible with Python 3.10.

- Use one `YY.MM.DD` release version for packaging, parsers, and ranking. Define it in `src/config.py`.
- Package internal policy in `core.yaml`. Use `user.yaml` as the source of defaults and override permissions for project options.
- Remove separate parser and chunking version overrides, duplicate cache fields, and the file-based defaults API. Compilation refreshes older cache snapshots when their fingerprints differ.
- Remove the `cache.fingerprint_strategy` setting. Remove it from existing project configuration and overrides. File fingerprints use SHA-256.
- Run `project compile` to refresh the graph on demand. Remove filesystem watch mode from the CLI, launcher, Python API and MCP.
- Use task IDs for CLI updates, claims and completion. REQL checks task revisions internally.
- Task finish drops the separate review step. Use `project diff` to inspect recorded file changes.
- Require release before a different session claims an in-progress task. Only the claiming session can finish it.
- Expand generated guidance for source briefs, parallel work and checkpoint recovery. Preserve claim receipts when source lookup fails.
- Remove the unused `--type`, `--status` and `--metadata` options from `agent search`.

- Remove REQL test runners and saved task checks. Run project checks directly, refresh the graph after direct edits with `project compile`, and report results in `agent finish`.
- Retire migration code for the old watcher guide.

- Remove `agent --no-progress`. Agent commands report progress on stderr.
- Remove the explicit verification and edit plan options from `agent finish`. Use `reql edit --plan` for edits against the latest compiled project revision.
- Remove the CLI `--max-file-size-mb` override. Set `scan.max_file_size_mb` in project configuration.
- Remove `agent export` and the Python workspace export method.

- Remove `agent batch` and its ordered record API. Use `agent task create` and `agent task update` for tasks and `agent record` for other shared records.

- Remove node inspection from the CLI, Python API and MCP. Query context read plans retain source paths and line ranges.

- Remove transcript compaction from generated assistant integrations, along with migration of retired per-tool hooks and batch guides.

- Use `stats` for graph counts, compilation and source freshness, cache status, and imported test evidence. It replaces `project status`, `cache status`, and `tests status`.
- Remove the `project workspace` CLI group.
- Remove the `query_graph` CLI command.
- Use `agent record failure --why REASON` in place of `agent reject`. Select `query_explore` views with `--view VIEW`. Run `project pipeline` without a format flag for HTML.

- Rank exact identifiers and resolved relationships higher, and diversify results across files. Remove the `retrieval` section from older configuration files and overrides.
- Resolve more Python and JavaScript calls across files, including supported imports, re-exports and inherited members. Report unresolved calls with their source locations.
- Find tests for a symbol or file, distinguish static associations from candidates, and import observed test evidence from coverage.py JSON.
- Compare current project files with the latest REQL revision and verify source locally without version-control metadata.
- Recover relevant failed approaches through the CLI, Python API and MCP, with source context that references matching engineering records.
- Invalidate cached parser results when installed Tree-sitter grammar versions change. Entries with the former project-wide fingerprint rebuild on the next compile.
- Publish tagged GitHub releases with notes from this changelog and attach a bundle containing the checked wheel and source distribution.

## [v0.2.0] - 2026-11-17

- Recover intact C definitions from partially parsed files and report parse diagnostics.
- Reduce peak memory when writing graph checkpoints while retaining compatibility with existing stores.
- Document isolated CLI installation with `uv` and add coverage reports to CI.

## [v0.1.8] - 2026-11-16

- Return bounded source context with a shared, versioned response across Python, CLI and MCP.
- Keep incomplete scans from publishing revisions or treating files as deleted.
- Recover project direction and active work from Agent Workspace through project context and overview.

## [v0.1.6] - 2026-09-22

- Inspect inferred repository capabilities and processing flows with `reql project explain` and `reql project pipeline`.
- Tune how many project revisions and agent sessions REQL retains.
