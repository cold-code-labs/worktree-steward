#!/usr/bin/env bash
# WTPILOT_SCRIPT_CONTRACT_V1
# Example for scripts/wt.sh in a repository whose integration branch is origin/main.
set -euo pipefail
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"
pilot=${WTPILOT_COMMAND:-wtpilot}
if [ "${WTPILOT_DELEGATED:-}" != "1" ]; then
  exec "$pilot" --repo "$REPO" legacy "$@"
fi
slug=${2:-}
[[ "$slug" =~ ^[a-z0-9][a-z0-9-]{0,79}$ ]] || { echo "Invalid task key" >&2; exit 1; }
root=${WTPILOT_WORKTREE_ROOT:?Steward must provide the workspace root}
workspace="$root/$slug"
case "${1:-}" in
  new)
    mkdir -p "$root"
    git worktree add -b "wt/$slug" "$workspace" origin/main
    ;;
  rm)
    "$pilot" --state "${WTPILOT_STATE:?}" verify-removal \
      "${WTPILOT_REMOVAL_TASK:?}" --path "$workspace" >/dev/null
    git worktree remove "$workspace"
    ;;
  *) echo "Delegated action unsupported" >&2; exit 1 ;;
esac
