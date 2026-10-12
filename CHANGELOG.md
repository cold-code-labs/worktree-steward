# Changelog

## 0.1.0 — 2026-10-12

First public preview of Worktree Steward, released under MIT.

- SQLite task registry, repository locks and stable task-key reuse.
- Supervisor ownership, PID identity, heartbeats and per-task runtime namespaces.
- Explicit adoption, audit decisions and guarded archival of proven-integrated work.
- Ancestry and optional exact complete squash-delta integration evidence.
- Private ignored-file archives, recovery refs and generic worktree restoration.
- Shared Codex, Cursor and Claude Code skill.
- Opt-in macOS scheduling and a versioned repository-script contract.
- 58 behavioral tests, passing locally on macOS with Python 3.12 and 3.14.
- Configured macOS/Linux CI matrix and a pinned, checksummed Git-history secret scan.

Linux validation in hosted CI is pending. Existing local installations are not
automatically migrated; public defaults use a separate state directory, explicit
scan roots and no discarded ignored directories.
