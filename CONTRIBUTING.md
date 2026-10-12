# Contributing

Use Python 3.12+ and the standard library only. Discuss larger changes in an issue;
small fixes and documentation improvements can go straight to a pull request.

Run the checks in README before opening a PR. Tests must use synthetic temporary
repositories and isolated state, never a contributor's real worktrees or credentials.
Run macOS checks locally and the Linux matrix in Woodpecker (`.woodpecker.yml`).
Keep container images pinned by digest. GitHub Actions is disabled and is not a
release gate; don't add workflows or wait for its historical failed runs.

Preserve the lifecycle invariants in AGENTS.md: uncertainty preserves work; unmanaged
worktrees need explicit adoption; expiry is not integration; archive and recovery
evidence precede removal; scripts/wt.sh retains repository authority. Add behavioral
regression coverage when changing ownership, evidence, removal or restoration.

Do not include local inventories, environment files, private repository names,
runtime state, installation backups or credential-bearing command output. Reproduce
bugs with fictional paths and minimal test repositories. Before publishing changes,
review both the diff and history and run a redacted secret scan, for example:

```sh
gitleaks git --redact --no-banner .
```

Contributions are licensed under the project's MIT license. There is no CLA.
