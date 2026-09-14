"""No broker-specific import above the connector, and no strategy import inside one (#608).

Operator, 2026-08-27: "Any broker specific code above the connector is a problem. Violates the idea of
the platform." Measured that day, one class, four instances: a strategy imported Alpaca's
EXCHANGE_TO_MIC (fixed, pinned by strategies/test_no_lane_imports_a_broker.py), the engine imported
the Alpaca connector, the IBKR connector imported a strategy, and a liveness signal only Alpaca's
tick stream could write. Each looked small; together they are why an IBKR tenant can be healthy and
report itself dead.

THE CLASS, not the instances: this walks the AST of every production module under api/ and asserts
    - nothing outside api/providers/ imports api.providers.alpaca.* or api.providers.ibkr*, and
    - nothing inside api/providers/ imports strategies.*,
minus an explicit allowlist in which every entry carries a one-line justification and the ticket
that owns removing it. A sixth instance fails here instead of being discovered during an outage.

Test files (test_*.py, conftest.py) are excluded from the walk: a test may import the vendor module
it tests — tests are not a shipped layer above the connector.

STALE ENTRIES FAIL TOO. An allowlist entry that no longer matches a real import is asserted on —
"assert the anchor was found" (2026-08-29): a guard anchored on something absent does nothing,
quietly, and the allowlist would otherwise only ever grow.
"""

from __future__ import annotations

import ast
import pathlib

_BACKEND = pathlib.Path(__file__).resolve().parent.parent  # backend/
_API = _BACKEND / "api"
_PROVIDERS = _API / "providers"

#: Broker packages that must not be named above the connector. `api.providers` itself (the neutral
#: spec layer: build_data_client_spec / build_exec_client_spec) and api.providers.databento /
#: api.providers.fmp are data vendors behind the neutral seam, not execution brokers — the defect
#: class in #608 is BROKER coupling, so the guard names the two brokers.
_BROKER_PREFIXES = ("api.providers.alpaca", "api.providers.ibkr")

#: (path relative to backend/, imported-module prefix) -> one-line justification + owning ticket.
#: Every entry is load-bearing today and each is a debt with a named neutral shape in #608.
_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "api/engine_node.py",
        "api.providers.alpaca.http",
    ): "today's-range + realized-periods sweeps are documented Alpaca-only, gated on "
       "data_provider=='alpaca'; neutral shape = a provider-neutral broker-history port (#608)",
    (
        "api/engine_node.py",
        "api.providers.alpaca.config",
    ): "default base URLs for the same Alpaca-only sweeps' AlpacaHttpClient (#608)",
    (
        "api/instrument_search.py",
        "api.providers.alpaca.http",
    ): "the in-process instrument catalog fetches Alpaca /v2/assets by design (#25); neutral shape "
       "= a per-provider catalog behind the api.providers spec (#608, #595)",
    (
        "api/instrument_search.py",
        "api.providers.alpaca.config",
    ): "default base URLs for the catalog's AlpacaHttpClient (#608)",
    (
        "api/instrument_search.py",
        "api.providers.alpaca.providers",
    ): "AlpacaInstrumentProvider builds the catalog's Equity objects (#608)",
    (
        "api/providers/ibkr.py",
        "strategies.rotation_from_cache",
    ): "the compass reference universe (#606) — the connector reaches UP for 'which instruments "
       "does this deployment need'; neutral shape = required_instruments() declared above the "
       "providers and consumed per-venue (#608)",
}


