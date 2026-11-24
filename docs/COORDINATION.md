# Engineering coordination

Use `reql project context --file src/serializer.py --failures-only` to recover
active failures for a file. The same projection accepts a task ID or focused
query. A match identifies the work record, its revision and the reason it was
selected. Superseded, invalidated and resolved records are excluded from this
projection.

Failure records can include an optional `failure_evidence` object with
`source_revision`, `symbol_ids`, `test_ids` and short `observed_checks` notes.
REQL validates graph references before using them as ranking evidence. A change
to an affected file can mark a record `potentially_obsolete`. Changing the
record's status remains an explicit coordination action.

REQL has three distinct owners: the compiled graph owns repository facts,
shared work owns engineering intent and execution, and private agent stores own
scratch notes and session lifecycle. Neither work records nor checkpoints copy
source nodes. Repository-relative paths connect intent to source-backed retrieval;
the current source and tests remain authoritative.

## Work records

`.reql/agent-dashboard.reql` stores `work_record` nodes alongside the existing
roster and public messages. `memory.services.coordination.CoordinationStore` owns
their validation, transactions, reconciliation, retrieval and projections.
Agent Workspace supplies the current agent/session provenance. Construction does
not open stores or create files. Reads of absent work storage return empty context.

Kinds are goal, task, decision, constraint, failure, question, change, observation
and checkpoint. A record has stable identity, content, status, rationale, evidence
paths, optional workstream, parent, dependencies, conflicts, replacements,
importance, next action, revision and origin/latest agent/session attribution.
The `workspace` section configures the default identity, shared and private
store names, and the agent directory. The dashboard shows five rows per
section by default. Use `reql agent dashboard --limit N` to request more rows. Roster
listing and history recovery work from the complete records.

Only kind and content are required for simple work. Decisions and failures require
a rationale. Files are repository-relative evidence pointers, never inputs to
filesystem operations.

MCP exposes `reql_work_context`, `reql_work_overview` and the optional write tool
`reql_work_record`; read-only mode hides/rejects writes. Direct writes carry explicit
agent/session provenance and use the same revision semantics, without creating
private scratch. MCP-only clients save explicit checkpoints at session boundaries.

A task's claim identifies its editing agent and activity session. Latest
agent/session attribution identifies the writer of the current revision.
A progress update from a coordinator preserves the worker's claim. A managed
task marked `in_progress` without an `init` claim remains coordinator-managed.

The default identity normalizes content within kind/workstream/parent. `--key` supplies
a stable name when wording will evolve. Repeating an identical request is a
no-op. Task commands use the task ID and read the current revision internally.
Updates through `agent record` require `--id` and the observed `--revision`.
Stale writes fail with reconciliation guidance. The block-store writer lease and transaction
make validation, revision checks, replacement and updates atomic across processes.

Statuses are active, open, in_progress, blocked, done, resolved, superseded,
invalidated and abandoned. Blocked/obsolete transitions and reopening require
a rationale. `--supersedes` atomically retires the old record with replacement id
and reason. Each revision preserves the previous content, assumptions, status,
and provenance. Use `agent show ID --json` to inspect prior snapshots.

Relations are stored once, as ids on their owning record:

- `parent` groups outcomes under tasks/goals without a second plan document.
- `depends_on` links prerequisites and assumptions across workstreams.
- `contradicts` exposes a conflict from either endpoint.
- `supersedes` records a change of direction and its cause.
- `summarizes` links a checkpoint to its evidence without treating that work as
  a prerequisite for ending the session.

Records accept full text, repository-relative evidence paths, and all relation
targets. Each target must exist. REQL rejects self links and cycles in hierarchy,
dependencies, and replacement. Repeat the CLI flags to add multiple targets.
Pass empty lists in Python to clear reconciled links. Review dependent tasks when
their assumptions are replaced.

## Planning and continuation

