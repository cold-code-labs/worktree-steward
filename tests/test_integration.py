import json
import os
import shlex
import sys
from pathlib import Path
import unittest

import test_pilot
from audit import audit, apply_audit
from integration import prove, verify, delta
from wtpilot import PilotError, git, output


class IntegrationTests(unittest.TestCase):
    setUp = test_pilot.PilotTests.setUp
    tearDown = test_pilot.PilotTests.tearDown
    create = test_pilot.PilotTests.create

    def feature(self):
        task = self.create()
        path = Path(task["path"])
        (path / "tracked.txt").write_text("first change\n")
        git(path, "commit", "-am", "first")
        (path / "tracked.txt").write_text("complete change\n")
        (path / "binary.dat").write_bytes(bytes(range(256)))
        git(path, "add", ".")
        git(path, "commit", "-m", "second")
        return task

    def squash(self, task):
        git(self.repo, "merge", "--squash", task["branch"])
        git(self.repo, "commit", "-m", "integrated squash")
        return output(git(self.repo, "rev-parse", "HEAD"))

    def proof(self, task):
        return prove(self.repo, output(git(task["path"], "rev-parse", "HEAD")),
                     output(git(self.repo, "rev-parse", "main")), True)

    def test_multicommit_squash_with_unrelated_parent_changes_is_exact(self):
        task = self.feature()
        (self.repo / "unrelated.txt").write_text("other work")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "unrelated")
        squash = self.squash(task)
        proof = self.proof(task)
        self.assertEqual(proof["kind"], "exact_squash")
        self.assertEqual(proof["squash_commit"], squash)
        self.assertTrue(verify(self.repo, proof, proof["head"], proof["target"]))
        self.assertIsNone(prove(self.repo, proof["head"], proof["target"]))

    def test_squash_archival_restore_and_persisted_proof(self):
        task = self.feature()
        (Path(task["path"]) / ".env").write_text("preserved local file")
        self.squash(task)
        with self.assertRaises(PilotError):
            self.pilot.finish(task["id"], "main")
        finished = self.pilot.finish(task["id"], "main", allow_squash=True)
        self.assertEqual(finished["integration_proof"]["kind"], "exact_squash")
        archived = self.pilot.archive(task["id"])
        manifest = json.loads((Path(archived["archive"]) / "manifest.json").read_text())
        self.assertEqual(manifest["task"]["integration_proof"], finished["integration_proof"])
        self.pilot.restore(task["id"])
        self.assertEqual((Path(task["path"]) / ".env").read_text(), "preserved local file")

    def test_partial_squash_and_whitespace_similarity_are_not_proof(self):
        task = self.feature()
        (self.repo / "tracked.txt").write_text("complete change\n")
        git(self.repo, "commit", "-am", "partial integration")
        self.assertIsNone(self.proof(task))
        (self.repo / "binary.dat").write_bytes(bytes(range(256)))
        (self.repo / "tracked.txt").write_text("complete  change\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "similar integration")
        self.assertIsNone(self.proof(task))

    def test_extra_work_after_squash_remains_unintegrated(self):
        task = self.feature()
        self.squash(task)
        path = Path(task["path"])
        (path / "extra.txt").write_text("pending committed work")
        git(path, "add", ".")
        git(path, "commit", "-m", "extra")
        self.assertIsNone(self.proof(task))

    def test_duplicate_squash_deltas_are_ambiguous(self):
        task = self.feature()
        squash = self.squash(task)
        git(self.repo, "revert", "--no-edit", squash)
        git(self.repo, "cherry-pick", squash)
        self.assertIsNone(self.proof(task))

    def test_modes_renames_and_binary_changes_are_preserved_in_delta(self):
        task = self.feature()
        path = Path(task["path"])
        git(path, "mv", "tracked.txt", "renamed file.txt")
        (path / "renamed file.txt").chmod(0o755)
        git(path, "commit", "-am", "rename and executable")
        self.squash(task)
        proof = self.proof(task)
        self.assertEqual(proof["kind"], "exact_squash")
        raw = delta(self.repo, proof["base"], proof["head"])
        self.assertIn(b"100755", raw)
        self.assertIn(b"renamed file.txt", raw)

    def test_tampered_completion_proof_blocks_archive(self):
        task = self.feature()
        self.squash(task)
        finished = self.pilot.finish(task["id"], "main", allow_squash=True)
        proof = finished["integration_proof"]
        proof["delta_sha256"] = "0" * 64
        self.pilot.db.execute("UPDATE tasks SET integration_proof=? WHERE id=?", (json.dumps(proof), task["id"]))
        with self.assertRaises(PilotError):
            self.pilot.archive(task["id"])
        self.assertTrue(Path(task["path"]).exists())

    def test_audit_squash_is_opt_in_and_active_or_dirty_remains_preserved(self):
        task = self.feature()
        self.squash(task)
        normal = audit(self.pilot, repo_path=self.repo, refresh=True)
        self.assertEqual(normal["eligible"], 0)
        result = audit(self.pilot, repo_path=self.repo, refresh=True, allow_squash=True)
        self.assertEqual(result["eligible"], 1)
        report = json.loads(Path(result["report"]).read_text())
        self.pilot.claim(task["id"], "active-agent", os.getpid())
        self.assertEqual(apply_audit(self.pilot, report)["archived"], 0)
        self.pilot.release(task["id"], "active-agent")
        (Path(task["path"]) / "pending.txt").write_text("pending")
        self.assertEqual(apply_audit(self.pilot, report)["archived"], 0)

    def test_proof_is_historical_even_if_target_reverts_it(self):
        task = self.feature()
        squash = self.squash(task)
        git(self.repo, "revert", "--no-edit", squash)
        self.assertEqual(self.proof(task)["kind"], "exact_squash")

    def test_root_only_target_history_has_no_squash_proof(self):
        task = self.feature()
        self.assertIsNone(self.proof(task))

    def test_legacy_ancestry_completion_without_proof_remains_compatible(self):
        task = self.feature()
        git(self.repo, "merge", "--ff-only", task["branch"])
        self.pilot.finish(task["id"], "main")
        self.pilot.db.execute("UPDATE tasks SET integration_proof=NULL WHERE id=?", (task["id"],))
        self.assertEqual(self.pilot.archive(task["id"])["state"], "archived")

    def test_script_squash_removal_revalidates_prepared_archive(self):
        task = self.feature()
        self.squash(task)
        script = self.repo / "scripts/wt.sh"
        script.parent.mkdir()
        script.write_bytes((Path(__file__).resolve().parents[1] / "examples/wt.sh").read_bytes())
        launcher = self.root / "test-pilot-launcher"
        cli = Path(__file__).resolve().parents[1] / "wtpilot.py"
        launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(cli)) + ' "$@"\n')
        launcher.chmod(0o755)
        script.write_text(script.read_text().replace('pilot=${WTPILOT_COMMAND:-wtpilot}', 'pilot=' + shlex.quote(str(launcher))))
        self.pilot.repository(self.repo)
        self.pilot.finish(task["id"], "main", allow_squash=True)
        with self.assertRaises(PilotError):
            self.pilot.verify_removal(task["id"], task["path"])
        archived = self.pilot.archive(task["id"])
        self.assertEqual(archived["state"], "archived")
        self.assertFalse(Path(task["path"]).exists())
