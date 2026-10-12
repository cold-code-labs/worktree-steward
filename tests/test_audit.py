import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import test_pilot
from audit import apply_audit, audit, integration_target
from wtpilot import PilotError, git


class AuditTests(unittest.TestCase):
    setUp = test_pilot.PilotTests.setUp
    tearDown = test_pilot.PilotTests.tearDown
    create = test_pilot.PilotTests.create
    integrate = test_pilot.PilotTests.integrate
    def external(self, name="old"):
        path = self.root / name
        git(self.repo, "worktree", "add", "-b", name, str(path), "main")
        return path

    def report(self):
        result = audit(self.pilot, repo_path=self.repo, refresh=True)
        return json.loads(Path(result["report"]).read_text())

    def test_audit_only_observes_then_apply_adopts_and_preserves_archive(self):
        path = self.external()
        (path / ".env").write_text("private ignored data")
        report = self.report()
        self.assertEqual(report["summary"]["eligible"], 1)
        self.assertEqual(self.pilot.summary()["tasks"], 0)
        self.assertTrue(path.is_dir())
        result = apply_audit(self.pilot, report)
        self.assertEqual(result["archived"], 1)
        self.assertFalse(path.exists())
        task_id = result["results"][0]["task_id"]
        self.pilot.restore(task_id)
        self.assertEqual((path / ".env").read_text(), "private ignored data")

    def test_dirty_unmerged_and_primary_are_preserved(self):
        dirty = self.external("dirty")
        (dirty / "new.txt").write_text("pending")
        unmerged = self.external("unmerged")
        (unmerged / "tracked.txt").write_text("feature")
        git(unmerged, "commit", "-am", "pending feature")
        report = self.report()
        self.assertEqual(report["summary"]["eligible"], 0)
        self.assertEqual(report["summary"]["reasons"]["pending_work"], 1)
        self.assertEqual(report["summary"]["reasons"]["commits_not_proven_integrated"], 1)
        self.assertEqual(report["summary"]["reasons"]["primary_worktree"], 1)

    def test_active_unmanaged_process_is_preserved(self):
        path = self.external()
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=path)
        try:
            report = self.report()
            self.assertEqual(report["summary"]["eligible"], 0)
            self.assertIn("active_process", report["worktrees"][0]["reasons"])
        finally:
            process.terminate()
            process.wait()

    def test_owner_claim_after_audit_blocks_apply(self):
        task = self.create()
        report = self.report()
        self.assertEqual(report["summary"]["eligible"], 1)
        self.pilot.claim(task["id"], "other-agent", os.getpid())
        result = apply_audit(self.pilot, report)
        self.assertEqual(result["archived"], 0)
        self.assertEqual(self.pilot.task(task["id"])["owner"], "other-agent")

    def test_head_changed_after_audit_blocks_apply(self):
        path = self.external()
        report = self.report()
        (path / "tracked.txt").write_text("new work")
        git(path, "commit", "-am", "new work")
        result = apply_audit(self.pilot, report)
        self.assertEqual(result["archived"], 0)
        self.assertEqual(self.pilot.summary()["tasks"], 0)
        self.assertTrue(path.is_dir())

    def test_remote_fetch_failure_never_uses_cached_integration(self):
        self.external()
        git(self.repo, "remote", "add", "origin", str(self.root / "missing-origin"))
        git(self.repo, "update-ref", "refs/remotes/origin/main", "main")
        report = self.report()
        self.assertEqual(report["summary"]["eligible"], 0)
        self.assertIn("integration_target_unavailable", report["summary"]["reasons"])
        with self.assertRaises(PilotError):
            integration_target(self.pilot.repository(self.repo), refresh=True)

    def test_cached_origin_requires_refresh_and_remote_rewrite_blocks_apply(self):
        remote = self.root / "remote.git"
        git(self.repo, "clone", "--bare", str(self.repo), str(remote))
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "fetch", "origin")
        path = self.external()
        cached = audit(self.pilot, repo_path=self.repo, refresh=False)
        self.assertEqual(cached["eligible"], 0)
        report = self.report()
        self.assertEqual(report["summary"]["eligible"], 1)
        (path / "tracked.txt").write_text("remote feature")
        git(path, "commit", "-am", "remote feature")
        git(self.repo, "merge", "--ff-only", "old")
        git(self.repo, "push", "origin", "main")
        report = self.report()
        git(remote, "update-ref", "refs/heads/main", "HEAD~1")
        result = apply_audit(self.pilot, report)
        self.assertEqual(result["archived"], 0)
        self.assertTrue(path.exists())

    def test_finish_expected_head_protects_gap_before_lifecycle_lock(self):
        task = self.create()
        old_head = task["base_sha"]
        self.integrate(task)
        with self.assertRaises(PilotError):
            self.pilot.finish(task["id"], "main", expected_head=old_head)

    def test_cli_module_uses_same_error_type_and_audit_entrypoint(self):
        self.external()
        cli = Path(__file__).resolve().parents[1] / "wtpilot.py"
        result = subprocess.run([sys.executable, str(cli), "--state", str(self.pilot.root),
                                 "--repo", str(self.repo), "audit", "--refresh"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["eligible"], 1)

    def test_app_owned_workspace_is_preserved_without_open_files(self):
        root = self.root / ".codex/worktrees"
        root.mkdir(parents=True)
        path = root / "app-task"
        git(self.repo, "worktree", "add", "-b", "app-task", str(path), "main")
        report = self.report()
        self.assertEqual(report["summary"]["eligible"], 0)
        self.assertIn("application_owned_workspace", report["summary"]["reasons"])

    def test_install_skills_is_identical_and_idempotent_for_all_three_agents(self):
        import contextlib
        import io
        import install
        with mock.patch.object(Path, "home", return_value=self.root), \
             mock.patch.dict(os.environ, {"WTPILOT_STATE": str(self.pilot.root)}), \
             mock.patch.object(sys, "argv", ["install.py", "--skills"]):
            first = io.StringIO()
            with contextlib.redirect_stdout(first):
                install.main()
            self.assertEqual(len(json.loads(first.getvalue())["changed"]), 6)
            for agent in (".codex", ".cursor", ".claude"):
                skill = self.root / agent / "skills/worktree-hygiene/SKILL.md"
                self.assertEqual(skill.read_text(), (install.SOURCE / "skills/worktree-hygiene/SKILL.md").read_text())
            second = io.StringIO()
            with contextlib.redirect_stdout(second):
                install.main()
            self.assertEqual(json.loads(second.getvalue())["changed"], [])
