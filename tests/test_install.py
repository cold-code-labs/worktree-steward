import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install
from wtpilot import PilotError


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="steward-install-")
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        self.home = self.root / "agent-home"
        self.home_patch = mock.patch.object(install.Path, "home", return_value=self.home)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.temporary.cleanup()

    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            install.main(["--bin-dir", str(self.bin), "--state", str(self.state), *args])
        return json.loads(output.getvalue())

    def test_launcher_uses_explicit_state_without_scheduling(self):
        with mock.patch.object(install.subprocess, "run") as launchctl:
            result = self.invoke()
            launchctl.assert_not_called()
        execution = subprocess.run([str(self.bin / "wtpilot"), "status"], capture_output=True, text=True)
        self.assertEqual(execution.returncode, 0, execution.stderr)
        self.assertEqual(json.loads(execution.stdout)["repositories"], 0)
        self.assertIsNone(result["service"])
        self.assertFalse((self.home / "Library/LaunchAgents").exists())
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_unrelated_command_is_preserved(self):
        self.bin.mkdir()
        launcher = self.bin / "wtpilot"
        launcher.write_text("unrelated command")
        with self.assertRaises(PilotError):
            self.invoke()
        self.assertEqual(launcher.read_text(), "unrelated command")
        self.assertFalse(self.state.exists())

    def test_skill_collision_preserves_every_installation(self):
        destination = self.home / ".claude/skills/worktree-hygiene"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("someone else's skill")
        with self.assertRaises(PilotError):
            self.invoke("--skills")
        self.assertFalse(self.bin.exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_shared_skill_is_identical_for_three_agents(self):
        self.invoke("--skills")
        expected = (install.SOURCE / "skills/worktree-hygiene/SKILL.md").read_bytes()
        for agent in (".codex", ".cursor", ".claude"):
            self.assertEqual((self.home / agent / "skills/worktree-hygiene/SKILL.md").read_bytes(), expected)

    def test_reinstall_retains_original_launcher_backup(self):
        self.invoke()
        original = (self.bin / "wtpilot").read_bytes()
        second = self.invoke()
        third = self.invoke()
        self.assertEqual(len(third["backup_directories"]), 2)
        self.assertIn(second["backup_directories"][0], third["backup_directories"])
        files = list(Path(third["backup_directories"][0]).rglob("wtpilot"))
        self.assertEqual(files[0].read_bytes(), original)

    def test_mac_service_is_explicit_and_uses_selected_state(self):
        with mock.patch.object(install.sys, "platform", "darwin"), \
                mock.patch.object(install.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch.object(install, "plist_xml", wraps=install.plist_xml) as serializer:
            result = self.invoke("--service")
        config = serializer.call_args_list[0].args[0]
        self.assertEqual(config["ProgramArguments"][-3:], ["--state", str(self.state.resolve()), "maintenance"])
        self.assertEqual(config["StartInterval"], 600)
        if sys.platform == "darwin":
            subprocess.run(["plutil", "-lint", result["service"]], check=True, capture_output=True)

    def test_linux_service_refusal_happens_before_changes(self):
        with mock.patch.object(install.sys, "platform", "linux"), self.assertRaises(PilotError):
            self.invoke("--service")
        self.assertFalse(self.bin.exists())

    def test_pilot_upgrade_requires_explicit_registry_and_preserves_old_files(self):
        from wtpilot import Pilot
        registry = Pilot(self.state)
        registry.event(None, "preexisting-registry")
        registry.close()
        self.bin.mkdir()
        old = "#!/bin/sh\n# Worktree Pilot managed launcher\nexit 0\n"
        (self.bin / "wtpilot").write_text(old)
        destination = self.home / ".codex/skills/worktree-hygiene"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("Use the installed `~/.local/bin/wtpilot`\n")
        with self.assertRaises(PilotError):
            self.invoke("--skills")
        result = self.invoke("--skills", "--upgrade-pilot")
        registry = Pilot(self.state)
        try:
            self.assertEqual(registry.db.execute("select kind from events").fetchone()[0], "preexisting-registry")
        finally:
            registry.close()
        backup = Path(result["backup_directories"][0])
        self.assertEqual(next(backup.rglob("wtpilot")).read_text(), old)
        self.assertIn("Worktree Steward", (destination / "SKILL.md").read_text())

    def test_pilot_upgrade_never_overwrites_unknown_command(self):
        from wtpilot import Pilot
        Pilot(self.state).close()
        self.bin.mkdir()
        (self.bin / "wtpilot").write_text("unrelated command")
        with self.assertRaises(PilotError):
            self.invoke("--upgrade-pilot")
        self.assertEqual((self.bin / "wtpilot").read_text(), "unrelated command")


if __name__ == "__main__":
    unittest.main()
