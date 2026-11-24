"""Durable engineering work, separate from compiled facts and private scratch.

One mutable record owns each outcome or decision. Relations refer to stable ids;
revision snapshots retain the reasons for changes.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from heapq import heapify, heappop, heappush
from pathlib import Path, PurePosixPath
import re
import hashlib
import json
import math
import os
import time
from typing import Any, Iterable, Iterator

from memory.config import REQLConfig, default_config
from memory.domain.exceptions import StorageBusyError
from memory.domain.ids import stable_id
from memory.domain.models import MemoryNode
from memory.domain.timeutils import parse_dt, utcnow_iso
from memory.storage import BlockGraphStore
from memory.extraction.normalization import tokenize

KINDS = frozenset({"goal", "task", "decision", "constraint", "failure", "question", "change", "observation", "checkpoint"})
STATUSES = frozenset({"active", "open", "in_progress", "blocked", "done", "resolved", "superseded", "invalidated", "abandoned"})
OBSOLETE_STATUSES = frozenset({"superseded", "invalidated", "abandoned"})
TERMINAL = frozenset({"done", "resolved", "superseded", "invalidated", "abandoned"})
RELATIONS = ("parent", "depends_on", "contradicts", "supersedes", "summarizes")
RECORD_TYPE = "work_record"


def _tokens(text: str) -> set[str]:
    """Normalize identifiers, paths and prose for deterministic matching."""
    return set(tokenize(re.sub(r"[\\/._-]", " ", text)))


def _paths(paths: list[str]) -> list[str]:
    """Accept only repository-relative evidence paths; never open them here."""
    if not isinstance(paths, list) or any(not isinstance(value, str) for value in paths):
        raise ValueError("Evidence paths must be a list of strings")
    result: dict[str, None] = {}
    for value in paths:
        path = PurePosixPath(value.replace("\\", "/"))
        if not value.strip() or path.is_absolute() or ".." in path.parts or ":" in value:
            raise ValueError(f"Evidence path must be repository-relative: {value}")
        normalized = path.as_posix()
        if normalized != ".":
            result[normalized] = None
    return list(result)


def task_claim(task: dict[str, Any]) -> dict[str, str] | None:
    """Read an active claim, including in-progress records from older stores."""
    if task.get("kind") != "task" or task.get("status") != "in_progress":
        return None
    if "claim" in task:
        return task["claim"]
    if task.get("agent_id") and task.get("session_id"):
        return {"agent_id": task["agent_id"], "session_id": task["session_id"]}
    return None


def require_task_claim(task: dict[str, Any], agent_id: str, session_id: str | None) -> None:
    """Reject use of work assigned to a different session."""
    owner = task_claim(task)
    if owner is None:
        raise ValueError(f"Task {task['id']} needs a claim in this session. Run `reql agent init --task {task['id']}` first.")
    if owner != {"agent_id": agent_id, "session_id": session_id}:
        raise ValueError(
            f"Task {task['id']} is already claimed by {owner['agent_id']} in session {owner['session_id']}. "
            f"Ask the coordinator to release it with `reql agent task update --id {task['id']} "
            '--status open --why "handoff reason"`.'
        )


def _record_fields(kind: str, content: str) -> dict[str, Any]:
    """Construct the common fields for a new work record."""
    return {"kind": kind, "content": content, "title": content.splitlines()[0],
            "status": "open" if kind in {"task", "question"} else "active",
            "rationale": "", "files": [], "workstream": "", "parent": "",
            "depends_on": [], "contradicts": [], "supersedes": [], "summarizes": [],
            "next_action": "", "importance": 2}


class CoordinationStore:
    """Serialize shared updates and project compact, causal engineering context."""

    def __init__(self, path: str | Path, *, config: REQLConfig | None = None) -> None:
        self.config = config or default_config()
        self.path = Path(path).expanduser().absolute()
        if not self.path.resolve().is_relative_to(self.path.parent.resolve()):
            raise ValueError("Engineering store must remain inside its storage directory")

    @classmethod
    def for_project(cls, storage: str | Path, *, config: REQLConfig | None = None) -> CoordinationStore:
        """Resolve the configured shared work store for a project graph."""
        config = config or default_config()
        return cls(config.workspace.dashboard_path(storage), config=config)

    @contextmanager
    def _open(self, *, read_only: bool = False, lock_timeout_seconds: float | None = None) -> Iterator[BlockGraphStore]:
        """Wait for the shared or exclusive lease before reading or writing work."""
        store = BlockGraphStore(self.path, create=not read_only, read_only=read_only,
                                defer_lexical_index=True, wait_for_lock=lock_timeout_seconds is None,
                                lock_timeout_seconds=lock_timeout_seconds, policy=self.config.storage)
        try:
            yield store
        finally:
            store.close()

    def records(self) -> list[dict[str, Any]]:
        """Read shared state without creating an absent store."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return []
        with self._open(read_only=True) as store:
            # These nodes belong to this short-lived read-only store. Its payloads
            # can escape after close without copying a second, inaccessible owner.
            return [self._payload(node) for node in store.find_nodes_by_types([RECORD_TYPE], clone=False)]

    @staticmethod
    def _payload(node: MemoryNode) -> dict[str, Any]:
        """Expose operational status without reusing graph salience semantics."""
        payload = {"summarizes": [], **node.properties, "id": node.id, "type": node.properties["kind"],
                   "created_at": node.created_at, "updated_at": node.updated_at}
        if payload["kind"] == "task":
            payload["claim"] = task_claim(node.properties)
        return payload

    def put(
        self, kind: str | None = None, content: str | None = None, *, agent_id: str, session_id: str | None,
        session_title: str = "", key: str | None = None, record_id: str | None = None,
        expected_revision: int | None = None, status: str | None = None,
        rationale: str | None = None, files: list[str] | None = None,
        workstream: str | None = None, parent: str | None = None,
        depends_on: list[str] | None = None, contradicts: list[str] | None = None,
        supersedes: list[str] | None = None, next_action: str | None = None,
        summarizes: list[str] | None = None,
        importance: int | None = None,
        failure_evidence: dict[str, Any] | None = None,
        allowed_kinds: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Upsert one identity atomically; reject stale revisions and invalid links.

        Omitted fields, including kind/content, survive updates by id and revision.
        Creation requires kind/content. An explicit empty list clears a relation.
        Updating by id requires a revision, preventing silent cross-agent overwrite.
        An optional kind restriction applies even if the update omits its kind.
        """
        if (kind is not None and not isinstance(kind, str)) or (content is not None and not isinstance(content, str)):
            raise ValueError("Record kind and content must be strings")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("Record needs a nonempty agent identity")
        if record_id is not None and (not isinstance(record_id, str) or not record_id.strip()):
            raise ValueError("Assigned record id must be a nonempty string")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError("Expected revision must be a nonnegative integer")
        kind = kind.strip().casefold() if kind is not None else None
        content = content.strip() if content is not None else None
        if (kind is not None and kind not in KINDS) or content == "":
            raise ValueError("Record needs a supported kind and nonempty content")
        if record_id and expected_revision is None:
            raise ValueError("Updating by id requires expected_revision; read agent show first")
        if status is not None and status not in STATUSES:
            raise ValueError(f"Unsupported work status: {status}")
        if importance is not None and (type(importance) is not int or importance not in {1, 2, 3}):
            raise ValueError("Importance must be 1, 2 or 3")
        for value in (rationale, next_action, workstream, key):
            if value is not None and not isinstance(value, str):
                raise ValueError("Record fields must be strings")
        if parent is not None and not isinstance(parent, str):
            raise ValueError("Parent must be a record id")
        if files is not None:
            files = _paths(files)
        if failure_evidence is not None:
            if not isinstance(failure_evidence, dict) or set(failure_evidence) - {"source_revision", "symbol_ids", "test_ids", "observed_checks"}:
                raise ValueError("Unsupported failure evidence fields")
            revision = failure_evidence.get("source_revision")
            if revision is not None and (not isinstance(revision, str) or not revision):
                raise ValueError("Failure evidence source_revision must be a revision id")
            for field in ("symbol_ids", "test_ids", "observed_checks"):
                values = failure_evidence.get(field, [])
                if not isinstance(values, list) or any(
                    not isinstance(value, str) or not value for value in values
                ):
                    raise ValueError(f"Failure evidence {field} must be a list of nonempty strings")
        for relation, values in (("depends_on", depends_on), ("contradicts", contradicts), ("supersedes", supersedes), ("summarizes", summarizes)):
            if values is not None and (not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values)):
                raise ValueError(f"{relation} must be a list of record ids")
        depends_on = list(dict.fromkeys(depends_on)) if depends_on is not None else None
        contradicts = list(dict.fromkeys(contradicts)) if contradicts is not None else None
        supersedes = list(dict.fromkeys(supersedes)) if supersedes is not None else None
        summarizes = list(dict.fromkeys(summarizes)) if summarizes is not None else None
        partial = kind is None or content is None
        if record_id is None:
            if kind is None or content is None:
                raise ValueError("Creating work requires kind and content")
            implicit_key = (parent + "\n" if parent else "") + " ".join(content.casefold().split())
            identity = stable_id("work", kind, workstream or "", key or implicit_key)
        else:
            identity = record_id
        with self._open() as store, store.transaction():
            self._require_open_session(store, session_id)
            node = store.get_node(identity)
            if record_id and (node is None or node.type != RECORD_TYPE):
                raise ValueError(f"Work record not found: {record_id}")
            old = dict(node.properties) if node else {}
            if partial:
                kind = old.get("kind") if kind is None else kind
                content = old.get("content") if content is None else content
                if not isinstance(kind, str) or not isinstance(content, str):
                    raise ValueError("Stored work record has invalid kind or content")
            if old:
                old.setdefault("summarizes", [])
            if old and old["kind"] != kind:
                raise ValueError("A record's kind cannot change")
            if allowed_kinds is not None and kind not in allowed_kinds:
                raise ValueError(f"Work kind {kind!r} is not supported by this operation")
            if failure_evidence is not None and kind != "failure":
                raise ValueError("Only failure records accept failure_evidence")
            if expected_revision is not None and expected_revision != old.get("revision", 0):
                raise ValueError("Work revision changed; read agent show and reconcile before updating")
            now = utcnow_iso()
            owner = task_claim(old)
            if old and kind == "task":
                old["claim"] = owner
            defaults = _record_fields(kind, content)
            props = {**defaults, **old}
            props.update(content=content, title=defaults["title"])
            supplied = {"status": status, "rationale": rationale, "files": files, "workstream": workstream,
                        "parent": parent, "depends_on": depends_on, "contradicts": contradicts,
                        "supersedes": supersedes, "summarizes": summarizes, "next_action": next_action, "importance": importance}
            props.update({name: value for name, value in supplied.items() if value is not None})
            if failure_evidence is not None:
                props["failure_evidence"] = failure_evidence
            item_id = node.id if node else identity
            if kind == "task":
                if owner and props["status"] != old["status"]:
                    if props["status"] == "open":
                        if not rationale or not rationale.strip():
                            raise ValueError("Releasing a claim needs a handoff reason")
                    else:
                        require_task_claim({**old, "id": item_id}, agent_id, session_id)
                props["claim"] = owner if props["status"] == "in_progress" else None
            if props["status"] != old.get("status") and props["status"] in {"blocked", "superseded", "invalidated", "abandoned"} and not rationale:
                raise ValueError("Blocked or obsolete work needs a rationale")
            if old.get("status") in TERMINAL and props["status"] not in TERMINAL and not rationale:
                raise ValueError("Reopening completed or obsolete work needs a rationale")
            prior_replacements = old.get("supersedes", [])
            if (props["supersedes"] != prior_replacements or kind in {"decision", "failure"}) and not props["rationale"]:
                raise ValueError("Decisions, failures and replacement need a rationale")
            prior_targets = set(prior_replacements)
            records: dict[str, dict[str, Any]] = {}
            for relation in RELATIONS:
                targets = [props[relation]] if relation == "parent" and props[relation] else ([] if relation == "parent" else props[relation])
                for target in targets:
                    target_node = store.get_node(target, clone=False) if target != item_id else None
                    if target_node is None or target_node.type != RECORD_TYPE:
                        raise ValueError(f"Invalid {relation} target: {target}")
                    records[target] = target_node.properties
                    if relation == "supersedes" and target not in prior_targets and records[target].get("replaced_by"):
                        raise ValueError(f"Already superseded: {target}; reconcile its current replacement {records[target]['replaced_by']}")
            records[item_id] = props
            self._validate_cycles(records, item_id, store=store)
            if kind == "task" and props["status"] in {"done", "resolved"}:
                # Completion depends on incoming conflicts as well as direct
                # links, so it still requires the complete consistent snapshot.
                records = {n.id: n.properties for n in store.find_nodes_by_types([RECORD_TYPE], clone=False)}
                records[item_id] = props
                self._require_completable(item_id, props, records)
            changed = {name: value for name, value in props.items() if name not in {"history", "revision", "agent_id", "session_id", "session_title", "activity_id"}}
            previous = {name: value for name, value in old.items() if name in changed}
            if node and changed == previous:
                return {"created": False, "changed": False, "node": self._payload(node)}
            if node and expected_revision is None:
                raise ValueError(f"Record already exists: {node.id}; update with its revision")
            for target in props["supersedes"]:
                if target in prior_targets:
                    continue
                replaced = store.get_node(target)
                if replaced is None:
                    raise ValueError(f"Replacement target disappeared: {target}")
                replacement_props = dict(replaced.properties)
                revision_history = list(replacement_props.get("history", []))
                revision_history.append({k: v for k, v in self._payload(replaced).items() if k != "history"})
                if replacement_props["kind"] == "task":
                    replacement_props["claim"] = None
                replacement_props.update(status="superseded", replaced_by=item_id, replacement_reason=props["rationale"],
                                         replacement_agent_id=agent_id, replacement_session_id=session_id,
                                         revision=replacement_props["revision"] + 1, history=revision_history)
                store.update_node_fields(target, properties=replacement_props, updated_at=now)
            stored, created = self._save_record(store, item_id, props, node, now, agent_id, session_id, session_title)
            return {"created": created, "changed": True, "node": self._payload(stored)}

    def _save_record(self, store: BlockGraphStore, item_id: str, props: dict[str, Any], node: MemoryNode | None,
                     now: str, agent_id: str, session_id: str | None, session_title: str) -> tuple[MemoryNode, bool]:
        """Persist a validated transition with its previous revision and provenance."""
        old = node.properties if node else {}
        history = list(old.get("history", []))
        if node:
            history.append({name: value for name, value in self._payload(node).items() if name != "history"})
        props.update(revision=int(old.get("revision", 0)) + 1, history=history,
                     agent_id=agent_id, session_id=session_id, session_title=session_title,
                     origin_agent_id=old.get("origin_agent_id", agent_id))
        return store.upsert_node(MemoryNode(
            id=item_id, type=RECORD_TYPE, canonical_key=item_id, label=props["title"], text=props["content"],
            properties=props, created_at=node.created_at if node else now, updated_at=now))

    @staticmethod
    def _active_files(records: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
        """Index files named by in-progress tasks, irrespective of their workstream."""
        files: dict[str, list[str]] = defaultdict(list)
        for item in records:
            if item["kind"] == "task" and item["status"] == "in_progress":
                for path in item["files"]:
                    files[os.path.normcase(path)].append(item["id"])
        return files

    def file_overlaps(self, task: dict[str, Any], records: Iterable[dict[str, Any]]) -> dict[str, Any]:
        """Return advisory overlaps without altering execution state."""
        return self._file_overlaps(task, self._active_files(records))

    def _file_overlaps(self, task: dict[str, Any], active_files: dict[str, list[str]]) -> dict[str, Any]:
        overlaps = []
        for path in task["files"]:
            peers = sorted({peer for peer in active_files.get(os.path.normcase(path), []) if peer != task["id"]})
            if peers:
                overlaps.append({"path": path, "tasks": peers, "omitted": 0})
        return {"files": overlaps, "omitted": 0}

    def assignment_context(self, task_id: str) -> dict[str, Any]:
        """Recover causal work and advisory file overlaps from one shared snapshot."""
        records = self.records()
        context = self.context(record_id=task_id, _records=records)
        result = {"work_context": context}
        task = next((record for record in context["records"] if record["id"] == task_id), None)
        if task is not None and task["status"] == "in_progress":
            overlaps = self.file_overlaps(task, records)
            if overlaps["files"]:
                result["file_overlaps"] = overlaps
        return result

    def _claim(self, store: BlockGraphStore, node: MemoryNode, *, agent_id: str, session_id: str,
               session_title: str) -> dict[str, Any]:
        """Publish a new claim from a writer snapshot."""
        claimed, _ = self._save_record(
            store, node.id, {"summarizes": [], **node.properties, "status": "in_progress",
                             "claim": {"agent_id": agent_id, "session_id": session_id}},
            node, utcnow_iso(), agent_id, session_id, session_title,
        )
        return self._payload(claimed)

    def claim_task(self, task_id: str, revision: int, *, agent_id: str, session_id: str,
                   session_title: str) -> dict[str, Any]:
        """Claim an explicit assignment or return the current session's existing claim."""
        with self._open() as store, store.transaction():
            self._require_open_session(store, session_id)
            node = store.get_node(task_id)
            if node is None or node.type != RECORD_TYPE or node.properties["kind"] != "task":
                raise ValueError(f"Task not found: {task_id}")
            task = self._payload(node)
            if task["revision"] != revision:
                raise ValueError("Work revision changed; read agent show and reconcile before claiming")
            if task["status"] in TERMINAL:
                raise ValueError("Assigned task is terminal; explicitly reopen it with a rationale first")
            if task["status"] == "blocked":
                raise ValueError("Assigned task is blocked; reopen it when the blocker is resolved")
            if task_claim(task):
                require_task_claim(task, agent_id, session_id)
                return {"assignment_status": "resumed", "claimed_task": task}
            return {"assignment_status": "claimed",
                    "claimed_task": self._claim(store, node, agent_id=agent_id, session_id=session_id,
                                                session_title=session_title)}

    def claim_next(self, *, agent_id: str, session_id: str, session_title: str = "",
                   workstream: str | None = None) -> dict[str, Any]:
        """Select and claim ready work from one writer snapshot, or resume the caller's claim."""
        if any(not isinstance(value, str) or not value.strip() for value in (agent_id, session_id)):
            raise ValueError("Claim needs nonempty agent and session identities")
        if workstream is not None and (not isinstance(workstream, str) or not workstream.strip()):
            raise ValueError("Workstream must be a nonempty record field")
        with self._open() as store, store.transaction():
            self._require_open_session(store, session_id)
            nodes = {node.id: node for node in store.find_nodes_by_types([RECORD_TYPE], clone=False)}
            records = {node_id: self._payload(node) for node_id, node in nodes.items()}
            candidates = sorted((item for item in records.values() if item["kind"] == "task"
                                 and item["status"] not in TERMINAL
                                 and (workstream is None or item["workstream"] == workstream)),
                                key=lambda item: (-item["importance"], item["created_at"], item["id"]))
            active_files = self._active_files(records.values())
            owner = {"agent_id": agent_id, "session_id": session_id}
            owned = next((item for item in candidates if task_claim(item) == owner), None)
            if owned is not None:
                return {"status": "resumed", "claimed_task": owned, "file_overlaps": self._file_overlaps(owned, active_files)}

            conflicts = self._conflict_index(records)
            states = [(item, self._execution(item, records, conflicts)) for item in candidates]
            ready = [item for item, state in states if state["execution_state"] == "ready"]
            if not ready:
                blockers = [{"id": item["id"], **state} for item, state in states
                            if state["execution_state"] in {"blocked", "needs_review"}]
                return {"status": "blocked" if blockers else "no_ready_task", "claimed_task": None,
                        "blockers": blockers, "omitted_blockers": 0}
            selected = next((item for item in ready if not any(os.path.normcase(path) in active_files
                                                               for path in item["files"])), ready[0])
            claimed = self._claim(store, nodes[selected["id"]], agent_id=agent_id,
                                  session_id=session_id, session_title=session_title)
            return {"status": "claimed", "claimed_task": claimed,
                    "file_overlaps": self._file_overlaps(selected, active_files)}

    @staticmethod
    def _finished_checkpoint(store: BlockGraphStore, session_id: str) -> MemoryNode | None:
        """Find this session's committed handoff while holding the relevant store lease."""
        checkpoint = store.get_node(stable_id("work", "checkpoint", "", session_id), clone=False)
        return checkpoint if checkpoint is not None and "session_finish" in checkpoint.properties else None

    def _require_open_session(self, store: BlockGraphStore, session_id: str | None) -> None:
        """Keep a partially closed session from acquiring new work or recording progress."""
        if session_id and self._finished_checkpoint(store, session_id) is not None:
            raise ValueError("Session finish already committed. Retry the original finish to close this session.")

    def finish_committed(self, session_id: str) -> bool:
        """Check whether a prior finish reached the shared commit before closing privately."""
        with self._open(read_only=True) as store:
            return self._finished_checkpoint(store, session_id) is not None

    def finish_session(
        self, summary: str, *, agent_id: str, session_id: str, session_title: str,
        task_id: str | None = None, task_revision: int | None = None,
    ) -> dict[str, Any]:
        """Commit a session checkpoint, claimed completion and remaining releases together."""
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("Finish message must not be empty")
        checkpoint_id = stable_id("work", "checkpoint", "", session_id)
        owner = {"agent_id": agent_id, "session_id": session_id}
        with self._open() as store, store.transaction():
            checkpoint = store.get_node(checkpoint_id, clone=False)
            if checkpoint is not None and (finished := checkpoint.properties.get("session_finish")) is not None:
                if checkpoint.text != summary or finished["task_id"] != task_id:
                    raise ValueError("Session finish already committed with a different outcome. Retry the original finish.")
                completed = None
                if task_id is not None:
                    node = store.get_node(task_id, clone=False)
                    completed = self._payload(node) if node is not None else {}
                    if completed.get("revision") != finished["task_revision"]:
                        completed = next(
                            (row for row in completed.get("history", []) if row["revision"] == finished["task_revision"]),
                            {"id": task_id, "status": "done", "revision": finished["task_revision"]},
                        )
                return {"checkpoint": self._payload(checkpoint), "completed_task": completed,
                        "released_claims": finished["released_claims"]}

            nodes = {node.id: node for node in store.find_nodes_by_types([RECORD_TYPE], clone=False)}
            now = utcnow_iso()
            completed = None
            if task_id is not None:
                node = nodes.get(task_id)
                if node is None or node.properties["revision"] != task_revision:
                    raise ValueError("Work revision changed; read agent show and reconcile before completing")
                require_task_claim(self._payload(node), agent_id, session_id)
                self._require_completable(task_id, node.properties,
                                          {item_id: item.properties for item_id, item in nodes.items()})
                finished_task, _ = self._save_record(
                    store, task_id, {**node.properties, "status": "done", "claim": None,
                                     "rationale": summary, "next_action": ""},
                    node, now, agent_id, session_id, session_title,
                )
                completed = self._payload(finished_task)
                nodes[task_id] = finished_task

            work = []
            released = set()
            for node in nodes.values():
                props = node.properties
                if props["kind"] == "checkpoint":
                    continue
                if task_claim(props) == owner:
                    updated, _ = self._save_record(
                        store, node.id, {**props, "status": "open", "claim": None},
                        node, now, agent_id, session_id, session_title,
                    )
                    work.append(self._payload(updated))
                    released.add(node.id)
                elif props.get("session_id") == session_id:
                    work.append(self._payload(node))
            work.sort(key=lambda item: item["updated_at"], reverse=True)
            # Keep released work visible ahead of historical summaries.
            work.sort(key=lambda item: (item["id"] not in released, item["status"] in TERMINAL))
            finish_fields = {
                "task_id": task_id, "task_revision": completed["revision"] if completed else None,
                "released_claims": len(released),
            }
            checkpoint, _ = self._save_record(
                store, checkpoint_id, {
                    **_record_fields("checkpoint", summary), "status": "done",
                    "files": sorted({path for item in work for path in item["files"]}),
                    "summarizes": [item["id"] for item in work],
                    "next_action": "; ".join(item["next_action"] or item["content"] for item in work
                                             if item["kind"] == "task" and item["status"] not in TERMINAL
                                             and (task_claim(item) in (None, owner))),
                    "session_finish": finish_fields,
                },
                checkpoint, now, agent_id, session_id, session_title,
            )
            return {"checkpoint": self._payload(checkpoint), "completed_task": completed,
                    "released_claims": len(released)}

    @staticmethod
    def _validate_cycles(records: dict[str, dict[str, Any]], start: str,
                         *, store: BlockGraphStore) -> None:
        """Reject dependency, hierarchy and replacement cycles before any write."""
        for relation in ("parent", "depends_on", "supersedes"):
            visiting: set[str] = set()
            visited: set[str] = set()

            def targets(item_id: str) -> Iterator[str]:
                """Read outgoing links of the current relation from the writer snapshot."""
                if item_id not in records:
                    node = store.get_node(item_id, clone=False)
                    if node is None or node.type != RECORD_TYPE:
                        raise KeyError(item_id)
                    records[item_id] = node.properties
                item = records[item_id]
                if relation == "parent":
                    if item[relation]:
                        yield item[relation]
                else:
                    yield from item[relation]

            visiting.add(start)
            stack = [(start, targets(start))]
            while stack:
                item_id, outgoing = stack[-1]
                target = next(outgoing, None)
                if target is None:
                    stack.pop()
                    visiting.remove(item_id)
                    visited.add(item_id)
                elif target in visiting:
                    raise ValueError(f"Cycle in {relation}")
                elif target not in visited:
                    visiting.add(target)
                    stack.append((target, targets(target)))

    def find(self, record_id: str, *, lock_timeout_seconds: float | None = None) -> dict[str, Any] | None:
        """Find one shared identity without scanning work or creating an absent store."""
        if self.path.exists() and self.path.stat().st_size:
            with self._open(read_only=True, lock_timeout_seconds=lock_timeout_seconds) as store:
                node = store.get_node(record_id)
                if node is not None and node.type == RECORD_TYPE:
                    return self._payload(node)
        return None

    def show(self, record_id: str) -> dict[str, Any]:
        """Retrieve a full record and its revision history."""
        result = self.find(record_id)
        if result is not None:
            return result
        raise ValueError(f"Work record not found: {record_id}")

    def wait_for_status(self, record_id: str, statuses: frozenset[str], *,
                        timeout_seconds: float) -> tuple[dict[str, Any] | None, bool]:
        """Wait for a status with no shared lease held between reads."""
        if not statuses or not statuses <= STATUSES:
            raise ValueError("Select at least one valid work status")
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("Wait timeout must be finite and non-negative")
        deadline = time.monotonic() + timeout_seconds
        last = None
        last_version = None
        delay = 0.05
        while True:
            try:
                version = BlockGraphStore.read_payload_version(
                    self.path, lock_timeout_seconds=max(0.0, deadline - time.monotonic()),
                )
                if version != last_version:
                    # Capture the version before reading. A commit between the two
                    # leases can trigger an extra read, but cannot hide an update.
                    last = self.find(record_id, lock_timeout_seconds=max(0.0, deadline - time.monotonic()))
                    last_version = version
            except StorageBusyError:
                return last, False
            if last is None:
                raise ValueError(f"Work record not found: {record_id}")
            matched = last["status"] in statuses
            remaining = deadline - time.monotonic()
            if matched or last["status"] in TERMINAL or remaining <= 0:
                return last, matched
            time.sleep(min(delay, remaining))
            delay = min(2 * delay, 0.5)

    @classmethod
    def _require_completable(cls, record_id: str, task: dict[str, Any], records: dict[str, dict[str, Any]]) -> None:
        """Check prerequisites and conflicts before completing a task."""
        execution = cls._execution({**task, "id": record_id, "status": "open"}, records)
        if execution["execution_state"] in {"blocked", "needs_review"}:
            raise ValueError("Cannot complete task with waiting, obsolete or conflicting dependencies")

    @staticmethod
    def _conflict_index(records: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
        """Index current conflict endpoints once per consistent record snapshot."""
        conflicts: dict[str, set[str]] = defaultdict(set)
        for item_id, item in records.items():
            for target in item["contradicts"]:
                if target in records and records[target]["status"] not in TERMINAL:
                    conflicts[item_id].add(target)
                if item["status"] not in TERMINAL:
                    conflicts[target].add(item_id)
        return conflicts

    @staticmethod
    def _execution(item: dict[str, Any], records: dict[str, dict[str, Any]],
                   conflicts_by_id: dict[str, set[str]] | None = None) -> dict[str, Any]:
        """Derive readiness, invalid assumptions and conflicts from current links."""
        if conflicts_by_id is None:
            conflicts_by_id = CoordinationStore._conflict_index(records)
        waiting, obsolete = [], []
        for target in item["depends_on"]:
            dependency = records.get(target)
            if dependency is None or dependency["status"] in {"superseded", "invalidated", "abandoned"}:
                obsolete.append(target)
            elif dependency["kind"] in {"task", "goal", "question"} and dependency["status"] not in {"done", "resolved"}:
                waiting.append(target)
            if dependency is not None and dependency["status"] not in TERMINAL and conflicts_by_id.get(target):
                obsolete.append(target)
        conflicts = conflicts_by_id.get(item["id"], set())
        state = item["status"]
        if state not in TERMINAL:
            state = "needs_review" if obsolete or conflicts else ("blocked" if waiting or state == "blocked" else ("ready" if state == "open" else state))
        return {"execution_state": state, "waiting_on": waiting, "obsolete_dependencies": obsolete, "conflicts": sorted(conflicts)}

    @staticmethod
    def context_limit(limit: int | None) -> int | None:
        """Validate an optional result limit supplied by the caller."""
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("Context limit must be a positive integer")
        return limit

    def context(self, query: str = "", *, files: list[str] | None = None, workstream: str | None = None,
                record_id: str | None = None, limit: int | None = None, include_history: bool = False,
                kinds: list[str] | None = None, _records: list[dict[str, Any]] | None = None,
                _source_lookup: bool = False) -> dict[str, Any]:
        """Prioritize current file contracts and recover all reachable causal work."""
        limit = self.context_limit(limit)
        if kinds is not None and (
            not isinstance(kinds, list) or any(not isinstance(kind, str) or kind not in KINDS for kind in kinds)
        ):
            raise ValueError("kinds must be a list of supported work record kinds")
        all_records = self.records() if _records is None else _records
        records = {item["id"]: item for item in all_records}
        if record_id and record_id not in records:
            raise ValueError(f"Work record not found: {record_id}")
        replacements: dict[str, dict[str, Any] | None] = {}

        def resolve(item_id: str | None) -> dict[str, Any] | None:
            """Resolve historical links against the same work snapshot."""
            if item_id is None:
                return None
            return records.get(item_id) if include_history else self._current_replacement(records, item_id, replacements)

        anchor = resolve(record_id)
        anchor_id = anchor["id"] if anchor is not None else record_id
        constraint_targets: set[str] = set()
        if anchor and anchor["kind"] == "task":
            for target in anchor["depends_on"]:
                criterion = resolve(target)
                if criterion and criterion["kind"] == "constraint" and criterion["status"] not in OBSOLETE_STATUSES:
                    constraint_targets.add(target)
        tokens = _tokens(query)
        paths = _paths(files or [])
        # A known identity recovers linked work, not lexical matches in the backlog.
        eligible = [item for item in all_records
                    if (not record_id or item["id"] == anchor_id)
                    and (workstream is None or item["workstream"] == workstream)
                    and (include_history or item["status"] not in OBSOLETE_STATUSES)]
        words = {
            item["id"]: tokens & _tokens(" ".join(
                [item["content"], item["rationale"], item["next_action"], item["workstream"], *item["files"]]
            ))
            for item in eligible
        } if tokens and not record_id else {}
        frequency = Counter(term for terms in words.values() for term in terms)
        denominator = sum(1 / frequency.get(term, 1) for term in tokens) or 1
        now = parse_dt(utcnow_iso())
        scores: dict[str, float] = {}
        reasons: dict[str, dict[str, None]] = {}
        for item in eligible:
            overlap = sum(1 / frequency[term] for term in words[item["id"]]) / denominator if words else 0.0
            path_match = any(path == evidence or path.startswith(evidence + "/") or evidence.startswith(path + "/") for path in paths for evidence in item["files"])
            if _source_lookup and overlap and not path_match and item["id"] != anchor_id:
                common = words[item["id"]]
                # Source lookup is not a history search. Text-only checkpoints
                # need full query coverage; linked ones can still be expanded.
                enough = (
                    len(common) >= 3 and common == tokens if item["kind"] == "checkpoint"
                    else source_work_text_match(query, tokens, common) and overlap >= 0.5
                )
                if not enough:
                    continue
            anchors = []
            if overlap:
                anchors.append("text")
            if path_match:
                anchors.append("file")
            if anchor_id == item["id"]:
                anchors.append("task")
                if anchor_id != record_id:
                    anchors.append(f"replacement:{record_id}")
            if not tokens and not paths and not record_id:
                anchors.append("overview")
            if not anchors:
                continue
            age = max(0, (now.date() - parse_dt(item["updated_at"]).date()).days)
            priority = 0.18 if item["kind"] in {"decision", "constraint", "failure", "goal", "question"} else 0
            score = overlap * 2 + path_match * 3 + (anchor_id == item["id"]) * 4 + priority + item["importance"] * 0.05 + 0.1 / (1 + age)
            if item["status"] not in TERMINAL:
                # A current file-scoped obligation outranks even an exact lexical
                # match in a completed summary for that file. History stays available.
                score += 3 if path_match else 0.25
            scores[item["id"]], reasons[item["id"]] = score, dict.fromkeys(anchors)
        # Queries that match no work require neither reverse relations nor conflicts.
        incoming: dict[str, dict[str, set[str]]] = {relation: defaultdict(set) for relation in RELATIONS}
        if scores:
            for item in all_records:
                for relation in RELATIONS:
                    targets = (item[relation],) if relation == "parent" and item[relation] else (
                        () if relation == "parent" else item[relation]
                    )
                    for target in targets:
                        incoming[relation][target].add(item["id"])
        seed_ids = set(scores)
        frontier = [(-score, item_id) for item_id, score in scores.items()]
        heapify(frontier)
        expanded: set[str] = set()
        # Inspect dependents/children of direct matches, not every shared prerequisite
        # reached along the way: otherwise one contract broadcasts unrelated lanes.
        while frontier:
            _, item_id = heappop(frontier)
            if item_id in expanded:
                continue
            # Relation weights attenuate scores. The strongest route to this
            # record is therefore settled before its outgoing links are visited.
            expanded.add(item_id)
            item = records[item_id]
            related = dict.fromkeys(item["depends_on"] + item["contradicts"], 0.9)
            related.update(dict.fromkeys(incoming["contradicts"].get(item_id, ()), 0.9))
            related.update(dict.fromkeys(incoming["supersedes"].get(item_id, ()), 0.9))
            if item["parent"]:
                related.setdefault(item["parent"], 0.75)
            for target in item["summarizes"]:
                related.setdefault(target, 0.65)
            if item_id in seed_ids:
                dependency_anchors = {item_id}
                if item_id == anchor_id and record_id:
                    dependency_anchors.add(record_id)
                for dependency_anchor in dependency_anchors:
                    for target in incoming["depends_on"].get(dependency_anchor, ()):
                        related.setdefault(target, 0.45)
            for target in incoming["summarizes"].get(item_id, ()):
                related.setdefault(target, 0.35)
            if item_id in seed_ids and item["kind"] == "task":
                for target in incoming["parent"].get(item_id, ()):
                    related.setdefault(target, 0.65)
            if include_history:
                for target in item["supersedes"]:
                    related.setdefault(target, 0.65)
            if item_id == anchor_id:
                related.update(dict.fromkeys(constraint_targets, 0.95))
                related.update(dict.fromkeys(item["contradicts"], 0.97))
                related.update(dict.fromkeys(incoming["contradicts"].get(item_id, ()), 0.97))
            for target in sorted(related, key=lambda target: (-related[target], target)):
                candidate = resolve(target)
                if candidate is None or candidate["id"] == item_id:
                    continue
                candidate_id = candidate["id"]
                relation_score = scores[item_id] * related[target]
                if candidate_id not in scores or relation_score > scores[candidate_id]:
                    scores[candidate_id] = relation_score
                    heappush(frontier, (-relation_score, candidate_id))
                evidence = reasons.setdefault(candidate_id, {})
                evidence[f"related:{item_id}"] = None
                if candidate_id != target:
                    evidence[f"replacement:{target}"] = None
        ranked = sorted((item_id for item_id in scores if kinds is None or records[item_id]["kind"] in kinds),
                        key=lambda item_id: (-scores[item_id], records[item_id]["updated_at"], item_id))
        selected = []
        conflicts_by_id = self._conflict_index(records) if ranked else {}
        for item_id in ranked[:limit]:
            item = records[item_id]
            compact = {k: v for k, v in item.items() if k != "history"}
            if include_history and record_id == item_id:
                compact["history"] = item["history"]
            compact.update(self._execution(item, records, conflicts_by_id), score=round(scores[item_id], 4), reasons=list(reasons[item_id]))
            selected.append(compact)
        revision = hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return {"format": "reql-work-context-v1", "revision": revision, "query": query, "records": selected,
                "total_matches": len(ranked), "omitted": len(ranked) - len(selected), "expansion_truncated": False}

    @staticmethod
    def _current_replacement(records: dict[str, dict[str, Any]], record_id: str | None,
                             resolved: dict[str, dict[str, Any] | None] | None = None) -> dict[str, Any] | None:
        """Follow explicit replacements without reviving obsolete work or changing links."""
        if resolved is None:
            resolved = {}
        visited: set[str] = set()
        current = None
        while record_id and record_id not in visited:
            if record_id in resolved:
                current = resolved[record_id]
                break
            visited.add(record_id)
            item = records.get(record_id)
            if item is None or item["status"] not in OBSOLETE_STATUSES:
                current = item
                break
            record_id = item.get("replaced_by")
        resolved.update(dict.fromkeys(visited, current))
        return current

    def overview(self, *, limit: int | None = None) -> dict[str, Any]:
        """Project direction and execution from authoritative current records."""
        limit = self.context_limit(limit)
        records = self.records()
        by_id = {item["id"]: item for item in records}
        conflicts_by_id = self._conflict_index(by_id)
        sections: dict[str, list[dict[str, Any]]] = {name: [] for name in ("goals", "direction", "active", "completed", "blockers", "failures", "next", "checkpoints")}
        workstreams: dict[str, Counter[str]] = {}
        for item in sorted(records, key=lambda item: (item["updated_at"], item["id"]), reverse=True):
            projected = {k: v for k, v in item.items() if k != "history"}
            projected.update(self._execution(item, by_id, conflicts_by_id))
            state = projected["execution_state"]
            if item["status"] in {"superseded", "invalidated", "abandoned"}:
                continue
            if item["kind"] == "goal" and item["status"] not in TERMINAL:
                sections["goals"].append(projected)
            if item["kind"] in {"decision", "constraint"} and item["status"] not in TERMINAL:
                sections["direction"].append(projected)
            if state in {"blocked", "needs_review"} or (item["kind"] == "question" and item["status"] not in TERMINAL):
                sections["blockers"].append(projected)
            if item["kind"] == "task":
                workstreams.setdefault(item["workstream"] or "default", Counter())[state] += 1
                sections["completed" if item["status"] in {"done", "resolved"} else "active"].append(projected)
                if state == "ready":
                    sections["next"].append(projected)
            if item["kind"] == "change" and item["status"] in {"done", "resolved"}:
                sections["completed"].append(projected)
            if item["kind"] == "checkpoint":
                sections["checkpoints"].append(projected)
            if item["kind"] == "failure" and item["status"] not in TERMINAL:
                sections["failures"].append(projected)
        return {"format": "reql-work-overview-v1", "sections": {k: v[:limit] for k, v in sections.items()},
                "counts": {k: len(v) for k, v in sections.items()}, "workstreams": {k: dict(v) for k, v in sorted(workstreams.items())}}


def source_work_text_match(query: str, tokens: set[str], common: set[str]) -> bool:
    """Distinguish an exact source identifier or a multi-term match from generic prose."""
    return len(common) >= 2 or bool(common and len(tokens) == 1 and ("_" in query or "." in query))


def render_file_overlaps(overlaps: dict[str, Any]) -> str:
    """List concurrent tasks by overlapping path."""
    if not overlaps["files"]:
        return ""
    lines = ["Concurrent tasks also name these files. Coordinate edits with their owners."]
    for row in overlaps["files"]:
        lines.append(f"- `{row['path']}`: {', '.join(row['tasks'])}")
    return "\n".join(lines)


def render_work_context(payload: dict[str, Any], *, max_chars: int | None = None, record_id: str | None = None,
                        complete_records: set[tuple[str, int]] | None = None,
                        record_notes: dict[tuple[str, int], str] | None = None, brief: bool = False) -> str:
    """Render one independent response and mark any requested omissions."""
    return _WorkContextRenderer(payload.get("records", [])).render(
        payload, max_chars=max_chars, record_id=record_id, complete_records=complete_records, record_notes=record_notes,
        brief=brief,
    )


def render_work_overview(payload: dict[str, Any], *, max_chars_per_section: int | None = None,
                         complete_records: set[tuple[str, int]] | None = None) -> str:
    """Render section membership while sharing only evidence already emitted in this overview."""
    renderer = _WorkContextRenderer(item for items in payload["sections"].values() for item in items)
    sections = []
    for label, items in payload["sections"].items():
        title = label.title()
        count = payload["counts"][label]
        text = renderer.render({"records": items, "omitted": max(0, count - len(items))},
                               max_chars=max_chars_per_section, section=title, heading=False,
                               complete_records=complete_records)
        sections.append(f"### {title} ({count})\n{text}")
    return "\n\n".join(sections)


class _WorkContextRenderer:
    """Own references to emitted text for one context response or composite overview."""

    def __init__(self, records: Iterable[dict[str, Any]]) -> None:
        """Start independent reference state without retaining prior responses."""
        self.texts: dict[str, tuple[str, str]] = {}
        self.records: dict[tuple[str, int], tuple[dict[str, Any], str, bool]] = {}
        owners: dict[str, set[tuple[str, int]]] = defaultdict(set)
        for item in records:
            owners[item["agent_id"]].add((item["id"], item["revision"]))
        self.owner_aliases: dict[str, str] = {}
        alias_number = 1
        for owner, identities in owners.items():
            alias = f"owner{alias_number}"
            while alias in owners:
                alias_number += 1
                alias = f"owner{alias_number}"
            # Count the declaration once and every reference, including the first.
            if len(identities) * len(owner) > len(owner) + 1 + len(identities) * len(alias):
                self.owner_aliases[owner] = alias
                alias_number += 1
        self.declared_owners: set[str] = set()

    def render(self, payload: dict[str, Any], *, max_chars: int | None = None, record_id: str | None = None,
               section: str | None = None, heading: bool = True,
               complete_records: set[tuple[str, int]] | None = None,
               record_notes: dict[tuple[str, int], str] | None = None, brief: bool = False) -> str:
        """Render full focused evidence when it fits, with explicit omissions and drill-down."""
        if max_chars is not None and (type(max_chars) is not int or max_chars < 1):
            raise ValueError("Work context character budget must be a positive integer")
        rows = ["## Engineering context"] if heading else []
        records = list(payload.get("records", []))

        def focused(item: dict[str, Any]) -> bool:
            """Keep the requested outcome or its current replacement ahead of neighbors."""
            return bool(record_id and (item["id"] == record_id or f"replacement:{record_id}" in item.get("reasons", [])))

        records.sort(key=lambda item: not focused(item))
        detailed = bool(record_id)
        compact = brief and not detailed
        used = len(rows[0]) if rows else 0
        rendered = 0
        needs_drill_down = False
        drill_down = "Truncated records: `reql agent show ID --json`."
        # Reserve the omission and recovery hints before adding prose.
        footer_budget = 100
        if compact:
            footer_budget += len(drill_down) + 1
        for item in records:
            summary_only = compact and item["kind"] in {"observation", "checkpoint"}
            remaining = max_chars - used - footer_budget - 1 if max_chars is not None else None
            owner = item["agent_id"]
            alias = self.owner_aliases.get(owner)
            attribution = (alias if owner in self.declared_owners else f"{alias}={owner}") if alias else owner
            header = f"- [{item['kind']}/{item['execution_state']}] {item['id']} r{item['revision']}"
            if not summary_only:
                header += f" ({attribution})"
            identity = (item["id"], item["revision"])
            previous = self.records.get(identity)
            if previous is not None and previous[0] == item:
                line = f"- [{item['kind']}/{item['execution_state']}] {item['id']} r{item['revision']}; see {previous[1]} above"
                if previous[2]:
                    line += " (truncated there)"
                if remaining is not None and len(line) > remaining:
                    break
                rows.append(line)
                used += len(line) + 1
                rendered += 1
                continue
            core = header
            if (claim := task_claim(item)) and claim["agent_id"] != owner:
                core += f"; claimed by {claim['agent_id']}"
            if record_notes and (note := record_notes.get(identity)):
                core += f"; {note}"
            links = [("waiting", item.get("waiting_on", [])),
                     ("reconcile", item.get("obsolete_dependencies", []) + item.get("conflicts", [])),
                     ("replaces", item["supersedes"])]
            if detailed:
                links.extend((("parent", [item["parent"]] if item.get("parent") else []),
                              ("depends", item.get("depends_on", []))))
            if item.get("replaced_by"):
                links.append(("replaced_by", [item["replaced_by"]]))
            replacements = [reason.removeprefix("replacement:") for reason in item.get("reasons", [])
                            if reason.startswith("replacement:")]
            if replacements:
                links.append(("replacement for", replacements))
            roles: dict[str, list[str]] = {}
            for label, targets in links:
                for target in dict.fromkeys(targets):
                    roles.setdefault(target, []).append(label)
            groups: dict[str, list[str]] = {}
            for target, labels in roles.items():
                groups.setdefault("+".join(labels), []).append(target)
            for label, targets in groups.items():
                core += f"; {label}: " + ", ".join(targets)

            truncated = summary_only  # The full record includes attribution even when it has no other details.
            details = []
            emitted = {}
            location = f"{section} item {rendered + 1}" if section else f"item {rendered + 1}"
            fields = (("", "content", item["content"]),)
            if not summary_only:
                fields += (("next", "next", item["next_action"]), ("Why", "rationale", item["rationale"]),
                           ("Replacement reason", "replacement reason", item.get("replacement_reason", "")))
            for label, field, value in fields:
                if not value:
                    continue
                display = value
                reference = emitted.get(value) or self.texts.get(value)
                if reference is not None:
                    source_location, source_field = reference
                    pointer = f"same as {source_field} above" if source_location == location else f"same as {source_location} {source_field} above"
                    if len(pointer) < len(display):
                        display = pointer
                else:
                    emitted[value] = (location, field)
                details.append(f"{label}: {display}" if label else display)
            if not summary_only and item["files"]:
                details.append("files: " + ", ".join(item["files"]))
            detail = "; ".join(details)
            hint = "[truncated]" if compact else f"... [truncated; reql agent show {item['id']} --json]"
            suffix = " " + hint if truncated else ""
            if remaining is not None and len(core) + len(hint) + 2 > remaining:
                # Never present a partially cut dependency/conflict identifier as complete.
                line = header + "; coordination links omitted " + hint
                if len(line) > remaining:
                    break
                truncated = True
            elif remaining is not None and len(core) + len(detail) + len(suffix) + 2 > remaining:
                ending = " " + hint if compact else hint
                line = core + "; " + detail[:remaining - len(core) - len(ending) - 2] + ending
                truncated = True
            else:
                line = core + "; " + detail + suffix
                # References become valid only after every referenced field has
                # actually been emitted. Budget-cut rows cannot seed later pointers.
                self.texts.update(emitted)
                if complete_records is not None and not truncated:
                    complete_records.add(identity)
                if section is not None:
                    self.records[identity] = (item, section, truncated)
            rows.append(line)
            needs_drill_down |= compact and truncated
            # Only emitted headers can establish attribution for later rows.
            if alias and not summary_only:
                self.declared_owners.add(owner)
            used += len(line) + 1
            rendered += 1
        omitted = len(records) - rendered + int(payload.get("omitted", 0))
        if needs_drill_down:
            rows.append(drill_down)
        if omitted:
            advice = "refine the query" if compact else "refine context or increase --limit/budget"
            rows.append(f"- {omitted} matching record(s) omitted; {advice}.")
        elif not records:
            rows.append("No matching engineering records.")
        text = "\n".join(rows)
        if max_chars is None or len(text) <= max_chars:
            return text
        if complete_records is not None:
            complete_records.clear()
        return "Work context truncated; increase budget."[:max_chars]
