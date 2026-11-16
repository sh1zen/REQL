"""Exercise assigned-work and record flows through the public entry points."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from agents.install import install_agent_files
from memory.agent import AgentWorkspace
from memory.cli import main
from memory.services.coordination import CoordinationStore


class AgentWorkWorkflowTests(unittest.TestCase):
    """Protect revision, dependency, provenance and recovery semantics."""

    def setUp(self) -> None:
        scratch=Path('.tmp')/'regression-tests';scratch.mkdir(parents=True,exist_ok=True)
        self.temp=TemporaryDirectory(dir=scratch);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'project';self.root.mkdir()
        self.storage=self.root/'.reql/memory.reql';self.storage.parent.mkdir()
        self.storage.write_bytes(b'Canonical graph must not be opened by work operations')
        self.work=CoordinationStore(self.storage.with_name('agent-dashboard.reql'))

    def agent(self,name: str) -> AgentWorkspace:
        """Use explicit independent identities and activities."""
        return AgentWorkspace(self.storage,agent_id=f'agent:{name}',activity_id=name)

    def record_plan(self, coordinator: AgentWorkspace, *, managed: bool = False) -> dict[str, dict]:
        """Save a goal and link its decision and dependent tasks by their returned ids."""
        goal = coordinator.record("goal", "Keep wire compatibility", key="wire-goal")["node"]
        contract = coordinator.record(
            "decision", "Retain schema", rationale="Existing consumer", parent=goal["id"],
        )["node"]
        status = "in_progress" if managed else "open"
        producer = coordinator.record(
            "task", "Update producer", parent=goal["id"], depends_on=[contract["id"]],
            status=status, next_action="Run producer tests",
        )["node"]
        consumer = coordinator.record(
            "task", "Update consumer", parent=goal["id"], depends_on=[contract["id"]], status=status,
        )["node"]
        integration = coordinator.record(
            "task", "Verify wire integration", parent=goal["id"], depends_on=[producer["id"], consumer["id"]],
        )["node"]
        return {"goal": goal, "contract": contract, "producer": producer, "consumer": consumer, "integration": integration}

    def cli(self,*args: str,expected: int=0) -> subprocess.CompletedProcess:
        """Run the real entrypoint with a local project and no provider access."""
        env=dict(os.environ,PYTHONIOENCODING='utf-8',REQL_AGENT_ID='agent:cli',REQL_AGENT_ACTIVITY_ID='cli')
        result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'cli.py'),*args],
                              cwd=self.root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=40)
        self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
        return result

    def test_linked_plan_claim_complete_and_dependency_guard(self) -> None:
        coordinator = self.agent("coordinator")
        coordinator.init("Plan wire")
        nodes = self.record_plan(coordinator)
        coordinator.finish("Contract agreed; workers remain")
        self.assertFalse(coordinator.exists())
        self.assertEqual(nodes['producer']['depends_on'],[nodes['contract']['id']])
        workers=[]
        for name in ('producer','consumer'):
            worker=self.agent(name);workers.append(worker)
            claimed=worker.init(name,task_id=nodes[name]['id'],expected_revision=1)
            self.assertEqual(claimed['claimed_task']['revision'],2)
            self.assertIn(nodes['contract']['id'],{row['id'] for row in claimed['work_context']['records']})
        coordinator.init('Integrate',task_id=nodes['integration']['id'],expected_revision=1)
        with self.assertRaisesRegex(ValueError,'waiting'):
            coordinator.finish('Cannot finish early',task_id=nodes['integration']['id'],expected_revision=2)
        self.assertTrue(coordinator.exists())
        self.assertEqual(self.work.show(nodes['integration']['id'])['status'],'in_progress')
        for name,worker in zip(('producer','consumer'),workers):
            finished=worker.finish('Owned contract tests pass',task_id=nodes[name]['id'],expected_revision=2)
            self.assertEqual(finished['completed_task']['revision'],3)
            self.assertEqual(finished['completed_task']['next_action'],'')
            self.assertEqual(finished['completed_task']['origin_agent_id'],'agent:coordinator')
            self.assertFalse(worker.exists())
        result=coordinator.finish('Integration passed',task_id=nodes['integration']['id'],expected_revision=2)
        self.assertEqual(result['completed_task']['status'],'done')
        self.assertTrue({'agent_id', 'status', 'closed_session_id', 'context', 'retention', 'completed_task'}.issubset(result))
        checkpoint = self.work.show(result['checkpoint']['id'])
        self.assertEqual(result['checkpoint'], {key: checkpoint[key] for key in ('id', 'revision', 'status')})
        self.assertIn(nodes['integration']['id'], checkpoint['summarizes'])
        self.assertFalse(coordinator.exists())
        self.assertEqual(self.storage.read_bytes(),b'Canonical graph must not be opened by work operations')

    def test_coordinator_accepts_managed_lanes_without_worker_sessions(self) -> None:
        """Accept worker evidence in dependency order without duplicating private sessions."""
        coordinator=self.agent('coordinator')
        coordinator.init("Dispatch")
        nodes = self.record_plan(coordinator, managed=True)
        coordinator.finish("Managed workers assigned")
        coordinator.init("Review")
        integration = nodes["integration"]
        with self.assertRaisesRegex(ValueError, "waiting"):
            coordinator.record(record_id=integration["id"], expected_revision=1, status="done",
                               rationale="Cannot accept early", next_action="")
        self.assertTrue(coordinator.exists())
        self.assertEqual(self.work.show(integration["id"])["revision"], 1)

        for name in ("producer", "consumer", "integration", "goal"):
            node = nodes[name]
            result = coordinator.record(record_id=node["id"], expected_revision=node["revision"],
                                        status="done", next_action="", rationale=f"Accepted {name} checks and source review")
            self.assertEqual(result["node"]["revision"], 2)
        coordinator.finish("Reviewed worker outcomes and integration checks")
        self.assertFalse(coordinator.exists())
        for name in ('producer','consumer','integration','goal'):
            node=self.work.show(nodes[name]['id'])
            self.assertEqual(node['status'],'done')
            self.assertEqual(node['revision'],2)
            self.assertEqual(node['next_action'],'')
            self.assertEqual(node['agent_id'],'agent:coordinator')
            self.assertIn('Accepted',node['rationale'])
        self.assertEqual({item['agent_id'] for item in self.work.records()},{'agent:coordinator'})
        self.assertEqual(self.storage.read_bytes(),b'Canonical graph must not be opened by work operations')

    def test_parallel_cli_claims_and_restart_recover_shared_work(self) -> None:
        """Exercise overlapping queue claims and handoff across independent CLI invocations."""
        self.storage.unlink()
        (self.root / "codec.py").write_text("def encode(value):\n    return value\n", encoding="utf-8")
        self.cli("project", "compile")
        planner = self.agent("planner")
        planner.init("Plan work")
        decision = planner.record("decision", "Keep the wire format", rationale="Existing consumers")["node"]
        tasks = [planner.add_task(
            name, workstream="wire", files=["codec.py"], depends_on=[decision["id"]],
        )["node"] for name in ("Test codec inputs", "Test codec outputs")]
        integration = planner.add_task("Integrate codec", depends_on=[task["id"] for task in tasks])["node"]

        gate = Barrier(2)

        def claim(name: str) -> dict:
            gate.wait(timeout=10)
            result = self.cli("agent", "--agent", f"agent:{name}", "--activity", name, "init", "--next", "--workstream", "wire",
                              "--query", "encode", "--json")
            return json.loads(result.stdout)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(claim, ("first", "second"))
        self.assertCountEqual([first["claimed_task"]["id"], second["claimed_task"]["id"]],
                              [task["id"] for task in tasks])
        self.assertEqual(sum(bool(result["file_overlaps"]["files"]) for result in (first, second)), 1)
        self.assertTrue(all(result["source_context"]["payload"]["engineering_context"] for result in (first, second)))

        original = first["claimed_task"]
        task_id = original["id"]
        progress = planner.record(record_id=task_id, expected_revision=original["revision"],
                                  rationale="Focused check passed", next_action="Review downstream usage")["node"]
        self.assertEqual(progress["agent_id"], planner.agent_id)
        self.assertEqual(progress["claim"], original["claim"])
        with self.assertRaisesRegex(ValueError, "already claimed"):
            planner.record(record_id=task_id, expected_revision=progress["revision"], status="done")
        restarted = json.loads(self.cli("agent", "--agent", "agent:first", "--activity", "first", "init", "--task", task_id, "--json").stdout)
        self.assertEqual(restarted["assignment_status"], "resumed")
        self.assertEqual(restarted["claimed_task"], progress)
        blocked = json.loads(self.cli(
            "agent", "--agent", "agent:first", "--activity", "first", "task", "create", "Check a downstream client", "--parent", task_id,
            "--status", "blocked", "--why", "Fixture missing", "--next", "Get the fixture", "--json",
        ).stdout)["node"]
        closed = json.loads(self.cli("agent", "--agent", "agent:first", "--activity", "first", "finish", "Downstream review remains", "--json").stdout)
        self.assertEqual(closed["released_claims"], 1)
        self.assertEqual(self.work.show(second["claimed_task"]["id"]), second["claimed_task"])

        recovered = json.loads(self.cli("project", "context", "--task", closed["checkpoint"]["id"], "--json").stdout)
        rows = {row["id"]: row for row in recovered["records"]}
        self.assertEqual(rows[task_id]["status"], "open")
        self.assertIsNone(rows[task_id]["claim"])
        self.assertEqual(rows[task_id]["next_action"], progress["next_action"])
        self.assertEqual(rows[task_id]["rationale"], progress["rationale"])
        self.assertEqual(rows[blocked["id"]]["status"], "blocked")
        self.assertEqual(rows[decision["id"]]["rationale"], decision["rationale"])
        fresh = json.loads(self.cli("agent", "--agent", "agent:successor", "--activity", "successor", "init", "--task", task_id, "--json").stdout)
        self.assertNotEqual(fresh["claimed_task"]["claim"], original["claim"])
        self.assertEqual(self.work.context(record_id=integration["id"])["records"][0]["execution_state"], "blocked")
        self.cli("agent", "--agent", "agent:successor", "--activity", "successor", "finish", "Downstream review passed", "--task", task_id)
        self.cli("agent", "--agent", "agent:second", "--activity", "second", "finish", "Codec check passed", "--task", second["claimed_task"]["id"])
        self.assertEqual(self.work.context(record_id=integration["id"])["records"][0]["execution_state"], "ready")

    def test_finish_rolls_back_shared_changes_and_can_retry_private_close(self) -> None:
        """Preserve claims on failed commits and never repeat a committed release."""
        worker = self.agent("worker")
        worker.init("Review")
        selected = worker.add_task("Checked result")["node"]["id"]
        unfinished = worker.add_task("Follow-up", next_action="Read the fixture")["node"]["id"]
        worker.init(task_id=selected)
        worker.init(task_id=unfinished)
        claimed = [self.work.show(task_id) for task_id in (selected, unfinished)]
        save = CoordinationStore._save_record

        def fail_checkpoint(store, block, item_id, props, *args):
            if props["kind"] == "checkpoint":
                raise RuntimeError("checkpoint write interrupted")
            return save(store, block, item_id, props, *args)

        with patch.object(CoordinationStore, "_save_record", fail_checkpoint):
            with self.assertRaisesRegex(RuntimeError, "checkpoint write interrupted"):
                worker.finish("Checked result", task_id=selected)
        self.assertEqual([self.work.show(task_id) for task_id in (selected, unfinished)], claimed)
        self.assertTrue(worker.exists())

        require_agent = worker._require_agent

        def fail_close(*, read_only=False):
            if not read_only:
                raise RuntimeError("private close interrupted")
            return require_agent(read_only=True)

        with patch.object(worker, "_require_agent", side_effect=fail_close):
            with self.assertRaisesRegex(RuntimeError, "private close interrupted"):
                worker.finish("Checked result", task_id=selected)
        self.assertEqual(self.work.show(selected)["status"], "done")
        self.assertEqual(self.work.show(unfinished)["status"], "open")
        with self.assertRaisesRegex(ValueError, "finish already committed"):
            worker.init(task_id=unfinished)
        new_claim = self.agent("successor").init(task_id=unfinished)["claimed_task"]
        retried = self.agent("worker").finish("Checked result", task_id=selected, before_finish=self.fail)
        self.assertEqual(retried["completed_task"]["revision"], claimed[0]["revision"] + 1)
        self.assertEqual(retried["released_claims"], 1)
        self.assertEqual(self.work.show(unfinished), new_claim)
        self.assertFalse(worker.exists())

    def test_partial_cli_updates_use_the_current_task_revision(self) -> None:
        """Update outcomes by id and preserve omitted fields and idempotent retries."""
        self.cli("agent", "init", "--name", "Create")
        create = ("agent", "task", "create", "Preserve this detailed contract", "--key", "contract",
                  "--workstream", "wire", "--file", "src/codec.py", "--next", "Run checks", "--json")
        created = json.loads(self.cli(*create).stdout)
        node = created["node"]
        self.assertTrue(created["created"])
        retried = json.loads(self.cli(*create).stdout)
        self.assertFalse(retried["changed"])
        self.assertEqual(retried["node"], node)

        update = ("agent", "task", "update", "--id", node["id"], "--status", "done",
                  "--why", "Owned regression checks passed", "--next=", "--json")
        updated = json.loads(self.cli(*update).stdout)["node"]
        self.assertEqual(updated["revision"], 2)
        saved = self.work.show(node["id"])
        self.assertEqual(saved["kind"], "task")
        self.assertEqual(saved["content"], "Preserve this detailed contract")
        self.assertEqual(saved["workstream"], "wire")
        self.assertEqual(saved["files"], ["src/codec.py"])
        self.assertEqual(saved["next_action"], "")
        repeated = json.loads(self.cli(*update).stdout)
        self.assertFalse(repeated["changed"])
        self.assertEqual(self.work.show(node["id"])["revision"], 2)

        replaced = self.cli("agent", "task", "update", "Revised contract", "--id", node["id"])
        self.assertEqual(replaced.stdout.splitlines(), [f"{node['id']}\ttask\tdone", "Revision: 3"])
        self.assertEqual(self.work.show(node["id"])["content"], "Revised contract")

    def test_task_update_rejects_changes_after_reading_the_revision(self) -> None:
        """The CLI's automatic snapshot remains a compare-and-set guard."""
        self.cli("agent", "init", "--name", "Update")
        task = json.loads(self.cli("agent", "task", "create", "Original contract", "--json").stdout)["node"]
        show = CoordinationStore.show

        def change_after_read(store: CoordinationStore, record_id: str) -> dict:
            snapshot = show(store, record_id)
            store.put(record_id=record_id, expected_revision=snapshot["revision"], content="Concurrent contract",
                      agent_id="agent:concurrent", session_id=None)
            return snapshot

        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with (patch.object(CoordinationStore, "show", change_after_read), redirect_stdout(StringIO()) as output,
              redirect_stderr(StringIO())):
            self.assertEqual(main(["agent", "--agent", "agent:cli", "--activity", "cli",
                                   "task", "update", "Conflicting contract", "--id", task["id"], "--json"]), 2)
        self.assertIn("revision changed", json.loads(output.getvalue())["error"]["message"])
        current = self.work.show(task["id"])
        self.assertEqual((current["content"], current["revision"]), ("Concurrent contract", 2))

    def test_parallel_claim_accepts_one_owner_for_open_work(self) -> None:
        """A race on the observed revision can publish only one claim."""
        planner = self.agent("planner")
        planner.init("Plan")
        task = planner.add_task("Claim serializer work")["node"]

        def claim(name: str) -> str:
            try:
                self.agent(name).init("Claim work", task_id=task["id"], expected_revision=1)
                return "accepted"
            except ValueError as exc:
                self.assertIn("revision changed", str(exc))
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("one", "two")))
        self.assertCountEqual(results, ["accepted", "rejected"])
        saved = self.work.show(task["id"])
        self.assertEqual(saved["revision"], 2)
        self.assertIn(saved["agent_id"], {"agent:one", "agent:two"})

    def test_cli_requires_release_before_another_session_claims_the_task(self) -> None:
        """Reject reassignment and finish by a foreign session until the coordinator releases work."""
        owner = ("agent", "--agent", "agent:cli", "--activity", "cli")
        peer = ("agent", "--agent", "agent:peer", "--activity", "peer")
        same_agent = ("agent", "--agent", "agent:cli", "--activity", "different")
        self.cli(*owner, "init", "--name", "Plan")
        task = self.agent("cli").add_task("Update serializer")["node"]
        task_id = task["id"]
        claim_args = ("init", "--task", task_id, "--json")
        claimed = json.loads(self.cli(*owner, *claim_args).stdout)["claimed_task"]
        self.assertEqual(json.loads(self.cli(*owner, *claim_args).stdout)["claimed_task"], claimed)

        for contender in (peer, same_agent):
            with self.subTest(contender=contender):
                error = json.loads(self.cli(*contender, *claim_args, expected=2).stdout)["error"]
                self.assertIn("already claimed by agent:cli", error["message"])
                self.assertIn("--status open", error["message"])
                self.assertEqual(self.work.show(task_id), claimed)
        rejected = self.cli(*peer, "init", "--task", task_id, expected=2)
        self.assertIn("--status open", rejected.stderr)
        self.assertNotIn("read command help", rejected.stderr)
        self.assertFalse(self.agent("peer").exists())

        self.cli(*owner, "task", "update", "--id", task_id, "--status", "open", "--why", "Agreed handoff")
        opened = self.work.show(task_id)
        self.assertEqual((opened["status"], opened["revision"]), ("open", claimed["revision"] + 1))
        with self.assertRaisesRegex(ValueError, "needs a claim"):
            self.agent("cli").finish("Premature finish", task_id=task_id, before_finish=self.fail)
        transferred = json.loads(self.cli(*peer, *claim_args).stdout)["claimed_task"]
        self.assertEqual((transferred["agent_id"], transferred["revision"]), ("agent:peer", opened["revision"] + 1))

        with self.assertRaisesRegex(ValueError, "already claimed by agent:peer"):
            self.agent("cli").finish("Stale finish", task_id=task_id, before_finish=self.fail)
        error = json.loads(self.cli(*owner, "finish", "Stale finish", "--task", task_id, "--json", expected=2).stdout)
        self.assertIn("already claimed by agent:peer", error["error"]["message"])
        self.assertEqual(self.work.show(task_id), transferred)
        self.assertTrue(self.agent("cli").exists())
        finished = json.loads(self.cli(*peer, "finish", "Serializer checked", "--task", task_id, "--json").stdout)
        self.assertEqual(finished["completed_task"]["status"], "done")

    def test_search_rejects_unused_flags_and_keeps_limited_results(self) -> None:
        """Exercise search output and the rejected options through the CLI parser."""
        self.cli("agent", "init", "--name", "Searchable work")
        self.cli("agent", "note", "Coordination cue one")
        self.cli("agent", "note", "Coordination cue two")
        results = json.loads(self.cli("agent", "search", "coordination cue", "--limit", "1", "--json").stdout)
        self.assertEqual(len(results["results"]), 1)
        self.assertIn("Coordination cue", results["results"][0]["context"])

        for option, values in (("--type", ["task"]), ("--status", ["open"]), ("--metadata", [])):
            with self.subTest(option=option):
                rejected = self.cli("agent", "search", "coordination cue", option, *values, expected=2)
                self.assertIn(f"unrecognized arguments: {option}", rejected.stderr)

    def test_partial_single_updates_preserve_validation_and_dependency_guards(self) -> None:
        """Optional positional text must not bypass creation, CAS or completion rules."""
        self.cli('agent', 'init', '--name', 'Guard partial updates')
        question = json.loads(self.cli('agent', 'record', 'question', 'Which wire version?', '--json').stdout)['node']
        task = json.loads(self.cli('agent', 'task', 'create', 'Verify consumer', '--depends-on',
                                   question['id'], '--json').stdout)['node']
        before = self.work.records()
        task_update = ('task', 'update', '--id', task['id'])
        cases = [
            ((*task_update, '--status', 'done'), 'waiting'),
            ((*task_update, '--status', 'resolved'), 'waiting'),
            (('task', 'update', '--id', question['id']), 'kind cannot change'),
            (('record', '--id', task['id'], '--revision', '1', '--next', 'Run checks'), "Work kind 'task'"),
            (('record', 'decision', '--id', task['id'], '--revision', '1'), 'kind cannot change'),
            (('record',), 'kind and content'),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                rejected = self.cli('agent', *arguments, '--json', expected=2)
                self.assertIn(message, json.loads(rejected.stdout)['error']['message'])
                self.assertEqual(self.work.records(), before)

        parse_errors = [
            (('record', 'task', 'Old syntax'), "invalid choice: 'task'"),
            (('task', 'create'), 'content'),
            (('task', 'update'), '--id'),
            (('task', 'diff', '--id', task['id']), "invalid choice: 'diff'"),
            ((*task_update, '--revision', '1'), 'unrecognized arguments: --revision'),
            (('init', '--task', task['id'], '--revision', '1'), 'unrecognized arguments: --revision'),
            (('finish', 'Verified', '--task', task['id'], '--revision', '1'), 'unrecognized arguments: --revision'),
            (('finish', 'Verified', '--task', task['id'], '--reviewed', 'old-token'), 'unrecognized arguments: --reviewed'),
            (('finish', 'Verified', '--task', task['id'], '--review'), 'unrecognized arguments: --review'),
        ]
        for arguments, message in parse_errors:
            with self.subTest(arguments=arguments):
                self.assertIn(message, self.cli('agent', *arguments, expected=2).stderr)
                self.assertEqual(self.work.records(), before)

        updated = json.loads(self.cli('agent', 'record', '--id', question['id'], '--revision', '1',
                                      '--next', 'Ask product', '--json').stdout)['node']
        self.assertEqual((updated['kind'], updated['content'], updated['next_action']),
                         ('question', question['content'], 'Ask product'))
        self.cli('agent', 'finish', 'Question and dependent work remain')

    def test_finish_checkpoint_recovers_current_linked_records(self) -> None:
        """Recover current dependency state through a finished plan's checkpoint."""
        self.cli("agent", "init", "--name", "Plan")
        coordinator = self.agent("cli")
        nodes = self.record_plan(coordinator)
        payload = json.loads(self.cli("agent", "finish", "Workers remain", "--json").stdout)
        checkpoint = payload["checkpoint"]
        saved = self.work.show(checkpoint["id"])
        self.assertEqual(checkpoint, {key: saved[key] for key in ("id", "revision", "status")})
        self.assertEqual(saved["kind"], "checkpoint")
        ids = {node["id"] for node in nodes.values()}
        self.assertEqual(set(saved["summarizes"]), ids)
        self.assertFalse(coordinator.exists())

        resumed = ("agent", "--activity", "resumed")
        initialized = json.loads(self.cli(*resumed, "init", "--name", "Reconcile", "--json").stdout)
        self.assertNotIn("checkpoint", initialized)
        recovered = json.loads(self.cli("project", "context", "--task", checkpoint["id"], "--json").stdout)
        self.assertTrue(ids.issubset({record["id"] for record in recovered["records"]}))
        producer = nodes["producer"]
        self.cli(*resumed, "task", "update", "--id", producer["id"], "--status", "blocked",
                 "--why", "Await consumer decision")
        self.cli(*resumed, "finish", "Producer blocked")
        recovered = json.loads(self.cli("project", "context", "--task", checkpoint["id"], "--json").stdout)
        current = next(record for record in recovered["records"] if record["id"] == producer["id"])
        self.assertEqual((current["revision"], current["status"]), (2, "blocked"))
        self.assertEqual(self.storage.read_bytes(), b"Canonical graph must not be opened by work operations")

    @unittest.skipUnless(os.name=='nt' and shutil.which('powershell'),'Needs native Windows PowerShell')
    def test_native_powershell_shim_can_clear_next_without_empty_argument(self) -> None:
        installed=install_agent_files(('codex',),project=True,project_dir=self.root,command_dir=self.root/'bin',hooks=False)
        command=next(item.path for item in installed.actions if item.kind=='command')
        self.cli('agent','init','--name','Question')
        question=json.loads(self.cli('agent','record','question','Budget includes first send?','--next','Ask product','--json').stdout)['node']
        quote=lambda value:"'"+str(value).replace("'","''")+"'"
        script=f"$env:REQL_AGENT_ID='agent:cli'; $env:REQL_AGENT_ACTIVITY_ID='cli'; & {quote(command)} agent record question 'Budget includes first send?' --id {quote(question['id'])} --revision 1 --status resolved --why 'Confirmed total budget' --next=; exit $LASTEXITCODE"
        result=subprocess.run([shutil.which('powershell'),'-NoProfile','-Command',script],cwd=self.root,
                              capture_output=True,text=True,timeout=40)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.work.show(question['id'])['next_action'],'')


if __name__=='__main__':unittest.main()
