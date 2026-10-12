# Security and private data

Worktree Steward runs as your Unix user and trusts your local Git repositories,
repository scripts, setup commands and agents. It is not a sandbox. Keep its state
on a private local filesystem and share one state directory among participating
agents. It cannot coordinate users or machines with independent registries.

The runtime database and audit reports include paths, branch names, process IDs and
task metadata. Ignored-file archives can contain real secrets, including `.env`
contents. File permissions protect local access but do not encrypt data or prevent
your backup/sync tooling from copying it. Don't publish these artifacts or push
recovery refs containing private commits to a public remote.

For suspected security vulnerabilities, use GitHub's private vulnerability reporting
on this repository's Security tab. Do not open a public issue containing exploitable
details or private data. Provide a minimal synthetic reproduction and affected
version. The latest release is the supported version; this early project makes no
guarantee of a response deadline.

If a secret is accidentally published, revoke or rotate it immediately. Removing
the file from the latest commit does not remove it from Git history or existing clones.
