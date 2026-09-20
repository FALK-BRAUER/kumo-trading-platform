# bin/

Tree-level scripts that are about the REPOSITORY rather than about a running instance (the instances
repo holds those). `check-public-tree.sh` refuses any tree carrying what a public repository must never
hold — byte-identical in the private and the public tree. `export-to-public.sh` is the one-way private→
public snapshot (#1048). `hooks/` holds the Claude Code commit gate. Not for runtime code, deploy
scripts (`deploy/`) or backend tooling (`backend/scripts/`).
