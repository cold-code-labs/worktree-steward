#!/usr/bin/env python3
"""Install the wtpilot launcher; skills and macOS scheduling are opt-in."""
import argparse
import datetime
import html
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from wtpilot import Pilot, PilotError, atomic_json

SOURCE = Path(__file__).resolve().parent
LABEL = "org.cold-code-labs.worktree-steward"
MARKER = "# Worktree Steward managed launcher"


def plist_xml(value):
    """Generate only the plist types we use, without loading an XML parser."""
    if isinstance(value, bool):
        return "<true/>" if value else "<false/>"
    if isinstance(value, int):
        return f"<integer>{value}</integer>"
    if isinstance(value, str):
        return "<string>" + html.escape(value) + "</string>"
    if isinstance(value, list):
        return "<array>" + "".join(plist_xml(v) for v in value) + "</array>"
    if isinstance(value, dict):
        return "<dict>" + "".join("<key>" + html.escape(k) + "</key>" + plist_xml(v)
                                  for k, v in value.items()) + "</dict>"
    raise PilotError("Unsupported LaunchAgent configuration value")


def backup_file(path, directory):
    if not path.exists():
        return
    target = directory / str(path.resolve()).lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(path, target)
    target.chmod(0o600)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--skills", action="store_true", help="Install the shared skill for Codex, Cursor and Claude")
    parser.add_argument("--service", action="store_true", help="Opt into a macOS LaunchAgent every ten minutes")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        raise PilotError("Python 3.12 or newer is required")
    if args.service and sys.platform != "darwin":
        raise PilotError("--service currently supports macOS; use your scheduler on Linux")
    os.umask(0o077)
    launcher = args.bin_dir.expanduser().resolve() / "wtpilot"
    destinations = [Path.home() / agent / "skills/worktree-hygiene"
                    for agent in (".codex", ".cursor", ".claude")] if args.skills else []
    # Check all collisions before modifying the command or agent installations.
    if launcher.is_symlink() or (launcher.exists() and MARKER not in launcher.read_text()):
        raise PilotError("Another wtpilot command exists; choose a different --bin-dir")
    for destination in destinations:
        existing = destination / "SKILL.md"
        if destination.is_symlink() or (destination.exists() and (
                not existing.is_file() or "Worktree Steward" not in existing.read_text())):
            raise PilotError("A different worktree-hygiene skill already exists; preserve it before installing")
    pilot = Pilot(args.state)
    try:
        prior = pilot.root / "installation.json"
        previous = json.loads(prior.read_text()) if prior.exists() else {}
        backups = pilot.root / "installation-backups" / (
            datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        launcher.parent.mkdir(parents=True, exist_ok=True)
        backup_file(launcher, backups)
        content = ("#!/bin/sh\n" + MARKER + "\nexec " + shlex.quote(sys.executable)
                   + " " + shlex.quote(str(SOURCE / "wtpilot.py"))
                   + " --state " + shlex.quote(str(pilot.root)) + ' "$@"\n')
        temporary = launcher.with_name("wtpilot.install.tmp")
        temporary.write_text(content)
        temporary.chmod(0o755)
        os.replace(temporary, launcher)
        changed = []
        for destination in destinations:
            for source in sorted((SOURCE / "skills/worktree-hygiene").rglob("*")):
                if not source.is_file():
                    continue
                target = destination / source.relative_to(SOURCE / "skills/worktree-hygiene")
                if target.exists() and target.read_bytes() == source.read_bytes():
                    continue
                backup_file(target, backups)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                changed.append(str(target))
        service = None
        if args.service:
            service = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
            service.parent.mkdir(parents=True, exist_ok=True)
            backup_file(service, backups)
            logs = pilot.root / "logs"
            logs.mkdir(exist_ok=True, mode=0o700)
            config = {
                "Label": LABEL,
                "ProgramArguments": [sys.executable, str(SOURCE / "wtpilot.py"),
                                     "--state", str(pilot.root), "maintenance"],
                "StartInterval": 600, "RunAtLoad": True, "WorkingDirectory": str(SOURCE),
                "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10,
                "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": str(logs / "maintenance-errors.log"),
            }
            service.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">' + plist_xml(config) + '</plist>\n')
            service.chmod(0o600)
            domain = "gui/" + str(os.getuid())
            subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
            result = subprocess.run(["launchctl", "bootstrap", domain, str(service)], capture_output=True)
            if result.returncode:
                raise PilotError("LaunchAgent saved, but bootstrap failed; inspect launchctl locally")
        directories = previous.get("backup_directories", [])
        if backups.exists():
            directories.append(str(backups))
        result = {"command": str(launcher), "source": str(SOURCE), "state": str(pilot.root),
                  "changed": changed, "backup_directories": directories,
                  "service": str(service) if service else previous.get("service")}
        atomic_json(prior, result)
        print(json.dumps(result, indent=2))
    finally:
        pilot.close()


if __name__ == "__main__":
    try:
        main()
    except (PilotError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
