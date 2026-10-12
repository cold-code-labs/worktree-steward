import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wtpilot import Pilot, PilotError, git, output, run_task

CLI = Path(__file__).resolve().parents[1] / "wtpilot.py"


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wtpilot-test-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Pilot Test")
        git(self.repo, "config", "user.email", "pilot@example.invalid")
        (self.repo / "tracked.txt").write_text("initial\n")
        (self.repo / ".gitignore").write_text(".env\nnode_modules/\nignored-link\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        self.pilot = Pilot(self.root / "state")
        self.pilot.policy.update(managed_root=str(self.root / "workspaces"),
                                 scan_roots=[str(self.root)], completed_retention_hours=0,
                                 discardable_ignored_directories=["node_modules"])
        self.pilot.policy_path.write_text(json.dumps(self.pilot.policy))

    def tearDown(self):
        self.pilot.close()
        self.temporary.cleanup()

    def create(self, key="feature", **kwargs):
        return self.pilot.new(self.repo, key, "main", **kwargs)

    def integrate(self, task):
        p = Path(task["path"])
        (p / "tracked.txt").write_text("change\n")
        git(p, "add", "tracked.txt")
        git(p, "commit", "-m", "feature")
        git(self.repo, "merge", "--ff-only", task["branch"])
        return self.pilot.finish(task["id"], "main")

    def test_same_task_reuses_identity(self):
        a, b = self.create(), self.create()
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b["reused"])
        self.assertEqual(len(self.pilot.inventory(self.pilot.repository(self.repo))), 2)

    def test_concurrent_creates_have_no_agent_limit_and_unique_slots(self):
        def create(i):
            result = subprocess.run([sys.executable, str(CLI), "--state", str(self.pilot.root),
                "--repo", str(self.repo), "new", f"task-{i}", "--base", "main"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            tasks = list(pool.map(create, range(8)))
        self.assertEqual(len({t["slot"] for t in tasks}), 8)
        self.assertEqual(len({t["id"] for t in tasks}), 8)
        self.assertIsNone(self.pilot.summary()["task_limit"])

    def test_concurrent_same_task_creates_once(self):
        def create(i):
            result = subprocess.run([sys.executable, str(CLI), "--state", str(self.pilot.root),
                "--repo", str(self.repo), "new", "same", "--base", "main"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["id"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(create, range(5)))
        self.assertEqual(len(set(ids)), 1)

    def test_failed_preparation_blocks_claim_and_can_retry(self):
        argv = [sys.executable, "-c", "from pathlib import Path; p=Path('.first-run'); exists=p.exists(); p.touch(); raise SystemExit(0 if exists else 7)"]
        with self.assertRaises(PilotError):
            self.create(setup=[argv])
        task = self.pilot.task("feature")
        self.assertEqual(task["state"], "failed")
        with self.assertRaises(PilotError):
            self.pilot.claim(task["id"], "worker")
        self.assertEqual(self.pilot.retry(task["id"])["state"], "ready")

    def test_two_live_owners_cannot_claim_one_task(self):
        task = self.create()
        self.pilot.claim(task["id"], "one", os.getpid())
        with self.assertRaises(PilotError):
            self.pilot.claim(task["id"], "two", os.getpid())
        with self.assertRaises(PilotError):
            self.pilot.heartbeat(task["id"], "two")
        self.pilot.release(task["id"], "one")
        self.assertEqual(self.pilot.claim(task["id"], "two", os.getpid())["owner"], "two")

    def test_expired_owner_becomes_interrupted_without_deletion(self):
        task = self.create()
        self.pilot.db.execute("""UPDATE tasks SET state='working',owner='dead',owner_pid=999999,
            owner_start='not alive',heartbeat=? WHERE id=?""", (time.time()-10000, task["id"]))
        self.pilot.doctor()
        self.assertEqual(self.pilot.task(task["id"])["state"], "interrupted")
        self.assertTrue(Path(task["path"]).exists())
        self.assertEqual(self.pilot.hygiene(True)["archived"], [])

    def test_live_pid_overrides_expired_heartbeat(self):
        task = self.create()
        self.pilot.claim(task["id"], "alive", os.getpid())
        self.pilot.db.execute("UPDATE tasks SET heartbeat=0 WHERE id=?", (task["id"],))
        self.pilot.doctor()
        self.assertEqual(self.pilot.task(task["id"])["state"], "working")

    def test_uncommitted_work_blocks_finish(self):
        task = self.create()
        (Path(task["path"]) / "tracked.txt").write_text("pending")
        with self.assertRaises(PilotError):
            self.pilot.finish(task["id"], "main")
        self.assertTrue(Path(task["path"]).is_dir())

    def test_untracked_work_blocks_archive(self):
        task = self.integrate(self.create())
        (Path(task["path"]) / "important.txt").write_text("keep")
        self.assertEqual(len(self.pilot.hygiene(True)["blocked"]), 1)
        self.assertTrue(Path(task["path"]).is_dir())

    def test_unmerged_commits_cannot_be_completed(self):
        task = self.create()
        (Path(task["path"]) / "tracked.txt").write_text("unmerged")
        git(task["path"], "commit", "-am", "unmerged")
        with self.assertRaises(PilotError):
            self.pilot.finish(task["id"], "main")

    def test_open_process_blocks_cleanup(self):
        task = self.integrate(self.create())
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=task["path"])
        try:
            time.sleep(0.1)
            with self.assertRaises(PilotError):
                self.pilot.archive(task["id"])
            self.assertTrue(Path(task["path"]).exists())
        finally:
            child.terminate(); child.wait()

    def test_archive_and_restore_preserve_ignored_assets(self):
        task = self.integrate(self.create())
        p = Path(task["path"])
        (p / ".env").write_text("TEST_SECRET=preserved\n")
        (p / "node_modules").mkdir()
        (p / "node_modules/generated.js").write_text("reinstallable")
        archived = self.pilot.archive(task["id"])
        self.assertEqual(archived["state"], "archived")
        self.assertFalse(p.exists())
        manifest = json.loads((Path(archived["archive"]) / "manifest.json").read_text())
        self.assertEqual(manifest["saved_ignored_files"], 1)
        self.assertEqual(manifest["discarded_dependency_files"], 1)
        self.assertEqual((Path(archived["archive"]) / "ignored-files.tar.gz").stat().st_mode & 0o777, 0o600)
        restored = self.pilot.restore(task["id"])
        self.assertEqual((p / ".env").read_text(), "TEST_SECRET=preserved\n")
        self.assertEqual((p / "tracked.txt").read_text(), "change\n")
        self.assertFalse((p / "node_modules").exists())
        self.assertEqual(restored["state"], "ready")

    def test_retention_and_dry_run(self):
        task = self.integrate(self.create())
        self.pilot.policy["completed_retention_hours"] = 24
        self.assertEqual(self.pilot.hygiene(True)["candidates"], [])
        self.pilot.policy["completed_retention_hours"] = 0
        self.assertEqual(self.pilot.hygiene(False)["candidates"], [task["id"]])
        self.assertTrue(Path(task["path"]).exists())

    def test_locked_worktree_is_preserved(self):
        task = self.integrate(self.create())
        git(self.repo, "worktree", "lock", task["path"])
        with self.assertRaises(PilotError):
            self.pilot.archive(task["id"])

    def test_committed_changes_after_completion_are_preserved(self):
        task = self.integrate(self.create())
        (Path(task["path"]) / "tracked.txt").write_text("later work")
        git(task["path"], "commit", "-am", "later")
        with self.assertRaises(PilotError):
            self.pilot.archive(task["id"])
        self.assertTrue(Path(task["path"]).exists())

    def test_interrupted_preparation_is_retryable(self):
        task = self.create()
        self.pilot.db.execute("UPDATE tasks SET state='preparing' WHERE id=?", (task["id"],))
        self.pilot.doctor()
        self.assertEqual(self.pilot.task(task["id"])["state"], "failed")
        self.assertEqual(self.pilot.retry(task["id"])["state"], "ready")

    def test_archive_crash_after_removal_is_reconciled(self):
        task = self.integrate(self.create())
        self.pilot.archive(task["id"])
        self.pilot.db.execute("UPDATE tasks SET state='archiving',slot=0 WHERE id=?", (task["id"],))
        self.pilot.doctor()
        restored_state = self.pilot.task(task["id"])
        self.assertEqual(restored_state["state"], "archived")
        self.assertIsNone(restored_state["slot"])

    def test_branch_changed_outside_pilot_blocks_archive(self):
        task = self.integrate(self.create())
        git(task["path"], "switch", "-c", "another")
        with self.assertRaises(PilotError):
            self.pilot.archive(task["id"])

    def test_external_workspaces_remain_observation_only(self):
        external = self.root / "external"
        git(self.repo, "worktree", "add", "-b", "external", str(external), "main")
        self.pilot.scan([self.root])
        self.assertEqual(self.pilot.summary()["worktrees"], 2)
        task = self.pilot.adopt(external, "external")
        self.assertEqual(task["state"], "observed")
        with self.assertRaises(PilotError):
            self.pilot.finish(task["id"], "main")
        self.assertEqual(self.pilot.hygiene(True)["archived"], [])
        self.assertTrue(external.exists())

    def test_conflicts_include_uncommitted_and_untracked_changes(self):
        a, b = self.create("a"), self.create("b")
        for task in (a, b):
            (Path(task["path"]) / "tracked.txt").write_text(task["id"])
            (Path(task["path"]) / "new.txt").write_text(task["id"])
        files = {x["file"] for x in self.pilot.conflicts(self.repo)["overlaps"]}
        self.assertEqual(files, {"tracked.txt", "new.txt"})

    def test_run_injects_resources_and_releases_owner(self):
        task = self.create()
        code = run_task(self.pilot, task["id"], [sys.executable, "-c",
            "import os; assert os.environ['PORT']; assert os.environ['COMPOSE_PROJECT_NAME'].startswith('wt_')"], None)
        self.assertEqual(code, 0)
        self.assertEqual(self.pilot.task(task["id"])["state"], "ready")

    def test_run_refuses_occupied_port(self):
        task = self.create()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", task["port"]))
            with self.assertRaises(PilotError):
                run_task(self.pilot, task["id"], [sys.executable, "-c", "pass"], None)
        self.assertEqual(self.pilot.task(task["id"])["state"], "ready")

    def test_script_adapter_is_only_creation_and_removal_path(self):
        scripts = self.repo / "scripts"; scripts.mkdir()
        script = scripts / "wt.sh"
        script.write_text('''#!/bin/bash
# WTPILOT_SCRIPT_CONTRACT_V1
set -euo pipefail
test "$WTPILOT_DELEGATED" = 1
printf '%s\\n' "$1" >> "$WTPILOT_WORKTREE_ROOT/calls"
case "$1" in
 new) git worktree add -b "wt/$2" "$WTPILOT_WORKTREE_ROOT/$2" origin/main ;;
 rm) git worktree remove "$WTPILOT_WORKTREE_ROOT/$2"; git branch -d "wt/$2" ;;
esac
''')
        git(self.repo, "add", "scripts/wt.sh"); git(self.repo, "commit", "-m", "script")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        adapter_root = self.root / "script-worktrees"; adapter_root.mkdir()
        old = os.environ.get("WTPILOT_WORKTREE_ROOT")
        os.environ["WTPILOT_WORKTREE_ROOT"] = str(adapter_root)
        try:
            task = self.pilot.new(self.repo, "script-task", "origin/main")
            task = self.pilot.finish(task["id"], "origin/main")
            self.pilot.archive(task["id"])
            self.assertEqual((adapter_root / "calls").read_text().splitlines(), ["new", "rm"])
            self.assertTrue(output(git(self.repo, "rev-parse", "refs/wtpilot/archive/" + task["id"])))
        finally:
            if old is None: os.environ.pop("WTPILOT_WORKTREE_ROOT", None)
            else: os.environ["WTPILOT_WORKTREE_ROOT"] = old

    def test_script_archive_remembers_workspace_root(self):
        scripts = self.repo / "scripts"; scripts.mkdir()
        script = scripts / "wt.sh"
        script.write_text('''#!/bin/bash
# WTPILOT_SCRIPT_CONTRACT_V1
set -euo pipefail
case "$1" in
 new) git worktree add -b "wt/$2" "$WTPILOT_WORKTREE_ROOT/$2" origin/main ;;
 rm) git worktree remove "$WTPILOT_WORKTREE_ROOT/$2" ;;
esac
''')
        git(self.repo, "add", "scripts/wt.sh"); git(self.repo, "commit", "-m", "script")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        with mock.patch.dict(os.environ, {"WTPILOT_WORKTREE_ROOT": str(self.root / "custom-root")}):
            task = self.pilot.new(self.repo, "custom", "origin/main")
            self.pilot.finish(task["id"], "origin/main")
        # Cleanup runs with a different environment, as a LaunchAgent would.
        with mock.patch.dict(os.environ, {"WTPILOT_WORKTREE_ROOT": str(self.root / "wrong-root")}):
            self.pilot.archive(task["id"])
        self.assertFalse(Path(task["path"]).exists())

    def test_ignored_files_changed_during_snapshot_block_removal(self):
        task = self.integrate(self.create())
        env_path = Path(task["path"]) / ".env"
        env_path.write_text("original")
        original = self.pilot.safety
        count = 0
        def safety(t):
            nonlocal count
            count += 1
            if count == 2:
                env_path.write_text("changed while archiving")
            return original(t)
        with mock.patch.object(self.pilot, "safety", safety):
            with self.assertRaises(PilotError):
                self.pilot.archive(task["id"])
        self.assertEqual(env_path.read_text(), "changed while archiving")

    def test_unknown_script_prevents_management_without_running_it(self):
        scripts = self.repo / "scripts"; scripts.mkdir()
        marker = self.root / "script-executed"
        (scripts / "wt.sh").write_text(f'#!/bin/bash\ntouch "{marker}"\n')
        with self.assertRaises(PilotError):
            self.create()
        self.assertFalse(marker.exists())
        self.assertEqual(len(self.pilot.inventory(self.pilot.repository(self.repo))), 1)

    def test_default_archive_keeps_dependency_assets(self):
        self.pilot.policy["discardable_ignored_directories"] = []
        task = self.integrate(self.create())
        directory = Path(task["path"]) / "node_modules"
        directory.mkdir()
        (directory / "custom-asset.txt").write_text("cannot assume reproducible")
        self.pilot.archive(task["id"])
        restored = self.pilot.restore(task["id"])
        self.assertEqual((Path(restored["path"]) / "node_modules/custom-asset.txt").read_text(),
                         "cannot assume reproducible")

    def test_example_script_checks_prepared_removal_and_retains_branch(self):
        import shlex
        scripts = self.repo / "scripts"; scripts.mkdir()
        (scripts / "wt.sh").write_bytes((CLI.parent / "examples/wt.sh").read_bytes())
        git(self.repo, "add", "scripts/wt.sh"); git(self.repo, "commit", "-m", "adapter")
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        launcher = self.root / "test-cli"
        launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                            + shlex.quote(str(CLI)) + ' "$@"\n')
        launcher.chmod(0o755)
        with mock.patch.dict(os.environ, {"WTPILOT_COMMAND": str(launcher),
                "WTPILOT_WORKTREE_ROOT": str(self.root / "script-workspaces"),
                "WTPILOT_STATE": str(self.pilot.root)}):
            creation = subprocess.run(["bash", str(scripts / "wt.sh"), "new", "sample"],
                                      capture_output=True, text=True)
            self.assertEqual(creation.returncode, 0, creation.stderr)
            task = json.loads(creation.stdout)
            # The script cannot remove a task without an actual prepared archive.
            refusal = subprocess.run(["bash", str(scripts / "wt.sh"), "rm", "sample"],
                env={**os.environ, "WTPILOT_DELEGATED": "1", "WTPILOT_REMOVAL_TASK": task["id"]},
                capture_output=True, text=True)
            self.assertNotEqual(refusal.returncode, 0)
            self.assertTrue(Path(task["path"]).exists())
            self.pilot.finish(task["id"], "origin/main")
            self.pilot.archive(task["id"])
            self.assertFalse(Path(task["path"]).exists())
            self.assertEqual(output(git(self.repo, "rev-parse", task["branch"])), task["base_sha"])



if __name__ == "__main__":
    unittest.main()
