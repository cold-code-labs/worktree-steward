# Repository script contract

If a repository has `scripts/wt.sh`, that script remains the sole creation/removal
entrypoint. Steward will not bypass it with its generic Git lifecycle operations.
Unknown scripts are preserved and refused for management, even if they happen to
share a filename. The installer never patches repository scripts.

Adapt your script deliberately using [examples/wt.sh](../examples/wt.sh) as a
starting point. Adding the contract marker is an assertion of compliance, not
automatic verification of arbitrary shell code. Keep repository-specific checks
and test the adapter against a synthetic repository first.

The current versioned contract is intentionally narrow:

- Include `# WTPILOT_SCRIPT_CONTRACT_V1` in the script.
- On ordinary invocation, delegate to `wtpilot --repo "$REPO" legacy "$@"`.
- On `WTPILOT_DELEGATED=1`, execute your own `new SLUG` or `rm SLUG` lifecycle.
- Create `wt/SLUG` from `origin/main` at `$WTPILOT_WORKTREE_ROOT/SLUG`.
- Honor `WTPILOT_WORKTREE_ROOT` supplied by Steward; default is `~/wt/REPO_NAME`.
- Propagate every setup failure as a nonzero exit; don't report success after failure.
- Before delegated removal, call `verify-removal` with the supplied
  `WTPILOT_REMOVAL_TASK`, `WTPILOT_STATE`, and actual workspace path.
- Use ordinary `git worktree remove`; retain branches and never force removal.

Steward holds the repository lock during delegated calls. `verify-removal` is a
read-only validation under the parent's lock; don't recursively call another
mutating CLI operation in that path. It verifies prepared archives, original HEAD,
recovery refs, integration proof, ownership, files and process inactivity.

Invocation uses Bash and the primary checkout's script. Custom roots must also be
supplied when auditing/adopting existing script-backed worktrees. Once a task is
managed, archival uses its recorded workspace root. This contract currently does
not support another base branch, bare repositories, custom branch naming or restore.
Those cases stay preserved until a compatible adapter exists.

For an explicitly reviewed older adapter, local policy can set
`script_contract_marker` and `script_root_environment`. These are compatibility
assertions by the operator, not automatic detection or permission to manage an
unknown script. `script_retry_setup` maps registry repository IDs to argv arrays
that repeat setup performed inside the script if creation fails. Those commands
are stored with each task for recovery; they are not run a second time on a
successful creation. Keep this repository-specific policy private.

The script belongs to your repository: make changes through its normal code review,
then keep using `scripts/wt.sh new`, `ls`, `rm`, and `gc`. The public project's own
example is under `examples/`, so the project uses the generic lifecycle itself.
