# Worktree Steward

**Let agents work in parallel. Keep the worktree pile recoverable.**

[![CI](https://github.com/cold-code-labs/worktree-steward/actions/workflows/ci.yml/badge.svg)](https://github.com/cold-code-labs/worktree-steward/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Worktree Steward is a local SQLite manager for Git worktrees used by parallel
coding agents. Its CLI, `wtpilot`, tracks task ownership, reuses task workspaces,
audits old worktrees, and archives completed work only after proving integration
and checking for pending work and active processes.

No arbitrary limit of four or five agents. No server, account, Python packages,
telemetry, or LLM calls. Just Python's standard library, Git, and local evidence.

This is an early release. macOS and Linux are tested in CI. Use one Unix account
and one shared state directory per machine; native Windows is unsupported.

## What it solves

| Problem | Steward's approach |
| --- | --- |
| Two agents recreate the same workspace | A stable repository/task key reuses the same task |
| Concurrent lifecycle operations collide | Short repository locks and SQLite transactions |
| An agent exits but its work remains | Ownership release and integration completion are separate |
| Squash merging breaks ancestry checks | Optional exact, complete Git tree-delta proof |
| Old worktrees contain local `.env` files | Private ignored-file archives and pinned recovery refs |
| Cleanup mistakes a live agent for an abandoned task | Live PID identity, heartbeat, Git state and `lsof` checks |
| Every agent tries to use the same port/database name | Port slots and task-specific environment namespaces |

Ports are checked before execution, but sockets are not held until your app binds
them. Database names and prefixes are environment conventions; Steward does not
provision services or sandbox processes.

## Install

Requirements: **Python 3.12+**, **Git 2.40+**, `ps`, `lsof`, and a local filesystem
supporting SQLite locking and Unix `flock`. On Linux, install `lsof` using your
distribution's package manager. Keep shared state off network filesystems.

```sh
git clone https://github.com/cold-code-labs/worktree-steward.git
cd worktree-steward
python3 install.py
export PATH="$HOME/.local/bin:$PATH"
wtpilot --help
```

The launcher references this checkout: keep it in a stable location. Installation
does not edit repositories or global agent rules, schedule maintenance, or delete
worktrees. It refuses to replace an unrelated `wtpilot` command. Use
`--bin-dir /path/to/bin` for a separate installation, or invoke
`python3 /path/to/worktree-steward/wtpilot.py` directly.

Install the shared skill for **Codex, Cursor, and Claude Code**:

```sh
python3 install.py --skills
```

The same `worktree-hygiene/SKILL.md` is installed under each agent's
`~/.codex/skills`, `~/.cursor/skills`, and `~/.claude/skills`. Existing unrelated
skills are preserved. [Agent setup](docs/agents.md) includes an optional rules
snippet and ownership examples.

## One task, one worktree

```sh
# Replace /path/to/repo and choose an integration branch that exists locally.
wtpilot --repo /path/to/repo new fix-cache --base main
# Copy the returned task ID. Running an agent owns and heartbeats its workspace.
wtpilot run TASK_ID -- your-agent-command

# Integrate through your normal reviewed Git workflow, then prove completion.
wtpilot finish TASK_ID --into main
wtpilot hygiene                       # preview
wtpilot hygiene --apply               # eligible completed work after retention
```

`run` starts in the task's worktree and releases ownership when the child exits.
It does not mark the task completed. A failed setup is retained for `retry`.
For an existing long-running agent, use `claim`, `heartbeat`, and `release`
with the real live PID and the same session owner. See [operations](docs/operations.md).

Repositories with `scripts/wt.sh` retain that entrypoint. Unknown scripts cannot
be used for managed creation or cleanup. Read the [adapter contract](docs/adapters.md)
before enabling a script-backed repository.

## Audit the worktrees you already have

```sh
# Explicit scan roots; nothing scans your home directory automatically.
wtpilot scan /path/to/projects
wtpilot audit --all --refresh --squash
# Read the private report returned above, including preservation reasons.
wtpilot audit-apply /absolute/path/to/report.json
wtpilot doctor
```

Auditing preserves existing unmanaged worktrees. `audit-apply` is an explicit
cleanup action: it rechecks evidence, adopts eligible worktrees, proves completion,
backs up ignored assets, and archives them immediately. It bypasses the default
24-hour retention, never the safety checks. Without `--refresh`, cached remote
refs cannot make a worktree eligible. Refresh fetches only the origin integration
branch; it never forces or prunes refs.

Application-owned `.codex/worktrees`, `.cursor/worktrees`, and `.claude/worktrees`
are preserved because Git cannot establish whether the application's chat is idle.

## What counts as integration?

The default proof is Git ancestry: the exact task HEAD is an ancestor of the
integration target. Opt into squash investigation with `audit --squash` or
`finish --allow-squash`.

The squash proof requires a unique merge base and an exact match between the
**entire nonempty base-to-task delta** and **one single-parent commit's delta**
reachable from the target. Paths, old/new blob IDs and modes must all match.
Binary files and symlinks are included. Partial patches, whitespace similarity,
identical current trees, multiple matching commits and ambiguity are refused.

This proves historical integration of the complete net change; it does not prove
which hosting-platform merge button was used or that a later change did not revert
it. The proof's HEAD, target, base, candidate and digest are stored and revalidated
before removal. Original commits remain reachable through recovery refs.

## Recoverability before reclamation

- Dirty, untracked, locked, primary, detached, active, changed, and uncertain worktrees are preserved.
- A stale heartbeat or an old directory never authorizes deletion.
- Automatic hygiene handles only managed, explicitly completed tasks after retention.
- Ignored files are archived privately before removal. No directories are discarded by default.
- `refs/wtpilot/archive/TASK_ID` preserves original HEAD; an evidence ref preserves the integration target.
- Generic worktrees can be restored with `wtpilot restore TASK_ID` onto a fresh branch.
- Cleanup never uses force removal, reset, clean, process killing, or automatic branch pruning.

Steward serializes its own lifecycle operations. External programs that bypass
ownership and locks can still race with it; use the ownership protocol for all
participating agents. This is a local trusted-user tool, not an adversarial isolation
boundary or a replacement for remote backups.

## Optional automatic hygiene

```sh
# macOS only: opt into a LaunchAgent running every ten minutes.
python3 install.py --service
```

On Linux, schedule `wtpilot maintenance` with your own user-level scheduler.
Scheduling is never installed implicitly. [Operations](docs/operations.md) covers
configuration, recovery, retention and disabling the macOS service.

## Local data and privacy

State lives outside the checkout:

- macOS: `~/Library/Application Support/worktree-steward`
- Linux: `$XDG_STATE_HOME/worktree-steward`, or `~/.local/state/worktree-steward`
- Override with `--state /path/to/state` or `WTPILOT_STATE`.

The state directory is private (`0700`); SQLite, JSON reports and archives are
created as `0600`. Reports contain local paths and branch names. Archives may
contain credentials from ignored files. **Do not upload state, reports, archives,
policy files, or installation backups to issues or Git.** Read [SECURITY.md](SECURITY.md).
There is no telemetry. Explicit Git fetches and user-supplied setup/agent commands
can access the network.

## Contribute

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q wtpilot.py audit.py integration.py install.py tests
```

The suite exercises real Git repositories, concurrent processes, owner conflicts,
failed setup, integration proofs, archival races and restoration. See
[CONTRIBUTING.md](CONTRIBUTING.md). Useful next steps include additional repository
adapter contracts, richer application ownership evidence, and Linux service tooling.

Built by [Cold Code Labs](https://github.com/cold-code-labs). Released under [MIT](LICENSE).
