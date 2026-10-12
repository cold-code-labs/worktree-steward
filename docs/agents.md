# Agent setup

`python3 install.py --skills` installs the same skill for Codex, Cursor and Claude
Code. Alternatively copy `skills/worktree-hygiene` into your agent's skills
directory. Restart/reload the agent as needed for skill discovery.

For an agent using a different installation path, make `wtpilot` available on its
PATH. The skill is self-contained and links to the public operations guide.

Add this optional snippet to your repository's `AGENTS.md`, `CLAUDE.md`, or Cursor
rules according to your team's convention. Installation does not add it automatically.

```text
Use Worktree Steward (wtpilot) for task workspaces. Reuse the same task key.
Repositories with scripts/wt.sh retain that creation/removal entrypoint.
Otherwise: wtpilot --repo <repo> new <task-key> --base <integration-ref>.
Claim with a real long-lived agent PID and stable owner before editing,
or launch the agent with: wtpilot run <task-id> -- <agent-command>.
Release with the same owner. Release is not completion.
After integration: wtpilot finish <task-id> --into <integration-ref>.
Existing unmanaged worktrees need explicit adoption for lifecycle management.
Never bypass refusals with force, reset, clean, process killing or Git-lock deletion.
No fixed concurrent agent or worktree count limit.
```

For the maintainer's environment, add a global CI rule: use Woodpecker for CI and
Coolify for deployable services; never create or enable GitHub Actions, or use
historical GitHub Actions runs as a delivery gate. Keep infrastructure addresses
and tokens outside the public skill and repository.

Updating the skill files prepares all three agents for subsequent skill loads.
An already-running conversation may retain instructions loaded earlier; reload
the skill in that conversation before its next lifecycle action. Do not restart
or terminate a live agent to refresh its context.

Using the skill does not itself authorize cleanup. An audit asks for inspection;
a cleanup request authorizes applying eligible findings. Keep live owners and
pending work protected even when integration is proven. State and archive artifacts
are private: don't paste their contents into public issues or agent messages.
