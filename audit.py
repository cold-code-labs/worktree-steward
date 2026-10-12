"""Evidence-based inventory and explicitly requested cleanup of existing worktrees."""
from collections import Counter
import concurrent.futures
import hashlib
import os
from pathlib import Path
import re
import time

from wtpilot import Pilot, PilotError, atomic_json, command, git, output, timestamp
from integration import prove, SquashIndex


def integration_target(repo, refresh=False):
    """Use origin's canonical branch, or local main/master only without origin."""
    path = repo["path"]
    has_origin = git(path, "config", "--get", "remote.origin.url", check=False).returncode == 0
    if has_origin:
        ref = output(git(path, "symbolic-ref", "refs/remotes/origin/HEAD", check=False))
        if not ref:
            ref = next(("refs/remotes/origin/" + name for name in ("main", "master")
                        if git(path, "rev-parse", "--verify", "refs/remotes/origin/" + name,
                               check=False).returncode == 0), None)
        if not ref or not ref.startswith("refs/remotes/origin/"):
            raise PilotError("Origin integration branch could not be identified")
        branch = ref.removeprefix("refs/remotes/origin/")
        if repo["adapter"] == "script" and branch != "main":
            raise PilotError("Local script adapter requires origin/main")
        if refresh:
            env = os.environ.copy()
            env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
            # No prune, force, checkout or working-file mutation. Non-fast-forward fails closed.
            command(["git", "-C", path, "fetch", "--no-tags", "origin",
                     "refs/heads/" + branch + ":" + ref], env=env, timeout=90)
        kind = "origin_refreshed" if refresh else "origin_cached"
    else:
        ref = next(("refs/heads/" + name for name in ("main", "master")
                    if git(path, "rev-parse", "--verify", "refs/heads/" + name,
                           check=False).returncode == 0), None)
        if not ref:
            raise PilotError("Local integration branch could not be identified")
        kind = "local"
    sha = output(git(path, "rev-parse", "--verify", ref + "^{commit}"))
    return {"ref": ref, "sha": sha, "kind": kind, "verified_at": timestamp()}


def provisional(row):
    return {**row, "owner": None, "owner_pid": None, "owner_start": None, "heartbeat": None}


def inspect_worktree(pilot, row, repo, target, target_error=None, allow_squash=False, squash_index=None):
    result = {**row, "reasons": [], "target": target, "checked_at": timestamp()}
    task = pilot.db.execute("SELECT * FROM tasks WHERE path=?", (row["path"],)).fetchone()
    result["task_id"] = task["id"] if task else None
    result["task_state"] = task["state"] if task else None
    reasons = result["reasons"]
    if row["primary_tree"]:
        reasons.append("primary_worktree")
    if not row["present"]:
        reasons.append("missing_worktree")
    if row["locked"]:
        reasons.append("locked_worktree")
    if row["prunable"]:
        reasons.append("prunable_registration")
    if not row["branch"]:
        reasons.append("detached_head")
    parts = Path(row["path"]).parts
    if any(parts[i] in {".codex", ".cursor", ".claude"} and parts[i + 1] == "worktrees"
           for i in range(len(parts) - 1)):
        # App-owned workspaces can have live chats without open files. Their
        # attachment/lifecycle evidence is not available through Git or lsof.
        reasons.append("application_owned_workspace")
    if reasons:
        result["decision"] = "preserve"
        return result
    try:
        if task and (pilot.owner_alive(task) or task["state"] not in {"ready", "interrupted", "completed"}):
            reasons.append("live_owner_or_pending_task")
            result["owner_pid"] = task["owner_pid"]
        raw = git(row["path"], "status", "--porcelain=v1", "-z", "--untracked-files=all", timeout=30).stdout
        result["dirty"] = bool(raw)
        if raw:
            reasons.append("pending_work")
        head = output(git(row["path"], "rev-parse", "HEAD"))
        result["head"] = head
        if target_error:
            reasons.append("integration_target_unavailable")
            result["error"] = target_error
        elif target:
            result["integration_proof"] = prove(row["path"], head, target["sha"], allow_squash, squash_index)
            result["integrated"] = bool(result["integration_proof"])
            if not result["integrated"]:
                reasons.append("commits_not_proven_integrated")
            elif target["kind"] == "origin_cached":
                reasons.append("remote_reference_not_refreshed")
        else:
            reasons.append("integration_target_unavailable")
        if repo["adapter"] == "script":
            pilot.require_script_contract(repo)
            root = pilot.script_root(repo).resolve()
            if Path(row["path"]).parent != root or row["branch"] != "wt/" + Path(row["path"]).name:
                reasons.append("outside_script_adapter_contract")
        if not reasons:
            # Same checks as finish/archive: markers, branch identity, dirty state,
            # recorded ownership and full lsof workspace inspection.
            result["active_pids"] = pilot.open_processes(row["path"])
            if result["active_pids"]:
                reasons.append("active_process")
            else:
                pilot.safety(dict(task) if task else provisional(result))
        result["decision"] = "preserve" if reasons else "eligible"
    except (PilotError, OSError, ValueError) as exc:
        result["decision"] = "preserve"
        reasons.append("inspection_uncertain")
        result["error"] = str(exc)
    return result