def _module_of(node: ast.stmt, file_pkg: str) -> list[str]:
    """Absolute dotted module name(s) imported by `node`, resolving relative imports."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level == 0:
            return [node.module or ""]
        # relative: climb `level` packages from the importing module's package
        base = file_pkg.split(".")
        base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
        stem = ".".join(base)
        return [f"{stem}.{node.module}" if node.module else stem]
    return []


def _production_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(
        p
        for p in root.rglob("*.py")
        if not p.name.startswith("test_")
        and not p.name.endswith("_test.py")
        and p.name != "conftest.py"
        and "__pycache__" not in p.parts
    )


def _imports_matching(files: list[pathlib.Path], prefixes: tuple[str, ...]) -> list[tuple[str, int, str]]:
    """(relative path, lineno, imported module) for every import whose module matches a prefix.

    `ast.walk` — so a function-level (lazy) import is found exactly like a top-level one. A lazy
    import is still a dependency; hiding one in a function must not clear the boundary.
    """
    hits: list[tuple[str, int, str]] = []
    for path in files:
        rel = path.relative_to(_BACKEND)
        file_pkg = ".".join(rel.with_suffix("").parts[:-1] or rel.with_suffix("").parts)
        tree = ast.parse(path.read_text(), filename=str(rel))
        for node in ast.walk(tree):
            for mod in _module_of(node, file_pkg):
                if any(mod == p or mod.startswith(p + ".") or mod.startswith(p) for p in prefixes):
                    hits.append((str(rel), node.lineno, mod))
    return hits


def _above_connector_hits() -> list[tuple[str, int, str]]:
    files = [p for p in _production_files(_API) if _PROVIDERS not in p.parents]
    return _imports_matching(files, _BROKER_PREFIXES)


def _inside_connector_hits() -> list[tuple[str, int, str]]:
    return _imports_matching(_production_files(_PROVIDERS), ("strategies",))


def _allowed(rel: str, mod: str) -> bool:
    return any(rel == a_rel and (mod == a_mod or mod.startswith(a_mod + ".")) for a_rel, a_mod in _ALLOWLIST)


def test_the_walker_actually_finds_imports():
    """FIXTURE PROPERTY FIRST. A walker that finds nothing would make every assertion below pass
    against nothing — 'empty IS the bug, so the bug cannot be what identifies it' (2026-08-29).
    Two known-good, deliberately-remaining imports must be seen: instrument_search's Alpaca client
    (above the connector) and ibkr's rotation reach-up (inside it, function-level — proving lazy
    imports are found)."""
    above = _above_connector_hits()
    assert ("api/instrument_search.py", 23, "api.providers.alpaca.http") in above, (
        f"the walker no longer sees instrument_search's Alpaca import — it finds: {above[:5]}. "
        "Either that module went neutral (delete its allowlist entries) or the walker is blind."
    )
    inside = _inside_connector_hits()
    assert any(rel == "api/providers/ibkr.py" and mod == "strategies.rotation_from_cache"
               for rel, _ln, mod in inside), (
        "the walker no longer sees ibkr's function-level strategies import — either #608's neutral "
        "shape landed (delete the allowlist entry) or the walker misses lazy imports."
    )


def test_every_allowlist_entry_is_still_load_bearing():
    """A stale allowlist entry is an anchor on something absent. When the debt it names is paid,
    the entry must be deleted in the same change — otherwise the hole outlives the reason."""
    hits = _above_connector_hits() + _inside_connector_hits()
    stale = [
        (a_rel, a_mod)
        for (a_rel, a_mod) in _ALLOWLIST
        if not any(rel == a_rel and (mod == a_mod or mod.startswith(a_mod + ".")) for rel, _ln, mod in hits)
    ]
    assert not stale, f"allowlist entries matching no import — delete them: {stale}"


def test_NOTHING_above_the_connector_imports_a_broker():
    """THE DEFECT, direction one: the platform naming a vendor. #608 instances 1 and 2."""
    offenders = [(rel, ln, mod) for rel, ln, mod in _above_connector_hits() if not _allowed(rel, mod)]
    assert not offenders, (
        f"broker-specific imports above the connector: {offenders}. Vendor dies at the adapter seam "
        "(CLAUDE.md); either make the seam neutral or add an allowlist entry with a justification "
        "and the ticket that owns removing it (#608)."
    )


def test_NO_connector_imports_a_strategy():
    """THE DEFECT, direction two: the connector reaching UP. #608 instance 3 — 'which instruments
    does this deployment need' must have ONE answer, declared above the providers."""
    offenders = [(rel, ln, mod) for rel, ln, mod in _inside_connector_hits() if not _allowed(rel, mod)]
    assert not offenders, (
        f"connectors importing strategies: {offenders}. Declare the requirement above the providers "
        "instead (#608)."
    )
