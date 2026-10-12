---
name: worktree-hygiene
description: Manage local worktrees for parallel coding agents with the SQLite wtpilot CLI. Use for task workspace creation or reuse, ownership, auditing existing worktrees, and cleaning proven-integrated work while preserving pending work and active agents.
---

# Worktree hygiene

Use `wtpilot` from Worktree Steward. Read the project
[operations guide](https://github.com/cold-code-labs/worktree-steward/blob/main/docs/operations.md)
when you need recovery details, adapter limitations or policy configuration. This skill manages local Git
workspaces; it does not merge, push, deploy or send messages to other agents.

## Audit and cleanup

An audit request authorizes inspection and reporting. A cleanup request also
authorizes adopting eligible existing worktrees and immediately archiving them;
do not ask for the same authorization again. Do not apply cleanup merely because
the skill was selected during unrelated coding work.

For a machine-wide audit:

```sh
wtpilot audit --all --refresh
```

For one repository, omit `--all` and pass `--repo /absolute/repo/path` before
`audit`. `--refresh` fetches only origin's integration branch, without force or
prune. Without refresh, cached remote refs produce a report but cannot establish
cleanup eligibility. Repositories without origin use local main/master.

Read the saved report path returned by the command. Every registered worktree
has a decision, reasons and integration evidence. Primary and missing entries
remain in the report. Eligible means: registered linked branch workspace; clean;
HEAD proven integrated by ancestry or the exact squash proof below; no active owner,
recent heartbeat, Git operation or open process; and compatible lifecycle
adapter. Branch names, age, push success, identical trees and patch similarity
alone do not prove integration.

To investigate integration without preserved commit ancestry, opt into the
strict squash proof:

```sh
wtpilot audit --all --refresh --squash
wtpilot finish <task-id> --into <integration-ref> --allow-squash
```

Read `integration_proof` for each result. `exact_squash` requires one unique merge
base and a nonempty, complete raw tree delta from that base to the exact task
HEAD. It must match exactly one single-parent commit reachable from the target,
whose parent follows the base. All paths, old/new Git object IDs and file modes
must match; binary files, symlinks, deletions and renamed paths are included.
No whitespace normalization, partial patch matches or current-tree equality.
Multiple matches, partial integration, extra commits with different net changes,
rebased conflict resolutions and uncertainty remain unproven and preserved.
This proves historical integration of the complete net change, not that the
hosting platform specifically used its squash button or that later commits have
not reverted it. Original task history is preserved in recovery refs.

The proof records HEAD, target, base, matching commit, parent and delta digest in
SQLite and the archive manifest. Completion recomputes proof from current
evidence; archival revalidates it and pins both original HEAD and target history.
Live owners, recent heartbeats, dirty/untracked work and active processes always
prevent cleanup, even when committed HEAD has a valid squash proof. The
contract-based script adapter delegates a final read-only removal check to the pilot while
keeping `scripts/wt.sh` as the removal entrypoint.

When the user has requested cleanup, apply that concrete report:

```sh
wtpilot audit-apply /absolute/path/to/report.json
```

The command refreshes integration evidence, checks the exact branch and HEAD,
rechecks ownership and processes, adopts eligible worktrees as managed tasks,
finishes them and archives through the pilot. It preserves recovery refs and
ignored assets; configured reinstallable dependencies may be discarded.
Explicit cleanup bypasses the 24-hour automatic retention, never safety checks.
Failed checks preserve the worktree and record a reason. Never release another
owner, adopt an ineligible workspace to make it removable, or bypass a refusal.

Review `audits/last-cleanup.json` in the state directory and run `wtpilot status`
and `wtpilot doctor`. Report removals, remaining worktrees, preservation reasons,
active agents, evidence/report paths and any actual inspection errors. Counts
can change while other sessions work. Avoid printing ignored-file contents,
credentials or subprocess output from setup/fetch commands.

Workspaces under `.codex/worktrees`, `.cursor/worktrees` or `.claude/worktrees`
are preserved because application/chat lifecycle evidence is outside this CLI.
When app APIs expose running chats, inspect them as additional evidence; do not
infer session inactivity solely from lsof. Native app-managed cleanup requires
the application's own ownership and archival mechanism.

## Create and work

Repositories with `scripts/wt.sh` must keep it as the creation/removal entrypoint.
For a script implementing the documented adapter contract, run `scripts/wt.sh new <task-key>` and use its
returned task ID. Never replace that script with manual Git lifecycle commands.
Otherwise:

```sh
wtpilot --repo /absolute/repo/path new <task-key> --base <integration-ref>
wtpilot run <task-id> -- <agent-or-command>
```

Reuse the same task key for the same work. `run` provides ownership, heartbeats
and runtime namespaces. An agent already running may instead claim with its
real long-lived PID and stable session owner, then release with the same owner.
Never use a short-lived shell's PID. Release is not completion: after actual
integration, use `wtpilot finish <task-id> --into <integration-ref>`.

There is no arbitrary limit on concurrent agents or worktrees. When an operator opts into scheduled maintenance, it automatically archives
managed, explicitly completed workspaces after the configured retention
(default: 24 hours). Existing worktrees are observation-only until an
explicit adoption or authorized cleanup. A dead PID or expired heartbeat alone
does not authorize deletion.

No force removal, reset, clean, process killing, manual Git-lock deletion,
branch pruning or blanket directory deletion. Preserve locked, detached,
prunable, missing, dirty, active, ambiguous and unsupported-adapter worktrees.
Inspect reported uncertainty; if it cannot be resolved with concrete evidence,
preserve that item and continue the independent eligible items.
