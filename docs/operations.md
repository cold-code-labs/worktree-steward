# Operations and recovery

All commands produce JSON except the child program run by `run`. Global `--repo`
and `--state` arguments precede the subcommand. `wtpilot --help` lists commands;
`wtpilot COMMAND --help` lists that command's arguments.

## Ownership and setup

```sh
wtpilot --repo /path/to/repo new feature-a --base main --no-ports
wtpilot run TASK_ID -- your-agent-command
```

`run` records its long-lived supervisor PID and process start time, forwards
termination signals to its child group, heartbeats every 30 seconds, and releases
ownership on exit. Do not assign ownership to a short-lived command's PID.
For an agent that is already running:

```sh
wtpilot claim TASK_ID --owner SESSION_ID --pid LIVE_AGENT_PID
wtpilot heartbeat TASK_ID --owner SESSION_ID
wtpilot release TASK_ID --owner SESSION_ID
```

The same owner must release the task. Don't release someone else's task to enable
cleanup. Expired heartbeat alone does not authorize removing a worktree; a live
PID with the recorded start time still holds ownership.

`new --setup '[["your-tool","install"]]'` accepts an array of argv arrays,
without shell evaluation. A nonzero setup exit preserves the worktree as failed;
`wtpilot retry TASK_ID` reruns preparation. Setup output is not persisted because
it can contain credentials. No dependency installation is inferred from your files.

Task environments include `WTPILOT_TASK_ID`, `WTPILOT_WORKTREE`, `WTPILOT_BRANCH`,
`WTPILOT_NAMESPACE`, `DB_NAME`, `REDIS_PREFIX`, and `COMPOSE_PROJECT_NAME`.
Port-enabled tasks also receive `PORT`, `PORT_WEB`, `PORT_API`, and
`WTPILOT_PORT_END`. Your application must use these values itself. Port ranges
are finite OS resources; `--no-ports` removes that allocation requirement.

## Policy

`wtpilot init` creates private state and `policy.json`. Edit the policy deliberately;
configuration is local and should stay outside version control.

| Key | Default | Meaning |
| --- | --- | --- |
| `scan_roots` | `[]` | Explicit project directories; scans each root and its immediate Git children |
| `managed_root` | `~/wt/steward` (expanded) | Root for generic managed worktrees |
| `completed_retention_hours` | `24` | Automatic archival waits after verified completion |
| `heartbeat_ttl_seconds` | `180` | Recent heartbeat protection; live PID protection also applies |
| `low_disk_warning_gib` | `15` | Advisory only; never deletes pending work |
| `port_base`, `port_stride` | `40000`, `10` | Transactionally assigned port ranges |
| `discardable_ignored_directories` | `[]` | Optional reinstallable directory names to omit from archives |
| `automatic_hygiene` | `true` | Allows an explicitly invoked/scheduled maintenance cycle to apply hygiene |

Don't configure discardable names unless every matching directory in the worktree
is reproducible: the match applies to path components anywhere in an ignored path.
Nothing is discarded by default. Existing configurations retain their explicit
choices when upgrading.

## Completion, audit and archival

`finish TASK_ID --into REF` requires release, clean files, the registered branch,
no Git operation, no live owner, no open workspace process, and an ancestry proof.
`--allow-squash` enables the exact delta proof described in README.

`hygiene` previews managed completed tasks; `hygiene --apply` archives eligible
tasks after retention. `archive TASK_ID` is an explicit immediate archival action.
Neither command adopts unmanaged worktrees or bypasses evidence checks.

`scan /path/to/projects` discovers repositories and registers observations.
`inventory --all --inspect`, `list`, `status`, `events`, and `conflicts` support
inspection. Conflicts are an informational comparison of task file changes, not
a merge or dependency coordinator.

`audit --all --refresh --squash` writes a private report with per-worktree decisions,
preservation reasons and integration proof. Origin's canonical branch is used,
with main/master fallback. Without origin, local main/master is used. Without a
fresh origin fetch, remote-based evidence is observation-only. `audit-apply REPORT`
rechecks the current branch, exact HEAD, integration, ownership and processes
before explicit adoption and immediate archival. Other owners are never released.

An eligible worktree's ignored files are saved as a private tar archive with
symlinks retained without dereferencing. Snapshot checks bracket archival. The
original HEAD and target are pinned under `refs/wtpilot/archive/` and
`refs/wtpilot/evidence/`. Branches remain in generic repositories. Cleanup never
pushes recovery refs. State and repositories must both be retained for recovery.

## Recovery

```sh
wtpilot doctor
wtpilot restore TASK_ID
```

`doctor` reconciles known interrupted preparation/archival states. Missing,
locked, changed or ambiguous workspaces are preserved and reported.
`restore` recreates a generic archived worktree on a fresh `codex/restore-*` branch
and extracts ignored assets using Python's tar `data` filter. Original branch
changes are preserved. Unsafe archive members can make restoration fail; the
archive and refs remain available for manual inspection. Script-backed restore
requires a repository-specific adapter and is currently refused.

On a refusal, resolve the reported underlying condition or preserve the item.
Never force removal, reset/clean files, kill agents, or delete Git locks to make
cleanup pass. A dead owner can be reported as interrupted; its unintegrated work
remains protected. `lsof` absence, warnings and inspection failures refuse cleanup.

## Scheduling and uninstall

macOS: `python3 install.py --service` opts into a LaunchAgent every ten minutes.
It scans configured/known repositories, runs recovery inspection, and applies
eligible completed-task hygiene. It does not fetch or merge.

Disable that service with:

```sh
launchctl bootout "gui/$(id -u)/org.cold-code-labs.worktree-steward"
```

Then remove its plist from `~/Library/LaunchAgents` if desired. On Linux, use an
operator-managed user timer or cron entry for the explicit command
`/absolute/path/to/wtpilot maintenance`. Scheduling setup is not automated there.

Remove only the installed launcher and optional skill files to uninstall. Keep the
state directory and source repositories while any task, archive or recovery ref is
needed. `installation.json` records paths; `installation-backups` preserves changed
files across reinstalls. Don't delete archived data merely to uninstall the CLI.

## Limits

One Unix user and a shared local registry are assumed. Locks coordinate Steward
operations; unrelated Git/filesystem mutations can bypass them. `lsof` cannot
establish application chat ownership, so application-owned worktrees are preserved.
Bare repositories are observation-only. This tool does not approve code, merge PRs,
prune branches, deploy, or replace backups.
