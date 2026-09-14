"""A migration must not ALTER a table this repo never CREATES — unless it guards for absence.

FOUND BY BOOTSTRAPPING A SECOND INSTANCE, and unreachable before that:

    sqlalchemy.exc.ProgrammingError: relation "exec_position_state" does not exist
    [SQL: ALTER TABLE exec_position_state ADD COLUMN sessions_held INTEGER ...]

`exec_position_state` and `exec_action_log` are declared by KUMO-STRATEGIES and created by its
`create_all`. No `op.create_table` for either exists anywhere in `alembic/versions/`. So the migration
chain encoded an ordering dependency on another repo, and nothing asserted it.

It never surfaced because kumo-paper's database grew incrementally over months — both tables were long
present by the time these migrations ran. An EMPTY database had never been tried. The api crash-looped
on the first attempt to stand up `staging-ibkr`.

THE GUARD IS AIMED AT THE CLASS. Two migrations had it; the question is whether a THIRD can be added
without anyone noticing, and the answer must be no.

kumo-cockpit#486.
"""

from __future__ import annotations

import pathlib
import re

_VERSIONS = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _scan() -> tuple[set[str], dict[str, set[str]]]:
    created: set[str] = set()
    altered: dict[str, set[str]] = {}
    for f in sorted(_VERSIONS.glob("*.py")):
        t = f.read_text()
        created.update(m.group(1) for m in re.finditer(r'op\.create_table\(\s*["\']([a-z_]+)["\']', t))
        for m in re.finditer(r'op\.(?:add_column|alter_column|drop_column)\(\s*(?:_TABLE|["\']([a-z_]+)["\'])', t):
            name = m.group(1)
            if name is None:
                mm = re.search(r'_TABLE\s*=\s*["\']([a-z_]+)["\']', t)
                name = mm.group(1) if mm else None
            if name:
                altered.setdefault(name, set()).add(f.name)
        # RAW SQL COUNTS. 0016 creates a TRIGGER via `op.execute(...)`, which the structural patterns
        # above cannot see — so it passed this test and crash-looped the api on the next fresh boot.
        # Aiming at a class is only as good as the definition of the class, and `op.execute` was the
        # door left open.
        for m in re.finditer(r'(?:ON|INTO|UPDATE|FROM)\s+(exec_[a-z_]+)', t):
            altered.setdefault(m.group(1), set()).add(f.name)
    return created, altered


def test_the_scan_sees_the_migrations_it_judges():
    """An invariant over an empty set passes for the wrong reason."""
    created, altered = _scan()
    assert len(created) >= 5, f"only {len(created)} created tables found — the scan is not reading them"
    assert altered, "no ALTERs found at all — the guarantee below would be vacuous"


def test_every_migration_altering_a_FOREIGN_table_guards_for_its_absence():
    """The class, not the two instances of it.

    A table this repo never creates belongs to another repo, so a fresh database may not have it yet
    when migrations run. Altering it unguarded crash-loops the api on the FIRST boot of any new
    instance — and on an existing database it works forever, so nothing catches it until someone
    bootstraps.
    """
    created, altered = _scan()
    foreign = {t: files for t, files in altered.items() if t not in created}
    unguarded: list[str] = []
    for table, files in foreign.items():
        for name in files:
            # THE GUARD MUST BE CALLED FROM `upgrade`, not merely defined in the file. The first
            # version asserted `"has_table" not in text`, which survives deleting the CALL while the
            # helper function remains — a mutation escaped exactly that way. Fifth instance today of
            # matching a NAME rather than its USE.
            import ast

            tree = ast.parse((_VERSIONS / name).read_text())
            upgrade = next((n for n in ast.walk(tree)
                            if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None)
            if upgrade is None:
                unguarded.append(f"{name} has no upgrade()")
                continue
            guarded = any(
                (getattr(c.func, "id", "") in {"_table_exists"}
                 or getattr(c.func, "attr", "") == "has_table")
                for c in ast.walk(upgrade) if isinstance(c, ast.Call)
            )
            if not guarded:
                unguarded.append(f"{name} alters {table}")
    assert not unguarded, (
        f"{unguarded} ALTER a table this repo never creates, without checking it exists. On a fresh "
        f"database that raises `relation does not exist` and the api crash-loops — which is exactly "
        f"how staging-ibkr failed to bootstrap. Guard with `sa.inspect(op.get_bind()).has_table(...)`"
    )


def test_the_foreign_tables_are_the_ones_we_think():
    """Names them, so a NEW one is visible as a change rather than absorbed silently.

    This test earned itself immediately. It named TWO; widening the scan to see raw SQL surfaced a
    THIRD — `exec_strategy_state`, touched by 0016 via `op.execute("CREATE TRIGGER ... ON ...")` —
    and the failure is what forced the third guard rather than letting it be discovered by another
    crash-loop.

    If a new foreign table shows up here, someone has taken a dependency on another repo's schema and
    that is worth a decision rather than a passing test.
    """
    created, altered = _scan()
    foreign = {t for t in altered if t not in created}
    assert foreign == {"exec_position_state", "exec_action_log", "exec_strategy_state"}, (
        f"the set of foreign tables changed to {sorted(foreign)} — a migration now depends on another "
        f"repo's schema, or one of ours stopped being created here"
    )


def test_every_migration_module_actually_IMPORTS():
    """A migration that raises NameError at runtime passes every structural check in this file.

    `0016`'s guard called `sa.inspect(...)` in a module that never imported sqlalchemy. The
    guard-is-called test passed — it reads the AST and finds the call — and the api crash-looped with
    `NameError: name 'sa' is not defined` on the next boot.

    A test that a guard EXISTS is not a test that the guard can RUN.

    Resolves names with the AST rather than `co_names`: the first version flagged `now` in five
    migrations, which is `sa.func.now` — an ATTRIBUTE, not a global. A check that cries wolf on
    working code gets deleted by whoever is trying to land the next migration.
    """
    import ast
    import builtins

    failures = []
    for path in sorted(_VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text())
        module_names = {
            a.asname or a.name.split(".")[0]
            for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names
        }
        module_names |= {
            t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)
        }
        module_names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            local = {a.arg for a in fn.args.args} | {
                t.id for x in ast.walk(fn) if isinstance(x, ast.Assign)
                for t in x.targets if isinstance(t, ast.Name)
            } | {
                x.target.id for x in ast.walk(fn)
                if isinstance(x, (ast.For, ast.comprehension)) and isinstance(getattr(x, "target", None), ast.Name)
            }
            for name in (x.id for x in ast.walk(fn)
                         if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)):
                if name not in local and name not in module_names and not hasattr(builtins, name):
                    failures.append(f"{path.name}: {fn.name}() uses undefined name {name!r}")
    assert not failures, "migrations that cannot run:\n  " + "\n  ".join(sorted(set(failures)))