The plan is the current connected work, not a manually synchronized TODO file.
Execution is projected on read: an open task is ready when its task/goal/question
prerequisites are complete; waiting prerequisites block it; obsolete dependencies
and explicit conflicts require review. Conflicting dependency assumptions also
require review. Completing a task with those unresolved conditions is rejected.
Both `done` and `resolved` task states run these completion checks.
REQL does not infer human acceptance, implementation correctness or task completion
from a commit or a passing test. The agent records the observed result.
Record observable acceptance criteria for assigned tasks, including required
public signatures and boundary behavior. Link reusable criteria as `constraint`
records with `--depends-on`, and run checks that exercise those criteria.
Task recovery ranks direct constraints ahead of ordinary related history.

`agent finish SUMMARY` saves a shared checkpoint with the outcome, associated
evidence paths, work links, and next actions from unfinished tasks.
It returns unfinished in-progress claims to the queue without discarding
progress, decisions, or blockers. An assigned finish,
`agent finish SUMMARY --task ID`, also completes the selected claim and clears
its next action. The shared changes commit together before private session
close. Blocked and coordinator-managed tasks keep their status. Rejected
completion leaves the session and claims open. If close is interrupted after
the shared commit, repeat the same finish from the same activity.

The agent runs project checks before recording a successful completion. An assigned
brief can point to a verified setup section when setup is needed. Apply changes
before finishing. Use [the standalone edit command](CLI.md#source-edits) for
revision-checked line edits, or refresh the graph after direct edits.

Finish returns the saved `checkpoint` receipt (`id`, `revision`, `status`)
in Python and CLI JSON, and prints its id and revision in text. Resume on
request with `project context --task CHECKPOINT_ID` to read the current status
and next actions of linked work. Use `project overview` to find a checkpoint
in an indexed project, or use `project context --file PATH` or `--query TERMS`
if you know a file or phrase. Shared records survive private session cleanup.
The receipt comes directly from the checkpoint write without opening the canonical graph.

For managed parallel work, one coordinator can own shared records while workers
use its graph-backed briefs and return observed files, checks and blockers.
Workers need no private sessions for this mode. The coordinator sets assigned
tasks to `in_progress`, reviews actual changes, runs integration checks, then
accepts verified outcomes in dependency order with `agent task update`. The coordinator
authors each update and attributes the worker's evidence in its rationale. Each
write checks the observed revision, prerequisites, conflicts and obsolete dependencies.
The host owns worker dispatch and source-file ownership. REQL revision checks
apply to work records; source changes require normal review and verification.

For a shared queue, the host starts agents with distinct activities. Each calls
`agent init --next`, optionally filtered by `--workstream NAME`. The same writer
transaction selects and claims ready work. The queue prefers tasks without file
overlaps, then higher importance and earlier creation. Other ready tasks can
still be claimed and show which active tasks name the same files. The same
session can retry to recover its claim. See `project overview` for waiting work.

For independently assigned work, `agent init --task ID` claims the agreed
task and returns its revision, causal context, and overlapping files
with all concurrent task IDs for each file. Agree on file ownership before
editing an overlap. Add `--query "literal terms"` to retrieve source in the same response. If that
lookup fails after the claim, use the returned work context and source retry.

Repeat a claim with its original agent and activity to recover it without
advancing its revision. A process restart alone leaves the claim in place.
After normal finish, a new activity can claim any task returned to the queue.
For a direct transfer or stale owner, confirm that editing has stopped and
release the claim with
`agent task update --id ID --status open --why "handoff reason"`.
Reuse a supplied graph-backed working set; do not repeat discovery or copy the
same result into observations, public notes and checkpoints. Workers run focused
checks; the coordinator runs integration checks after their changes join.
The coordinator also refreshes the source graph once after parallel integration.
When the documented full suite includes worker tests, run it once after integration.
Repeat focused checks for failures, changed dependencies or coverage outside that suite.
Keep any checks required by repository instructions. A supplied passing baseline
is reusable only when its tested source and dependencies remain unchanged.
Assigned-session setup returns scoped context. Use exact symbols for the
worker's edit files in its query. A claimed worker waiting for a dependency can
read evidence and work independently within its agreed scope. The claim receipt
identifies which prerequisite must complete before finish.
Use `query_context --lock-timeout 60` when workers share the graph. If the
wait expires, use `reql storage locks`. Continue independent work and retry
after the active command releases the graph.

Publish a shared contract once on the producer task or in a linked decision.
Use `agent show ID` to read a prerequisite update. Complete independent work,
then use `agent wait ID` if the prerequisite is still pending. Review the
worker's scoped diff and reuse verified excerpts from the brief. Request JSON
for missing fields or revision history.

Use `project context
--task ID` when that handoff lacks required evidence. Focused text retains complete
content, rationale, next actions and evidence paths within its budget; inspect a marked incomplete record
with `agent show ID --json`. That command also includes revision history.
Do not reload every historical record for a complete handoff.

For linked work, create the prerequisite records first and use their returned
ids in `--parent` and `--depends-on`. Use `agent task create CONTENT` for tasks
and `agent record KIND CONTENT` for other work records. Plain write receipts
show the id, kind, status and revision. Use `--json` for the full saved record
and history. Update tasks using their IDs and changed fields.

```bash
reql agent task update --id TASK_ID --why "Unit checks passed" --next "Check downstream consumers"
reql agent task update --id TASK_ID --status blocked --why REASON --next "Resolve the linked question"
reql agent record --id QUESTION_ID --revision N --status resolved --why ANSWER --next=
```

To replace text, supply positional content to `agent task update` or positional
kind and content to `agent record`. The write transaction retains omitted text
and checks the revision captured for the task or supplied for the record.
After a stale-revision error, read the affected record before retrying.
Plain `agent show ID` returns current outcome text within the budget and marks
any truncation.

Private stores contain sessions, `private_note` and `external_note` records.
Durable work belongs in shared records, including `observation` and `failure`.
Workspace lifecycle operations wait for the project lock. Completed-store
cleanup removes private graph data when it can acquire the private writer lock.
It keeps the empty native lock files so future sessions use the same lock identity.
Use `agent task done` to publish completion and `agent task list` to list owned
tasks. `agent reset` discards private sessions and notes. Shared engineering
records remain available.

## Retrieval and overview

`query_context` performs source retrieval using lexical/identifier seeds,
bounded code-graph expansion and chain-aware ranking. Its shared Python/CLI/MCP
service also retrieves up to eight relevant engineering records. The schema-v2
payload includes `engineering_context` with a separate content revision; repository
graph revision and confidence retain their existing source-only meaning.
Code queries use their selected working-set files as work evidence anchors.
Add `--task ID` to a CLI source query (or set `task_id` in Python or MCP) when
working from an assigned task or checkpoint. This recovers the named record and
its causal work while running the normal source query. Text presents the work
first, including next actions, current revisions and dependency conflicts.
This read does not claim the task or update its revision.
The brief marks when it has shown the saved task and every direct prerequisite
in full. Use the omission markers for any work evidence you still need. Failure
freshness appears with the work record when it fits there. A budget cut can
surface a short failure explanation in a separate section.
For a ready or in-progress task, brief source context can also provide one
assigned test file as a complete, hash-verified body.
When a brief has no owner body, REQL can check definitions in the task's named
production files. A clear match can supply the complete verified body.
Ties retain file references.

An active prerequisite decision can provide verified declarations for calls it
names in its evidence files and for return types it explicitly mentions. The
brief omits ambiguous, stale or already covered declarations. These excerpts are source evidence
for the decision and carry no edit assignment.

Save edit ranges in a task's next action as inline code using
`src/module.py#L12-L19@FILE_SHA256`. Replace `FILE_SHA256` with the full hash
from the verified source response and list the file on the task. A brief query
for that task can embed all verified ranges. A pinned range that fully covers
an owner excerpt displays the shared body once. REQL reuses source snapshots
and skips ranges already shown. When a pin ends at a Markdown heading, REQL
can include its section or nearby fenced example.
Use [`reql edit --plan -`](CLI.md#source-edits) with exact lines from a
verified excerpt.
When verified pins cover a substantial part of a production file, the brief
can show the complete file. If the task names the type of a displayed method,
the brief can include the leading type declaration.
For a verified code pin, the brief can complete its indexed routine with a
complete tail and show local types referenced in the pin or sibling methods named
in the task. These excerpts share the pin's source snapshot.
If the task names one Markdown file and a qualified symbol, the brief can
include a unique verified passage referring to that symbol with its example.
If a passage is unavailable, the brief can show the assigned document's
verified section ranges. Read the relevant section.
Once it has verified source for every declared task file, the brief keeps
its file list focused on those paths and omits relations between unrelated
files. Structured output retains ranked files and impact.
When brief text includes an assigned test, it
retains static or observed relationships and omits lexical test hints.
The structured result still contains the original test candidates.
REQL labels ranges stale when
the saved hash no longer matches. Read the affected file and record a new range
when its content changes.
Brief text shows shortened range references when the exact verified body is
already on screen. The stored next action still has the full hashes.
Verified source shown for an assigned task reports its original encoding and
line endings. The brief can include a verified Markdown section linked from
that source.
Preserve both when applying the edit.

### Verification handoff

Run the project's documented checks using its own tools. Use
`reql project compile` after direct edits to refresh the graph.
To run a verification plan, use `reql agent verify --plan PLAN.json` from the
indexed project root. Add at least one focused check. Add broad checks when the
task calls for them. Specify each command as an argument list.

```json
{
  "focused": [
    {"command": ["python", "-m", "unittest", "tests.test_serializer"],
     "inputs": ["serializer.py", "tests/test_serializer.py", "pyproject.toml"]}
  ],
  "broad": [
    {"command": ["python", "-m", "unittest", "discover", "-s", "tests"]}
  ]
}
```

REQL stops at the first failed check. It can reuse a pass when the compiled
revision, command, executable, process environment and bytes of all declared
inputs match. List every project-local file the check reads, including test
configuration and dependency lockfiles. Omit `inputs` for checks with dynamic
or external dependencies. These checks run each time. Use `--json` to see
which checks ran and which reused a receipt. Commands run with no interactive
input. Receipts store fingerprints beside the graph and contain no check
output.

Record the commands, their results and any remaining failures in the handoff.
Once the work
is ready, use `reql agent finish SUMMARY --task WORK_ID` from the same workspace
to record the outcome and complete the task. For unfinished work, record the
blocker and next action on the task and save a checkpoint with plain
`reql agent finish SUMMARY`.

### Work retrieval

Brief source queries retain file and causal matches and require stronger text
evidence for unscoped work. Text-only checkpoints require all query terms and
at least three matched terms. An attached task includes its causal work records.
Brief text summarizes observations and checkpoints in unassigned source queries.
Use `project context` to recover shared work independently of source retrieval.

`project context` reads work independently of the canonical graph. Its anchors
are normalized text with inverse-frequency weighting, evidence path overlap,
explicit task id or workstream. An explicit `--task` selects causal scope rather
than adding a lexical boost: unrelated matches cannot crowd out its prerequisites,
even if many share its file or query terms. Importance, decision/constraint/failure kinds,
unresolved status and day-level recency adjust ranking. Current file-scoped work
outranks completed summaries for that file, even when a summary has stronger text
overlap. An explicit task skips lexical processing of backlog prose. Traversal
follows all reachable prerequisites, parent goals, conflicts, and replacements.
Direct anchors also add
their dependent work and child tasks. Reached prerequisites/parents do not fan
out into sibling consumers; query that shared contract directly for its impact.
This keeps assigned-task recovery scoped without changing readiness checks. Reverse
relations are indexed once per read. Prerequisites and conflicts rank ahead of
consumers and historical summaries. The text and JSON responses include every
matching record by default. Set `--limit N` to return only the first N records
after ranking. `total_matches` counts the complete result and `omitted` counts
records outside the requested limit. Scores and reasons explain inclusion. With
`--task`, the requested record or its current replacement comes first in text.
Text combines repeated relation targets with their roles, such as
`waiting+depends: ID`. Exactly equal fields can refer to an earlier field in the
same response by row or section. Each record keeps its ID and revision visible.
Repeated attribution can use `(owner1=agent:...)` on the first emitted row and
`(owner1)` on later rows in that response. Keys are local to the response and
retain exact agent identity; commands and structured data always use the full id.

Default retrieval excludes superseded/invalidated/abandoned records. Current
replacements expose the old ids and replacement rationale. Recovery follows
replacement chains even when the current decision shares no query terms or files.
An obsolete requested id resolves to its current replacement and exposes work
still dependent on the requested assumption. Task dependencies are never rewired
automatically: obsolete assumptions still require review and block completion.
`--history` admits
obsolete records for audit. Claims are scoped engineering evidence, not verified
source facts. Unrelated recency alone cannot anchor a focused query. Retrieval
requires no embeddings, provider calls or additional dependencies; it cannot
discover unrecorded intent or paraphrases with no lexical/structural connection.

`project overview` combines a compact source-backed architecture with goals,
current decisions/constraints, workstreams, completed and active outcomes,
blockers/questions, failures, checkpoints and ready next steps. Sections contain
all matching records by default. Set `--limit N` to limit rows per section.
Section counts show omissions. `--details` expands
the architecture and inspects available private sessions for per-agent details;
JSON also includes their retained notes. Public summaries show the full text
of each selected message. `project explain` drills into architecture;
`project context --task ID` and `agent show ID --json` drill into work and evidence.
When the same complete snapshot appears in several sections, later sections retain
its id, revision and execution state and point to the first displayed section.
Changed dependency state is rendered again even if the record revision is unchanged.

## Normal coding workflow

```bash
reql query_context --query "serializer" --code --brief
reql agent init --name "Serializer contract"
reql agent record goal "Preserve serializer round trips" --key serializer
reql agent task create "Verify empty payload" --parent GOAL_ID --file src/codec.py --next "Check empty-field round trip"
reql agent init --task TASK_ID
reql agent record decision "Keep empty fields" --parent TASK_ID --why "Consumers distinguish absent and empty"
reql agent finish "Empty fields preserved; codec and consumer checks passed; malformed-input question remains" --task TASK_ID
```

Use the ids/revisions actually returned by your commands for `GOAL_ID`, `TASK_ID` and `N`.
The example assumes the source query supplies sufficient context. Use `project overview`
for unknown direction or scoped `project context` for missing operational evidence.
Record at natural
boundaries: a decision, failed approach, substantial tested change, blocker or
interruption. Query and reconcile before creating another record. Do not persist
secrets, personal data, source dumps or repetitive tool output. Use private notes
for scratch, shared records for knowledge a later agent needs.

Before switching direction after a failed or discarded approach, record what was
tried and the observed failing checks or broken contract. Keep affected source and
test paths on the shared failure so recovery does not depend on matching prose:

```bash
reql agent record failure "<approach>" --why "<observed failure and reason>" --file SOURCE_PATH --file TEST_PATH
reql project context --file SOURCE_PATH --file TEST_PATH
```

Initialize a session before writing if none is active. Add `--parent TASK_ID` for
an existing task. Omit `--file` when file scope is unknown. Update an existing
failure by id and revision.
Keep failures active until evidence resolves or supersedes them; finishing a
session or finding a successful alternative does not resolve a failed approach.

Reuse relevant decisions and failures from a supplied handoff or source query.
Recover by affected files when the handoff lacks evidence. Use
`agent show ID --json` for a marked omission that affects the task. Retry only when new evidence addresses the recorded
reason. These instructions require the agent to capture the failure; REQL does
not infer failed approaches from shell output or transcripts.

## Retention

Shared storage keeps terminal outcomes and each previous revision. Finishing a
session saves a checkpoint linked to all its work and pending next actions.
`retention.agent_sessions` controls public messages and roster entries.

For installed skills, routed references and hooks, see
[Assistant installs](CLI.md#assistant-installs).