def summarize(rows):
    return {"worktrees": len(rows), "present": sum(bool(r["present"]) for r in rows),
            "eligible": sum(r["decision"] == "eligible" for r in rows),
            "proofs": dict(Counter(r["integration_proof"]["kind"] for r in rows if r.get("integration_proof"))),
            "preserved": sum(r["decision"] != "eligible" for r in rows),
            "reasons": dict(Counter(reason for r in rows for reason in r["reasons"]))}


def audit(pilot, all_repos=False, repo_path=None, refresh=False, allow_squash=False):
    if all_repos:
        scan = pilot.scan()
        repos = [dict(r) for r in pilot.db.execute("SELECT * FROM repositories ORDER BY name")]
    else:
        repo = pilot.repository(repo_path)
        pilot.inventory(repo)
        repos, scan = [repo], None

    def inspect_repo(repo):
        local = Pilot(pilot.root)
        try:
            rows = [dict(r) for r in local.db.execute("SELECT * FROM worktrees WHERE repo_id=? ORDER BY path", (repo["id"],))]
            target, error = None, None
            linked = any(not r["primary_tree"] and r["present"] for r in rows)
            if linked:
                try:
                    with local.file_lock(repo["id"], timeout=0.2):
                        target = integration_target(repo, refresh)
                except PilotError as exc:
                    error = str(exc)
            results = []
            index = SquashIndex(repo["path"], target["sha"]) if target and allow_squash else None
            for row in rows:
                try:
                    with local.file_lock(repo["id"], timeout=0.2):
                        results.append(inspect_worktree(local, row, repo, target, error, allow_squash, index))
                except PilotError as exc:
                    results.append({**row, "target": target, "decision": "preserve",
                                    "reasons": ["repository_busy"], "error": str(exc)})
            return results
        finally:
            local.close()

    # Read concurrency only; this is not a cap on agents or worktrees.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        rows = [row for group in pool.map(inspect_repo, repos) for row in group]
    report = {"version": 1, "time": timestamp(), "state_directory": str(pilot.root),
              "refresh": refresh, "allow_squash": allow_squash, "scan": scan, "summary": summarize(rows), "worktrees": rows}
    filename = pilot.root / "audits" / (time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex() + ".json")
    atomic_json(filename, report)
    pilot.event(None, "audit", {"report": str(filename), **report["summary"]})
    return {"report": str(filename), **report["summary"]}


def adoption_key(row, repo):
    if repo["adapter"] == "script":
        return Path(row["path"]).name
    slug = re.sub("[^a-z0-9-]+", "-", Path(row["path"]).name.lower()).strip("-")[:45] or "worktree"
    return "audit-" + slug + "-" + hashlib.sha256(os.fsencode(row["path"])).hexdigest()[:10]


def apply_audit(pilot, report):
    if report.get("version") != 1 or report.get("state_directory") != str(pilot.root):
        raise PilotError("Audit report version or state directory does not match")
    results, refreshed = [], {}
    for evidence in report["worktrees"]:
        if evidence["decision"] != "eligible":
            continue
        record = {"path": evidence["path"], "repo_id": evidence["repo_id"]}
        try:
            repo = dict(pilot.db.execute("SELECT * FROM repositories WHERE id=?", (evidence["repo_id"],)).fetchone())
            # Revalidate the report's exact branch, HEAD, integration and inactivity.
            # Nested flock calls are avoided: lifecycle operations take their own lock.
            with pilot.file_lock(repo["id"], timeout=0.2):
                pilot.inventory(repo)
                actual = pilot.db.execute("SELECT * FROM worktrees WHERE path=? AND repo_id=?",
                                          (evidence["path"], repo["id"])).fetchone()
                if not actual or actual["branch"] != evidence["branch"] or actual["head"] != evidence["head"]:
                    raise PilotError("Worktree changed since audit")
                if repo["id"] not in refreshed:
                    refreshed[repo["id"]] = integration_target(repo, refresh=True)
                check = inspect_worktree(pilot, dict(actual), repo, refreshed[repo["id"]],
                                         allow_squash=report.get("allow_squash", False))
                if check["decision"] != "eligible":
                    raise PilotError("Cleanup refused: " + ", ".join(check["reasons"]))
            task = pilot.db.execute("SELECT * FROM tasks WHERE path=?", (evidence["path"],)).fetchone()
            if task is None:
                task = pilot.adopt(evidence["path"], adoption_key(evidence, repo), owned=True,
                                   expected_head=evidence["head"])
                record["adopted"] = True
            record["task_id"] = task["id"]
            pilot.finish(task["id"], refreshed[repo["id"]]["ref"], expected_head=evidence["head"],
                         allow_squash=report.get("allow_squash", False))
            archived = pilot.archive(task["id"])
            record.update(result="archived", archive=archived["archive"])
        except (PilotError, OSError, ValueError, TypeError) as exc:
            # Other owners are never released and failures are never bypassed.
            record.update(result="preserved", reason=str(exc))
        results.append(record)
        atomic_json(pilot.root / "audits" / "last-cleanup.json", {"time": timestamp(), "results": results})
    result = {"time": timestamp(), "results": results,
              "archived": sum(r["result"] == "archived" for r in results),
              "preserved": sum(r["result"] != "archived" for r in results)}
    atomic_json(pilot.root / "audits" / "last-cleanup.json", result)
    pilot.event(None, "audit_cleanup", {"archived": result["archived"], "preserved": result["preserved"]})
    return result
