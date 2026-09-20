# Release order — the first public flip

Done 2026-09-20: strategies published (`v0.0.0`), this platform published against it. Kept as the record of
the order and what CI gates.


Two repositories go public together, and the ORDER is load-bearing: this platform's backend
depends on the strategies package by a git tag, so the tag must exist before this tree can
install.

1. **kumo-trading-strategies first.** Make its first public commit, then tag it `v0.0.0` on that
   commit and push the tag. The distribution name inside its `pyproject.toml` is
   `kumo-trading-strategies` (read 2026-09-20); the pin below uses the DIST name, not the repo name.
2. **kumo-trading-platform second.** In `backend/pyproject.toml`, uncomment the dependency line
   marked `TODO-flip`:
   `"kumo-trading-strategies @ git+https://github.com/FALK-BRAUER/kumo-trading-strategies.git@v0.0.0"`
   and make the first public commit with it live. `instances/example/versions.lock` names the same
   tag (`kumo-trading-strategies=v0.0.0`); the two must agree.
3. **Prove it from a stranger's clone**, not the exporting machine: `cd backend && uv sync
   --all-extras && uv run pytest -q` must collect and pass with `kumo_strategies` imported from the
   tag — 73 test modules import it, and until the tag resolves they cannot be collected.

Until step 1, `uv sync` succeeds and the suite cannot run; that is the intended, visible state.

## What the CI gates on day one
- `backend.yml`: `uv sync --all-extras`, `ruff check . --exit-zero --statistics` (reported, not
  blocking — the tree carries ~700 ruff findings; the count is ratcheted down before the flag goes),
  `pytest -q`.
- `merge-gate.yml`: `scripts/merge_gate.py` with `github.token` only — no secrets, but it needs the
  strategies pin to resolve (step 1).
- `public-tree.yml`: `bin/check-public-tree.sh` — 0 hits required.
- `ui.yml`: `npm ci`, `tsc --noEmit`, `vitest run`.
No workflow reads a repository secret.
