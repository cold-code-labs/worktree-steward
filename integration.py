"""Conservative, reproducible integration evidence without worktree mutation."""
import hashlib
import os
import re

from wtpilot import PilotError, command, git, output, timestamp


RAW_ARGS = ("diff-tree", "--no-commit-id", "-r", "--raw", "--no-abbrev",
            "--no-renames", "-z")


def delta(path, before, after):
    # Object IDs and modes, not whitespace-insensitive patch IDs or tree equality.
    return git(path, *RAW_ARGS, before, after).stdout


def unique_base(path, head, target):
    result = git(path, "merge-base", "--all", head, target, check=False)
    bases = output(result).splitlines()
    return bases[0] if result.returncode == 0 and len(bases) == 1 else None


class SquashIndex:
    def __init__(self, path, target):
        self.path, self.target = path, target
        self.signatures = None

    def load(self):
        parents = {}
        for line in output(git(self.path, "rev-list", "--parents", "--no-merges", self.target)).splitlines():
            fields = line.split()
            if len(fields) == 2:
                parents[fields[0]] = fields[1]
        if not parents:
            self.signatures = {}
            return
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        raw = command(["git", "-C", str(self.path), "diff-tree", "--stdin", "-r", "--raw",
                       "--no-abbrev", "--no-renames", "-z"], env=env,
                      input=("\n".join(parents) + "\n").encode(), timeout=120).stdout
        fields, signatures, i = raw.split(b"\0"), {}, 0
        while i < len(fields) and fields[i]:
            commit = fields[i].decode("ascii")
            i += 1
            if commit not in parents:
                raise PilotError("Unexpected commit in squash history index")
            change = bytearray()
            while i < len(fields) and fields[i].startswith(b":"):
                if i + 1 >= len(fields) or not fields[i + 1]:
                    raise PilotError("Incomplete squash tree delta")
                change.extend(fields[i] + b"\0" + fields[i + 1] + b"\0")
                i += 2
            if change:
                digest = hashlib.sha256(change).hexdigest()
                signatures.setdefault(digest, []).append((commit, parents[commit]))
        self.signatures = signatures

    def proof(self, head):
        base = unique_base(self.path, head, self.target)
        if not base:
            return None
        change = delta(self.path, base, head)
        if not change:
            return None
        digest = hashlib.sha256(change).hexdigest()
        if self.signatures is None:
            self.load()
        matches = []
        for commit, parent in self.signatures.get(digest, []):
            if git(self.path, "merge-base", "--is-ancestor", base, parent, check=False).returncode == 0 \
                    and delta(self.path, parent, commit) == change:
                matches.append((commit, parent))
        if len(matches) != 1:
            return None
        commit, parent = matches[0]
        return {"version": 1, "kind": "exact_squash", "head": head, "target": self.target,
                "base": base, "squash_commit": commit, "parent": parent,
                "delta_sha256": digest, "verified_at": timestamp()}


def prove(path, head, target, allow_squash=False, index=None):
    if git(path, "merge-base", "--is-ancestor", head, target, check=False).returncode == 0:
        return {"version": 1, "kind": "ancestry", "head": head,
                "target": target, "verified_at": timestamp()}
    if allow_squash:
        return (index or SquashIndex(path, target)).proof(head)
    return None


def verify(path, proof, head, target):
    if proof is None:  # Compatibility with completed tasks from the original pilot.
        return git(path, "merge-base", "--is-ancestor", head, target, check=False).returncode == 0
    if proof.get("version") != 1 or proof.get("head") != head or proof.get("target") != target:
        return False
    if proof.get("kind") == "ancestry":
        return git(path, "merge-base", "--is-ancestor", head, target, check=False).returncode == 0
    if proof.get("kind") != "exact_squash":
        return False
    if any(not isinstance(proof.get(key), str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", proof[key])
           for key in ("head", "target", "base", "squash_commit", "parent")):
        return False
    base, commit, parent = (proof[k] for k in ("base", "squash_commit", "parent"))
    if output(git(path, "show", "-s", "--format=%P", commit)).split() != [parent]:
        return False
    if unique_base(path, head, target) != base:
        return False
    for ancestor, descendant in ((base, parent), (commit, target)):
        if git(path, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode:
            return False
    change = delta(path, base, head)
    return bool(change) and hashlib.sha256(change).hexdigest() == proof.get("delta_sha256") \
        and change == delta(path, parent, commit)
