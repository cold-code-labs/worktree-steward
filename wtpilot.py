#!/usr/bin/env python3
"""Local, zero-dependency worktree lifecycle manager. Python 3.12+, macOS/Linux."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid

VERSION = "0.1.1"
DEFAULT_STATE = (Path.home() / "Library/Application Support/worktree-steward"
                 if sys.platform == "darwin" else
                 Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "worktree-steward")
DEFAULT_POLICY = {
    "scan_roots": [],
    "managed_root": str(Path.home() / "wt/steward"),
    "completed_retention_hours": 24,
    "heartbeat_ttl_seconds": 180,
    "low_disk_warning_gib": 15,
    "port_base": 40000,
    "port_stride": 10,
    "discardable_ignored_directories": [],
    "automatic_hygiene": True,
    "script_contract_marker": "# WTPILOT_SCRIPT_CONTRACT_V1",
    "script_root_environment": "WTPILOT_WORKTREE_ROOT",
    "script_retry_setup": {},
}
TERMINAL = {"completed", "archived"}
SCRIPT_CONTRACT = "# WTPILOT_SCRIPT_CONTRACT_V1"


class PilotError(Exception):
    pass


def command(args, cwd=None, *, timeout=120, env=None, check=True, input=None):
    try:
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                                timeout=timeout, input=input)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PilotError(f"Command unavailable or timed out: {args[0]}") from exc
    if check and result.returncode:
        # Do not persist subprocess output: setup commands may print credentials.
        raise PilotError(f"{args[0]} failed (exit {result.returncode})")
    return result


def git(path, *args, check=True, timeout=120):
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return command(["git", "-C", str(path), *args], env=env, check=check,
                   timeout=timeout)


def output(result):
    return os.fsdecode(result.stdout).strip()


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def process_identity(pid):
    if not pid:
        return None
    result = command(["ps", "-p", str(pid), "-o", "lstart="], check=False)
    return output(result) or None


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def worktree_records(path):
    raw = git(path, "worktree", "list", "--porcelain", "-z").stdout
    records, record = [], {}
    for field in raw.split(b"\0"):
        if not field:
            if record:
                records.append(record)
                record = {}
            continue
        key, _, value = field.partition(b" ")
        record[os.fsdecode(key)] = os.fsdecode(value) if value else True
    if record:
        records.append(record)
    return records


class Pilot:
    def __init__(self, state=None):
        self.root = Path(state or os.environ.get("WTPILOT_STATE", DEFAULT_STATE)).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.policy_path = self.root / "policy.json"
        if not self.policy_path.exists():
            # Initialization is shared by simultaneous first invocations.
            with self.file_lock("initialize"):
                if not self.policy_path.exists():
                    atomic_json(self.policy_path, DEFAULT_POLICY)
        self.policy = {**DEFAULT_POLICY, **json.loads(self.policy_path.read_text())}
        self.db_path = self.root / "state.sqlite3"
        self.db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        with self.file_lock("initialize-db"):
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS repositories (
                    id TEXT PRIMARY KEY, common_dir TEXT NOT NULL UNIQUE,
                    path TEXT NOT NULL, name TEXT NOT NULL,
                    script TEXT, adapter TEXT NOT NULL, seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repositories(id),
                    task_key TEXT NOT NULL, path TEXT NOT NULL UNIQUE, branch TEXT NOT NULL,
                    base TEXT NOT NULL, base_sha TEXT NOT NULL, state TEXT NOT NULL,
                    owner TEXT, owner_pid INTEGER, owner_start TEXT, heartbeat REAL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL,
                    merged_into TEXT, merged_sha TEXT, completed_head TEXT, slot INTEGER UNIQUE,
                    archive TEXT, error TEXT, setup TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(repo_id, task_key)
                );
                CREATE TABLE IF NOT EXISTS worktrees (
                    path TEXT PRIMARY KEY, repo_id TEXT NOT NULL REFERENCES repositories(id),
                    branch TEXT, head TEXT, primary_tree INTEGER NOT NULL,
                    present INTEGER NOT NULL, locked INTEGER NOT NULL,
                    prunable INTEGER NOT NULL, seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
                    time REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state);
            """)
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}
            if "completed_head" not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN completed_head TEXT")
            if "integration_proof" not in columns:
                self.db.execute("ALTER TABLE tasks ADD COLUMN integration_proof TEXT")
            self.db.execute("PRAGMA user_version=2")
        os.chmod(self.db_path, 0o600)

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    @contextlib.contextmanager
    def file_lock(self, name, timeout=60):
        directory = self.root / "locks"
        directory.mkdir(exist_ok=True, mode=0o700)
        with (directory / (name + ".lock")).open("a") as handle:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise PilotError("Repository operation still running; retry later")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def event(self, task_id, kind, detail=None):
        self.db.execute("INSERT INTO events(task_id,time,kind,detail) VALUES(?,?,?,?)",
                        (task_id, time.time(), kind, json.dumps(detail or {})))

    def repository(self, path):
        path = Path(path).expanduser().resolve()
        common = Path(output(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))).resolve()
        repo_id = hashlib.sha256(os.fsencode(common)).hexdigest()[:16]
        records = worktree_records(path)
        # Git lists the main worktree first; common_dir can also be a bare repo.
        main = Path(records[0]["worktree"])
        if records[0].get("bare"):
            raise PilotError("Bare repositories are observation-only in this pilot")
        script = main / "scripts/wt.sh"
        adapter = "script" if script.is_file() else "git"
        self.db.execute("""INSERT INTO repositories VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET path=excluded.path, name=excluded.name,
            script=excluded.script, adapter=excluded.adapter, seen_at=excluded.seen_at""",
            (repo_id, str(common), str(main), main.name,
             str(script) if script.is_file() else None, adapter, time.time()))
        return dict(self.db.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone())

    def inventory(self, repo):
        records = worktree_records(repo["path"])
        with self.transaction():
            self.db.execute("UPDATE worktrees SET present=0 WHERE repo_id=?", (repo["id"],))
            for index, row in enumerate(records):
                path = str(Path(row["worktree"]).resolve())
                self.db.execute("""INSERT INTO worktrees VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET repo_id=excluded.repo_id,
                    branch=excluded.branch, head=excluded.head, primary_tree=excluded.primary_tree,
                    present=excluded.present, locked=excluded.locked,
                    prunable=excluded.prunable, seen_at=excluded.seen_at""",
                    (path, repo["id"], str(row.get("branch", "")).removeprefix("refs/heads/"),
                     row.get("HEAD"), index == 0, Path(path).is_dir(),
                     bool(row.get("locked")), bool(row.get("prunable")), time.time()))
        return records

    def scan(self, roots=None):
        errors, seen = [], set()
        paths = [Path(row["path"]) for row in self.db.execute("SELECT path FROM repositories")]
        for root in roots or self.policy["scan_roots"]:
            directory = Path(root).expanduser()
            if not directory.is_dir():
                errors.append({"path": str(directory), "error": "scan root missing"})
                continue
            if (directory / ".git").exists():
                paths.append(directory)
            paths.extend(p for p in directory.iterdir() if p.is_dir() and (p / ".git").exists())
        for path in paths:
            try:
                repo = self.repository(path)
                if repo["id"] in seen:
                    continue
                seen.add(repo["id"])
                self.inventory(repo)
            except (PilotError, OSError) as exc:
                errors.append({"path": str(path), "error": str(exc)})
        return {"repositories_scanned": len(seen), "errors": errors, **self.summary()}

    def summary(self):
        return {"repositories": self.db.execute("SELECT COUNT(*) FROM repositories").fetchone()[0],
                "worktrees": self.db.execute("SELECT COUNT(*) FROM worktrees WHERE present=1").fetchone()[0],
                "missing_worktrees": self.db.execute("SELECT COUNT(*) FROM worktrees WHERE present=0").fetchone()[0],
                "tasks": self.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                "states": {row[0]: row[1] for row in self.db.execute("SELECT state,COUNT(*) FROM tasks GROUP BY state")},
                "free_disk_gib": round(shutil.disk_usage(self.root).free / 2**30, 1),
                "task_limit": None}

    def task(self, reference, repo=None):
        rows = self.db.execute("SELECT * FROM tasks WHERE id=? OR task_key=? OR path=?",
                               (reference, reference, str(Path(reference).expanduser().resolve()))).fetchall()
        if repo:
            rows = [r for r in rows if r["repo_id"] == repo["id"]]
        if len(rows) != 1:
            raise PilotError("Task missing or ambiguous; use its full id and/or --repo")
        return dict(rows[0])

    def repo_for_task(self, task):
        return dict(self.db.execute("SELECT * FROM repositories WHERE id=?", (task["repo_id"],)).fetchone())

    def port(self, task):
        return None if task["slot"] is None else self.policy["port_base"] + task["slot"] * self.policy["port_stride"]

    def public_task(self, task):
        value = dict(task)
        value["integration_proof"] = json.loads(value["integration_proof"]) if value.get("integration_proof") else None
        value["port"] = self.port(task)
        value["setup"] = json.loads(value["setup"])
        value["owner_alive"] = self.owner_alive(task)
        return value

    def owner_alive(self, task):
        process_alive = bool(task["owner_pid"] and process_identity(task["owner_pid"]) == task["owner_start"])
        heartbeat_alive = bool(task["owner"] and task["heartbeat"] and
                               time.time() - task["heartbeat"] < self.policy["heartbeat_ttl_seconds"])
        return process_alive or heartbeat_alive

    def allocate_slot(self):
        used = {row[0] for row in self.db.execute("SELECT slot FROM tasks WHERE slot IS NOT NULL")}
        stride, base = self.policy["port_stride"], self.policy["port_base"]
        for slot in range((65536 - base) // stride):
            if slot not in used:
                return slot
        raise PilotError("Port ranges exhausted; use --no-ports or archive completed tasks")

    def require_script_contract(self, repo):
        marker = self.policy["script_contract_marker"]
        if not isinstance(marker, str) or not marker.startswith("# WTPILOT_") or len(marker) > 100:
            raise PilotError("Invalid script contract marker")
        if repo["adapter"] == "script" and marker not in Path(repo["script"]).read_text():
            raise PilotError("Unrecognized scripts/wt.sh; adapt to the documented contract before lifecycle management")

    def script_root(self, repo):
        return Path(os.environ.get("WTPILOT_WORKTREE_ROOT",
                    os.environ.get(self.script_root_environment(), str(Path.home() / "wt" / repo["name"]))))

    def script_root_environment(self):
        name = self.policy["script_root_environment"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", name):
            raise PilotError("Invalid script workspace-root environment name")
        return name

    def new(self, path, key, base=None, setup=None, ports=True):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", key):
            raise PilotError("Task key must use lowercase letters, numbers and hyphens")
        repo = self.repository(path)
        self.require_script_contract(repo)
        with self.file_lock(repo["id"]):
            existing = self.db.execute("SELECT * FROM tasks WHERE repo_id=? AND task_key=?", (repo["id"], key)).fetchone()
            if existing:
                if existing["state"] == "archived":
                    raise PilotError("Task was archived; use a new task key")
                return {"reused": True, **self.public_task(existing)}
            if base is None:
                remote = output(git(repo["path"], "symbolic-ref", "refs/remotes/origin/HEAD", check=False))
                base = remote or ("origin/main" if git(repo["path"], "rev-parse", "--verify", "origin/main", check=False).returncode == 0 else "HEAD")
            base_sha = output(git(repo["path"], "rev-parse", "--verify", base + "^{commit}"))
            task_id = uuid.uuid4().hex
            if repo["adapter"] == "script":
                branch = "wt/" + key
                wt_path = self.script_root(repo) / key
            else:
                branch = "codex/" + key + "-" + task_id[:8]
                wt_path = Path(self.policy["managed_root"]).expanduser() / (repo["name"] + "-" + repo["id"][:8]) / key
            wt_path = wt_path.resolve()
            if wt_path.exists():
                raise PilotError("Workspace already exists outside this task; adopt it explicitly")
            recovery_setup = list(setup or [])
            if repo["adapter"] == "script":
                recovery_setup = list(self.policy["script_retry_setup"].get(repo["id"], [])) + recovery_setup
                if any(not isinstance(argv, list) or not argv or
                       any(not isinstance(arg, str) for arg in argv) for argv in recovery_setup):
                    raise PilotError("Script retry setup must be an array of nonempty argv arrays")
            now = time.time()
            with self.transaction():
                slot = self.allocate_slot() if ports else None
                self.db.execute("""INSERT INTO tasks(id,repo_id,task_key,path,branch,base,base_sha,state,
                    created_at,updated_at,slot,setup) VALUES(?,?,?,?,?,?,?,'preparing',?,?,?,?)""",
                    (task_id, repo["id"], key, str(wt_path), branch, base, base_sha, now, now, slot,
                     json.dumps(recovery_setup)))
                self.event(task_id, "reserved", {"adapter": repo["adapter"]})
            try:
                if repo["adapter"] == "script":
                    # Scripts retain authority over creation/removal and their own preparation.
                    if base != "origin/main":
                        raise PilotError("Script adapter requires --base origin/main")
                    self.script(repo, "new", key, wt_path.parent)
                    actual = next((r for r in worktree_records(repo["path"]) if r.get("branch") == "refs/heads/" + branch), None)
                    if not actual:
                        raise PilotError("Repository script did not produce the requested branch")
                    actual_path = Path(actual["worktree"]).resolve()
                    if actual_path != wt_path:
                        raise PilotError("Repository script created a workspace outside its configured root")
                    self.db.execute("UPDATE tasks SET path=?,base_sha=? WHERE id=?",
                                    (str(wt_path), output(git(wt_path, "rev-parse", "HEAD")), task_id))
                else:
                    wt_path.parent.mkdir(parents=True, exist_ok=True)
                    git(repo["path"], "worktree", "add", "-b", branch, str(wt_path), base_sha, timeout=300)
                for argv in setup or []:
                    command(argv, cwd=wt_path, env=self.environment(self.task(task_id)), timeout=1800)
                self.db.execute("UPDATE tasks SET state='ready',updated_at=? WHERE id=?", (time.time(), task_id))
                self.event(task_id, "prepared")
            except BaseException as exc:
                self.db.execute("UPDATE tasks SET state='failed',error=?,updated_at=? WHERE id=?",
                                (str(exc), time.time(), task_id))
                self.event(task_id, "preparation_failed")
                raise
            self.inventory(repo)
            return {"reused": False, **self.public_task(self.task(task_id))}

    def script(self, repo, action, key, workspace_root=None):
        self.require_script_contract(repo)
        env = os.environ.copy()
        env["WTPILOT_DELEGATED"] = "1"
        env["WTPILOT_STATE"] = str(self.root)
        if action == "rm":
            task = self.task(key, repo)
            if task["state"] != "archiving":
                raise PilotError("Script removal requires a prepared archive")
            env["WTPILOT_REMOVAL_TASK"] = task["id"]
        if workspace_root is not None:
            env["WTPILOT_WORKTREE_ROOT"] = str(workspace_root)
            env[self.script_root_environment()] = str(workspace_root)
        result = command(["bash", repo["script"], action, key], cwd=repo["path"], env=env, timeout=1800)
        return result

    def adopt(self, path, key, owned=False, setup=None, expected_head=None):
        path = Path(path).expanduser().resolve()
        repo = self.repository(path)
        self.require_script_contract(repo)
        with self.file_lock(repo["id"]):
            rows = worktree_records(repo["path"])
            matched = next((r for r in rows[1:] if Path(r["worktree"]).resolve() == path), None)
            if not matched or not matched.get("branch") or matched.get("locked") or matched.get("prunable"):
                raise PilotError("Only registered, non-primary, unlocked branch worktrees can be adopted")
            if expected_head and matched["HEAD"] != expected_head:
                raise PilotError("Worktree HEAD changed since audit")
            if repo["adapter"] == "script":
                expected = self.script_root(repo) / key
                if path != expected.resolve() or matched["branch"] != "refs/heads/wt/" + key:
                    raise PilotError("Script adapter can only adopt its configured root and wt/<task> branches")
            task_id, now = uuid.uuid4().hex, time.time()
            with self.transaction():
                self.db.execute("""INSERT INTO tasks(id,repo_id,task_key,path,branch,base,base_sha,state,
                    created_at,updated_at,setup) VALUES(?,?,?,?,?,?,?, ?,?,?,?)""",
                    (task_id, repo["id"], key, str(path), matched["branch"].removeprefix("refs/heads/"),
                     "HEAD", matched["HEAD"], "ready" if owned else "observed", now, now, json.dumps(setup or [])))
                self.event(task_id, "adopted", {"managed": owned})
            self.inventory(repo)
            return self.public_task(self.task(task_id))

    def environment(self, task):
        env = os.environ.copy()
        env.update(WTPILOT_TASK_ID=task["id"], WTPILOT_STATE=str(self.root),
                   WTPILOT_WORKTREE=task["path"], WTPILOT_BRANCH=task["branch"])
        port = self.port(task)
        namespace = "wt_" + task["repo_id"][:8] + "_" + task["id"][:12]
        env.update(WTPILOT_NAMESPACE=namespace, DB_NAME=namespace,
                   REDIS_PREFIX=namespace + ":", COMPOSE_PROJECT_NAME=namespace)
        if port is not None:
            env.update(PORT=str(port), PORT_WEB=str(port), PORT_API=str(port + 1),
                       WTPILOT_PORT_END=str(port + self.policy["port_stride"] - 1))
        return env

    def claim(self, reference, owner, pid=None, repo=None):
        task = self.task(reference, repo)
        with self.file_lock(task["repo_id"]):
            task = self.task(task["id"])
            if task["state"] not in {"ready", "working", "interrupted", "failed"}:
                raise PilotError("Task must be ready, interrupted or failed before claiming")
            if task["state"] == "failed":
                raise PilotError("Retry preparation before claiming a failed task")
            if self.owner_alive(task) and (task["owner"] != owner or
                    (pid and pid != task["owner_pid"])):
                raise PilotError("Another live owner holds this task")
            if not Path(task["path"]).is_dir():
                raise PilotError("Task worktree is missing")
            pid = pid or os.getppid()
            identity = process_identity(pid)
            if not identity:
                raise PilotError("Owner PID is not alive")
            with self.transaction():
                self.db.execute("""UPDATE tasks SET state='working',owner=?,owner_pid=?,owner_start=?,
                    heartbeat=?,updated_at=? WHERE id=?""", (owner, pid, identity, time.time(), time.time(), task["id"]))
                self.event(task["id"], "claimed", {"owner": owner, "pid": pid})
            return self.public_task(self.task(task["id"]))

    def heartbeat(self, reference, owner):
        with self.transaction():
            task = self.task(reference)
            if task["owner"] != owner or task["state"] != "working":
                raise PilotError("Heartbeat requires the current owner of a working task")
            self.db.execute("UPDATE tasks SET heartbeat=?,updated_at=? WHERE id=?",
                            (time.time(), time.time(), task["id"]))
        return self.public_task(self.task(task["id"]))

    def release(self, reference, owner):
        task = self.task(reference)
        with self.file_lock(task["repo_id"]), self.transaction():
            task = self.task(task["id"])
            if task["owner"] != owner:
                raise PilotError("Release requires the current owner")
            self.db.execute("""UPDATE tasks SET state='ready',owner=NULL,owner_pid=NULL,owner_start=NULL,
                heartbeat=NULL,updated_at=? WHERE id=?""", (time.time(), task["id"]))
            self.event(task["id"], "released")
        return self.public_task(self.task(task["id"]))

    def retry(self, reference):
        task = self.task(reference)
        with self.file_lock(task["repo_id"]):
            task = self.task(task["id"])
            if task["state"] != "failed" or self.owner_alive(task):
                raise PilotError("Retry requires an inactive failed task")
            self.validate_workspace(task)
            for argv in json.loads(task["setup"]):
                command(argv, cwd=task["path"], env=self.environment(task), timeout=1800)
            self.db.execute("UPDATE tasks SET state='ready',error=NULL,updated_at=? WHERE id=?", (time.time(), task["id"]))
            self.event(task["id"], "retried")
        return self.public_task(self.task(task["id"]))

    def validate_workspace(self, task):
        repo = self.repo_for_task(task)
        rows = worktree_records(repo["path"])
        row = next((r for r in rows[1:] if str(Path(r["worktree"]).resolve()) == task["path"]), None)
        if not row or row.get("locked") or row.get("prunable") or not Path(task["path"]).is_dir():
            raise PilotError("Worktree missing, primary, locked or prunable")
        if row.get("branch") != "refs/heads/" + task["branch"]:
            raise PilotError("Worktree branch changed outside the pilot")
        return row

    def open_processes(self, path):
        if not shutil.which("lsof"):
            raise PilotError("lsof unavailable; cannot prove workspace inactivity")
        result = command(["lsof", "-n", "-P", "-F", "p", "+D", str(path)], timeout=90, check=False)
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise PilotError("lsof could not inspect the complete workspace")
        return sorted({int(line[1:]) for line in result.stdout.splitlines() if line.startswith(b"p")})

    def safety(self, task):
        row = self.validate_workspace(task)
        if self.owner_alive(task):
            raise PilotError("Task has a live owner or recent heartbeat")
        if git(task["path"], "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout:
            raise PilotError("Workspace has staged, unstaged or untracked work")
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
            location = output(git(task["path"], "rev-parse", "--path-format=absolute", "--git-path", marker))
            if Path(location).exists():
                raise PilotError("Git operation in progress")
        if self.open_processes(task["path"]):
            raise PilotError("Workspace has open files or process working directories")
        return row["HEAD"]

    def finish(self, reference, target, expected_head=None, allow_squash=False):
        from integration import prove
        task = self.task(reference)
        with self.file_lock(task["repo_id"]):
            task = self.task(task["id"])
            if task["state"] not in {"ready", "interrupted", "completed"}:
                raise PilotError("Release ownership and resolve failures before completing")
            head = self.safety(task)
            if expected_head and head != expected_head:
                raise PilotError("Worktree HEAD changed since audit")
            target_sha = output(git(task["path"], "rev-parse", "--verify", target + "^{commit}"))
            proof = prove(task["path"], head, target_sha, allow_squash)
            if not proof:
                raise PilotError("Integration not proven; --allow-squash requires an exact complete squash delta")
            with self.transaction():
                self.db.execute("""UPDATE tasks SET state='completed',completed_at=COALESCE(completed_at,?),
                    merged_into=?,merged_sha=?,completed_head=?,integration_proof=?,updated_at=? WHERE id=?""",
                    (time.time(), target, target_sha, head, json.dumps(proof), time.time(), task["id"]))
                self.event(task["id"], "completed", {"target": target, "head": head, "proof": proof})
        return self.public_task(self.task(task["id"]))

    def archive(self, reference, automatic=False):
        from integration import verify
        task = self.task(reference)
        with self.file_lock(task["repo_id"]):
            task = self.task(task["id"])
            if task["state"] != "completed":
                raise PilotError("Only explicitly completed, integrated tasks can be archived")
            if automatic and time.time() - task["completed_at"] < self.policy["completed_retention_hours"] * 3600:
                raise PilotError("Completion retention period has not elapsed")
            repo = self.repo_for_task(task)
            head = self.safety(task)
            proof = json.loads(task["integration_proof"]) if task.get("integration_proof") else None
            if head != task["completed_head"] or not verify(task["path"], proof, head, task["merged_sha"]):
                raise PilotError("Task changed since its integration was verified")
            directory = self.root / "archives" / task["id"]
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            archive = directory / "ignored-files.tar.gz"
            ignored = git(task["path"], "ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout.split(b"\0")
            original_snapshot = self.ignored_snapshot(task)
            discarded, saved = [], 0
            temporary = directory / (uuid.uuid4().hex + ".tar.gz")
            try:
                with tarfile.open(temporary, "w:gz", dereference=False) as bundle:
                    for raw in ignored:
                        if not raw:
                            continue
                        relative = Path(os.fsdecode(raw))
                        if relative.is_absolute() or ".." in relative.parts:
                            raise PilotError("Invalid ignored-file path")
                        if any(part in self.policy["discardable_ignored_directories"] for part in relative.parts):
                            discarded.append(str(relative))
                            continue
                        source = Path(task["path"]) / relative
                        # Retain secrets and arbitrary ignored assets; never dereference links.
                        bundle.add(source, arcname=str(relative), recursive=False)
                        saved += 1
                os.chmod(temporary, 0o600)
                if self.safety(task) != head or self.ignored_snapshot(task) != original_snapshot:
                    raise PilotError("Workspace changed while archiving")
                os.replace(temporary, archive)
                ref = "refs/wtpilot/archive/" + task["id"]
                git(repo["path"], "update-ref", ref, head)
                git(repo["path"], "update-ref", "refs/wtpilot/evidence/" + task["id"], task["merged_sha"])
                atomic_json(directory / "manifest.json", {"task": self.public_task(task), "head": head,
                    "ref": ref, "saved_ignored_files": saved,
                    "discarded_dependency_files": len(discarded), "time": timestamp()})
                self.db.execute("UPDATE tasks SET state='archiving',archive=?,updated_at=? WHERE id=?",
                                (str(directory), time.time(), task["id"]))
                if repo["adapter"] == "script":
                    self.script(repo, "rm", task["task_key"], Path(task["path"]).parent)
                else:
                    git(repo["path"], "worktree", "remove", task["path"], timeout=300)
                with self.transaction():
                    self.db.execute("""UPDATE tasks SET state='archived',slot=NULL,owner=NULL,owner_pid=NULL,
                        owner_start=NULL,heartbeat=NULL,updated_at=? WHERE id=?""", (time.time(), task["id"]))
                    self.event(task["id"], "archived", {"archive": str(directory), "head": head})
            except BaseException:
                temporary.unlink(missing_ok=True)
                # Preserve an interrupted archiving state for doctor reconciliation.
                raise
            self.inventory(repo)
            return self.public_task(self.task(task["id"]))

    def restore(self, reference):
        task = self.task(reference)
        with self.file_lock(task["repo_id"]):
            task = self.task(task["id"])
            if task["state"] != "archived" or Path(task["path"]).exists():
                raise PilotError("Restore requires an archived task and absent destination")
            repo = self.repo_for_task(task)
            if repo["adapter"] == "script":
                raise PilotError("Script-backed restore requires a repository-specific restore adapter; backup retained")
            manifest = json.loads((Path(task["archive"]) / "manifest.json").read_text())
            Path(task["path"]).parent.mkdir(parents=True, exist_ok=True)
            git(repo["path"], "worktree", "add", "--detach", task["path"], manifest["head"])
            # Restore onto a fresh branch; preserve any externally changed original branch.
            branch = "codex/restore-" + task["id"][:12] + "-" + uuid.uuid4().hex[:6]
            git(task["path"], "switch", "-c", branch)
            try:
                with tarfile.open(Path(task["archive"]) / "ignored-files.tar.gz") as bundle:
                    bundle.extractall(task["path"], filter="data")
            except BaseException:
                self.db.execute("UPDATE tasks SET state='failed',branch=?,error='restore failed',updated_at=? WHERE id=?",
                                (branch, time.time(), task["id"]))
                raise
            with self.transaction():
                self.db.execute("""UPDATE tasks SET state='ready',branch=?,completed_at=NULL,merged_into=NULL,
                    merged_sha=NULL,completed_head=NULL,error=NULL,updated_at=? WHERE id=?""", (branch, time.time(), task["id"]))
                self.event(task["id"], "restored")
            self.inventory(repo)
        return self.public_task(self.task(task["id"]))

    def verify_removal(self, reference, path):
        """Read-only validation called by wt.sh while its parent holds the repo lock."""
        from integration import verify
        task = self.task(reference)
        if task["state"] != "archiving" or str(Path(path).resolve()) != task["path"]:
            raise PilotError("Removal does not match a prepared task archive")
        head = self.safety(task)
        proof = json.loads(task["integration_proof"]) if task.get("integration_proof") else None
        directory = Path(task["archive"] or "")
        if not (directory / "manifest.json").is_file() or not (directory / "ignored-files.tar.gz").is_file():
            raise PilotError("Recovery archive missing")
        manifest = json.loads((directory / "manifest.json").read_text())
        repo = self.repo_for_task(task)
        preserved = output(git(repo["path"], "rev-parse", manifest["ref"]))
        if head != task["completed_head"] or preserved != head or manifest["head"] != head \
                or not verify(task["path"], proof, head, task["merged_sha"]):
            raise PilotError("Integration or archive evidence no longer matches")
        return {"task_id": task["id"], "verified": True}

    def ignored_snapshot(self, task):
        snapshot = []
        for raw in git(task["path"], "ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout.split(b"\0"):
            if raw:
                relative = os.fsdecode(raw)
                stat = (Path(task["path"]) / relative).lstat()
                snapshot.append((relative, stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        return sorted(snapshot)

    def doctor(self):
        issues = []
        for row in self.db.execute("SELECT * FROM tasks WHERE state!='archived'").fetchall():
            try:
                with self.file_lock(row["repo_id"], timeout=0.2):
                    task = self.task(row["id"])
                    if task["state"] == "archived":
                        continue
                    repo = self.repo_for_task(task)
                    rows = worktree_records(repo["path"])
                    registered = any(str(Path(r["worktree"]).resolve()) == task["path"] for r in rows)
                    if task["state"] == "archiving" and not registered and not Path(task["path"]).exists():
                        manifest_path = Path(task["archive"] or "") / "manifest.json"
                        if manifest_path.is_file():
                            manifest = json.loads(manifest_path.read_text())
                            preserved = output(git(repo["path"], "rev-parse", manifest["ref"], check=False))
                            if preserved == manifest["head"] and (manifest_path.parent / "ignored-files.tar.gz").is_file():
                                self.db.execute("UPDATE tasks SET state='archived',slot=NULL,updated_at=? WHERE id=?", (time.time(), task["id"]))
                                self.event(task["id"], "archive_reconciled")
                                continue
                    self.validate_workspace(task)
                    if task["state"] == "archiving":
                        self.db.execute("UPDATE tasks SET state='completed',updated_at=? WHERE id=?", (time.time(), task["id"]))
                        self.event(task["id"], "archive_retryable")
                    elif task["state"] == "preparing":
                        self.db.execute("UPDATE tasks SET state='failed',error='preparation interrupted',updated_at=? WHERE id=?", (time.time(), task["id"]))
                        self.event(task["id"], "preparation_interrupted")
                    elif task["state"] == "working" and not self.owner_alive(task):
                        self.db.execute("""UPDATE tasks SET state='interrupted',owner=NULL,owner_pid=NULL,owner_start=NULL,
                            heartbeat=NULL,updated_at=? WHERE id=?""", (time.time(), task["id"]))
                        self.event(task["id"], "owner_lost")
                        issues.append({"task": task["id"], "kind": "interrupted", "message": "Owner lost; workspace preserved"})
                    if task["state"] in {"preparing", "archiving", "failed"}:
                        issues.append({"task": task["id"], "kind": task["state"], "message": "Recovered state; pending work preserved"})
            except (PilotError, OSError, ValueError) as exc:
                issues.append({"task": row["id"], "kind": "workspace", "message": str(exc)})
        summary = self.summary()
        if summary["free_disk_gib"] < self.policy["low_disk_warning_gib"]:
            issues.append({"kind": "disk", "message": "Low free disk; advisory only", "free_gib": summary["free_disk_gib"]})
        return {"issues": issues, **summary}

    def hygiene(self, apply=False, repo_id=None):
        candidates, blocked, archived = [], [], []
        rows = self.db.execute("SELECT * FROM tasks WHERE state='completed' AND (? IS NULL OR repo_id=?)",
                               (repo_id, repo_id)).fetchall()
        for row in rows:
            task = dict(row)
            try:
                if time.time() - task["completed_at"] < self.policy["completed_retention_hours"] * 3600:
                    raise PilotError("Completion retention period has not elapsed")
                self.safety(task)
                candidates.append(task["id"])
                if apply:
                    self.archive(task["id"], automatic=True)
                    archived.append(task["id"])
            except PilotError as exc:
                blocked.append({"task": task["id"], "reason": str(exc)})
        result = {"apply": apply, "candidates": candidates, "archived": archived, "blocked": blocked,
                  "external_worktrees": "observation-only", "time": timestamp()}
        self.event(None, "hygiene", result)
        return result

    def cycle(self):
        with self.file_lock("maintenance", timeout=1):
            result = {"scan": self.scan(), "doctor": self.doctor(),
                      "hygiene": self.hygiene(apply=self.policy["automatic_hygiene"]), "time": timestamp()}
            atomic_json(self.root / "last-maintenance.json", result)
            return result

    def conflicts(self, repo_path):
        repo = self.repository(repo_path)
        rows = self.inventory(repo)
        overlaps, failures, by_file = [], [], {}
        base = output(git(repo["path"], "symbolic-ref", "refs/remotes/origin/HEAD", check=False)) or "main"
        for row in rows[1:]:
            path = row["worktree"]
            if not Path(path).is_dir() or row.get("bare"):
                continue
            try:
                merge_base = output(git(path, "merge-base", base, "HEAD"))
                names = set(git(path, "diff", "--name-only", "-z", merge_base, "HEAD").stdout.split(b"\0"))
                names.update(git(path, "diff", "--name-only", "-z", "HEAD").stdout.split(b"\0"))
                names.update(git(path, "ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0"))
                for name in names - {b""}:
                    by_file.setdefault(os.fsdecode(name), []).append(path)
            except PilotError as exc:
                failures.append({"path": path, "error": str(exc)})
        overlaps = [{"file": name, "worktrees": paths} for name, paths in sorted(by_file.items()) if len(paths) > 1]
        return {"overlaps": overlaps, "errors": failures, "meaning": "File overlap, not a semantic or merge-conflict guarantee"}

    def observed_worktrees(self, repo_path=None, inspect=False):
        repo = self.repository(repo_path) if repo_path else None
        if repo:
            self.inventory(repo)
        rows = self.db.execute("""SELECT w.*,r.name AS repository,t.id AS task_id,t.state,
            CASE WHEN t.id IS NULL OR t.state='observed' THEN 'observed' ELSE 'managed' END AS management
            FROM worktrees w JOIN repositories r ON r.id=w.repo_id LEFT JOIN tasks t ON t.path=w.path
            WHERE (? IS NULL OR w.repo_id=?) ORDER BY r.name,w.path""", (repo["id"] if repo else None, repo["id"] if repo else None)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            if inspect and value["present"]:
                try:
                    value["dirty"] = bool(git(value["path"], "status", "--porcelain=v1", "-z", timeout=30).stdout)
                except PilotError as exc:
                    value["inspection_error"] = str(exc)
            result.append(value)
        return result


def run_task(pilot, reference, argv, owner):
    task = pilot.task(reference)
    if not argv:
        raise PilotError("run requires a command after --")
    owner = owner or "run:" + uuid.uuid4().hex
    pilot.claim(reference, owner, os.getpid())
    env = pilot.environment(task)
    port = pilot.port(task)
    child = None
    interrupted = False
    old_handlers = {}
    def forward(sig, frame):
        nonlocal interrupted
        interrupted = True
        if child and child.poll() is None:
            os.killpg(child.pid, sig)
    try:
        if port is not None:
            for number in range(port, port + pilot.policy["port_stride"]):
                with socket.socket() as listener:
                    try:
                        listener.bind(("127.0.0.1", number))
                    except OSError as exc:
                        raise PilotError(f"Allocated port {number} is in use; refusing launch") from exc
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, forward)
        child = subprocess.Popen(argv, cwd=task["path"], env=env, start_new_session=True)
        pilot.event(task["id"], "process_started", {"pid": child.pid})
        while True:
            try:
                code = child.wait(timeout=30)
                break
            except subprocess.TimeoutExpired:
                pilot.heartbeat(task["id"], owner)
        pilot.event(task["id"], "process_finished", {"exit": code, "interrupted": interrupted})
        return code if code >= 0 else 128 - code
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        pilot.release(task["id"], owner)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", help="State directory (also WTPILOT_STATE)")
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--json", action="store_true")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("init")
    scan = sub.add_parser("scan"); scan.add_argument("roots", nargs="*")
    sub.add_parser("status")
    sub.add_parser("list")
    inventory = sub.add_parser("inventory"); inventory.add_argument("--all", action="store_true")
    inventory.add_argument("--inspect", action="store_true")
    new = sub.add_parser("new"); new.add_argument("key"); new.add_argument("--base")
    new.add_argument("--no-ports", action="store_true")
    new.add_argument("--setup", help='JSON array of argv arrays, e.g. [["pnpm","install","--frozen-lockfile"]]')
    adopt = sub.add_parser("adopt"); adopt.add_argument("path"); adopt.add_argument("--task", required=True)
    adopt.add_argument("--managed", action="store_true", help="Explicitly enroll in lifecycle and eventual hygiene")
    for name in ("claim", "heartbeat", "release"):
        c = sub.add_parser(name); c.add_argument("task"); c.add_argument("--owner", required=True)
        if name == "claim": c.add_argument("--pid", type=int)
    for name in ("context", "retry", "archive", "restore"):
        c = sub.add_parser(name); c.add_argument("task")
    finish = sub.add_parser("finish"); finish.add_argument("task"); finish.add_argument("--into", required=True)
    finish.add_argument("--allow-squash", action="store_true")
    run = sub.add_parser("run"); run.add_argument("task"); run.add_argument("--owner"); run.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("doctor")
    hygiene = sub.add_parser("hygiene"); hygiene.add_argument("--apply", action="store_true")
    sub.add_parser("maintenance")
    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("--all", action="store_true")
    audit_parser.add_argument("--refresh", action="store_true")
    audit_parser.add_argument("--squash", action="store_true")
    audit_apply = sub.add_parser("audit-apply")
    audit_apply.add_argument("report")
    removal = sub.add_parser("verify-removal")
    removal.add_argument("task")
    removal.add_argument("--path", required=True)
    sub.add_parser("conflicts")
    events = sub.add_parser("events"); events.add_argument("--limit", type=int, default=30)
    legacy = sub.add_parser("legacy"); legacy.add_argument("operation", choices=["new", "ls", "rm", "gc"]); legacy.add_argument("key", nargs="?")
    return p


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    pilot = None
    try:
        pilot = Pilot(args.state)
        action = args.action
        if action == "init": result = {"version": VERSION, "state": str(pilot.root), "policy": pilot.policy}
        elif action == "scan": result = pilot.scan(args.roots or None)
        elif action == "status": result = pilot.summary()
        elif action == "list": result = [pilot.public_task(r) for r in pilot.db.execute("SELECT * FROM tasks ORDER BY created_at DESC")]
        elif action == "inventory": result = pilot.observed_worktrees(None if args.all else args.repo, args.inspect)
        elif action == "new":
            setup = json.loads(args.setup) if args.setup else []
            if not isinstance(setup, list) or any(not isinstance(c, list) or not c or any(not isinstance(s, str) for s in c) for c in setup):
                raise PilotError("--setup must be an array of nonempty argv arrays")
            result = pilot.new(args.repo, args.key, args.base, setup, not args.no_ports)
        elif action == "adopt": result = pilot.adopt(args.path, args.task, args.managed)
        elif action == "claim": result = pilot.claim(args.task, args.owner, args.pid)
        elif action == "heartbeat": result = pilot.heartbeat(args.task, args.owner)
        elif action == "release": result = pilot.release(args.task, args.owner)
        elif action == "context":
            task = pilot.task(args.task)
            result = {"task": pilot.public_task(task), "environment": {k: v for k, v in pilot.environment(task).items()
                       if k.startswith("WTPILOT_") or k in {"PORT", "PORT_WEB", "PORT_API", "DB_NAME", "REDIS_PREFIX", "COMPOSE_PROJECT_NAME"}}}
        elif action == "retry": result = pilot.retry(args.task)
        elif action == "finish": result = pilot.finish(args.task, args.into, allow_squash=args.allow_squash)
        elif action == "archive": result = pilot.archive(args.task)
        elif action == "restore": result = pilot.restore(args.task)
        elif action == "doctor": result = pilot.doctor()
        elif action == "hygiene": result = pilot.hygiene(args.apply)
        elif action == "maintenance": result = pilot.cycle()
        elif action == "audit":
            from audit import audit
            result = audit(pilot, args.all, args.repo, args.refresh, args.squash)
        elif action == "audit-apply":
            from audit import apply_audit
            result = apply_audit(pilot, json.loads(Path(args.report).read_text()))
        elif action == "verify-removal": result = pilot.verify_removal(args.task, args.path)
        elif action == "conflicts": result = pilot.conflicts(args.repo)
        elif action == "events": result = [dict(r) for r in pilot.db.execute("SELECT * FROM events ORDER BY sequence DESC LIMIT ?", (args.limit,))]
        elif action == "run": return run_task(pilot, args.task, args.command[1:] if args.command[:1] == ["--"] else args.command, args.owner)
        elif action == "legacy":
            repo = pilot.repository(args.repo)
            if args.operation == "new":
                if not args.key: raise PilotError("new requires a task key")
                result = pilot.new(args.repo, args.key, "origin/main")
            elif args.operation == "ls":
                pilot.inventory(repo)
                result = [dict(r) for r in pilot.db.execute("""SELECT w.*,t.id AS task_id,t.state,
                    CASE WHEN t.id IS NULL OR t.state='observed' THEN 'observed' ELSE 'managed' END AS management
                    FROM worktrees w LEFT JOIN tasks t ON w.path=t.path WHERE w.repo_id=?""", (repo["id"],))]
            elif args.operation == "rm":
                if not args.key: raise PilotError("rm requires a task key")
                task = pilot.task(args.key, repo)
                if task["state"] in {"ready", "interrupted"}:
                    task = pilot.finish(task["id"], "origin/main")
                result = pilot.archive(task["id"])
            else: result = pilot.hygiene(True, repo["id"])
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (PilotError, sqlite3.Error, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    finally:
        if pilot: pilot.close()


if __name__ == "__main__":
    sys.modules.setdefault("wtpilot", sys.modules[__name__])
    raise SystemExit(main())
