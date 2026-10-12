# Worktree Steward

- Python standard library only; no service, LLM or network is required for maintenance.
- No hard cap on concurrent agents or worktrees. Serialize short repository mutations,
  not agent execution. SQLite `BEGIN IMMEDIATE` protects shared resource allocation.
- Existing unmanaged workspaces are observation-only. Explicit adoption is required.
- Age or heartbeat expiration alone never authorizes deleting work.
- Archive only explicitly completed and proven-integrated work, preserving ignored assets
  and a reachable Git ref. Refuse dirty, active, locked, primary and changed worktrees.
- Never introduce automatic force removal, reset, clean, PID killing or Git lock deletion.
- Repositories with `scripts/wt.sh` must use it for creation and removal.
- Test real concurrent processes, failure paths and restoration; do not test source text.
- Checks: `python3 -m unittest discover -s tests -v` and
  `python3 -m compileall -q wtpilot.py audit.py integration.py install.py tests`.
- Local delivery only unless publishing is explicitly requested. Keep private state,
  installation backups and credentials out of this source directory.
