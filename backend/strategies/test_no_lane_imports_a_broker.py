"""No lane may import a broker to resolve a symbol (#622, #608).

THE FAILURE, measured twice on ibkr-paper-retired — 2026-08-27 18:38 SGT and 2026-08-28 06:20 SGT:

    RuntimeError: APCA_API_KEY_ID / APCA_API_SECRET_KEY not set
      kumo_strategies/runtime/executor/tradable.py:45   symbols()
      strategies/momentum.py:309                        _instrument_ids()
      strategies/momentum.py:656                        _build_rotation()
      api/engine_node.py:6657                           build_node()

`build_node()` raises, so this is not a degraded lane — THE ENGINE CANNOT START. 13 restarts,
`/positions` serving [] against 22 non-flat positions held at the broker.

#608 names this the worst of the layering violations:

    strategies/momentum.py:311   from api.providers.alpaca.providers import EXCHANGE_TO_MIC

Every lane went through it — momentum.py:669, qc27.py:385, qc345.py:813 — so an IBKR-only instance
could not construct a single strategy without an Alpaca API key.

WHY IT WAS INVISIBLE: both tenants had a key, so the dependency never bound. Nothing could
distinguish "does not need Alpaca" from "needs Alpaca and happens to have one" — which is why the
credential must be ABSENT for this file to mean anything.
"""

from __future__ import annotations

import ast
import pathlib

_LANES = ("momentum.py", "qc27.py", "qc345.py")
#: VENUE-RESOLUTION vendors only — the defect that stops a node BOOTING.
_VENDORS = ("TradableUniverse", "EXCHANGE_TO_MIC", "_instrument_ids")

#: qc345's own /v2/assets GET is GONE (#647): the ETF/fund metadata now comes from the cache
#: (`Instrument.info` via api/providers/asset_meta.py) and the delisting ledger from the exec
#: provider's declared `reference_assets` capability. The literal sweep below keeps the whole class
#: out of every strategy module.


def _code_only(path: pathlib.Path) -> str:
    """Module source with EVERY docstring stripped.

    `ast.unparse` keeps docstrings as string constants, so a plain unparse still matches PROSE. An
    earlier version of this check failed on qc345's commentary about `TradableUniverse` while its
    code was clean — red for the wrong reason, which reads as proof and is worse than no test.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_the_fixture_points_at_files_that_exist():
    """FIXTURE PROPERTY FIRST. A typo'd lane name would make every assertion below pass against
    nothing — pinning the absence of a mechanism rather than its presence."""
    root = pathlib.Path(__file__).parent
    for lane in _LANES:
        assert (root / lane).is_file(), f"{lane} is not a lane in this repo any more"


def test_NO_LANE_reaches_a_broker_to_resolve_a_symbol():
    """THE DEFECT. Aimed at the class: any broker module reached from a lane is the same failure,
    and a fourth lane added later inherits this without anyone remembering to."""
    root = pathlib.Path(__file__).parent
    offenders = {}
    for lane in _LANES:
        src = _code_only(root / lane)
        hits = [v for v in _VENDORS if v in src]
        if hits:
            offenders[lane] = hits
    assert not offenders, (
        f"lanes still reach a broker to resolve symbols: {offenders}. An IBKR-only instance cannot "
        f"construct these strategies without an Alpaca API key (#622/#608)"
    )


def test_the_ALPACA_ONLY_universe_fetcher_is_gone_from_the_lane_path():
    """`TradableUniverse` GETs Alpaca's /v2/assets. It may survive as an Alpaca-only detail behind
    the Alpaca provider; it must not be reachable from a strategy."""
    root = pathlib.Path(__file__).parent
    assert not (root / "momentum.py").read_text().count("TradableUniverse()"), (
        "momentum still constructs TradableUniverse — that is the Alpaca HTTP call that took the "
        "node down (#595)"
    )


def test_lanes_hand_the_strategy_SYMBOLS_not_resolved_ids():
    """THE SEAM, and the half that would ship green. Deleting the import while still resolving ids
    some other way at build time changes nothing: the venue is a runtime fact and construction cannot
    know it. The lanes must pass symbols and let the strategy resolve at `on_start`.
    """
    root = pathlib.Path(__file__).parent
    for lane in _LANES:
        src = _code_only(root / lane)
        assert "symbols=symbols" in src or "symbols=list(symbols)" in src, (
            f"{lane} does not hand the strategy symbols — if it still passes resolved ids, something "
            f"had to resolve them before any adapter connected (#622)"
        )
        assert "instrument_ids=iids" not in src, f"{lane} still passes build-time resolved ids"


#: A hardcoded vendor URL or credential read in a strategy module is the SAME violation as a broker
#: import, one layer down — the import detector (api/test_import_boundary.py) cannot see a urllib
#: call to a string literal. #647 measured both: qc345.py:1044 sent every account's keys to
#: `paper-api` because the endpoint did not follow the account, and qc345_universe.py:76-77 read
#: APCA creds from a lane.
_VENDOR_LITERALS = ("alpaca.markets", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA-API-KEY-ID")

#: filename -> justification + the ticket that owns removing it. Same contract as
#: api/test_import_boundary.py's allowlist: every entry is load-bearing today and stale entries fail.
_VENDOR_LITERAL_ALLOWLIST: dict[str, str] = {
    "qc345_universe.py": "the MONTHLY universe-refresh substrate fetch (~5.5k symbols of daily "
                         "bars + /v2/assets) is documented Alpaca-only and runs from a scheduled "
                         "job, not the boot/decide path; neutral shape = a bulk-bars + "
                         "reference-assets provider seam (#647, #608)",
}


def _production_strategy_files() -> list[pathlib.Path]:
    root = pathlib.Path(__file__).parent
    return sorted(p for p in root.glob("*.py")
                  if not p.name.startswith("test_") and p.name != "conftest.py")


def test_FIXTURE_PROPERTY_the_literal_sweep_can_see_its_own_allowlisted_hit():
    """Non-empty is not complete, but a sweep that cannot see the KNOWN remaining vendor literal is
    blind — and a stale allowlist entry is an anchor on something absent."""
    hits = {p.name for p in _production_strategy_files()
            if any(v in _code_only(p) for v in _VENDOR_LITERALS)}
    stale = set(_VENDOR_LITERAL_ALLOWLIST) - hits
    assert not stale, f"allowlist entries matching no vendor literal — delete them: {stale}"
    assert "qc345_universe.py" in hits, (
        "the sweep no longer sees qc345_universe's Alpaca literals — either the refresh job went "
        "neutral (delete its allowlist entry) or the sweep is blind"
    )


def test_NO_strategy_module_hardcodes_a_vendor_URL_or_credential():
    """THE #647 CLASS. qc345 hardcoded `https://paper-api.alpaca.markets` and read APCA creds in the
    lanes layer: on ibkr-paper-retired (no Alpaca credential, by design) the assets frame was None and
    every session died at `_decide` with `assets are required`; on any future LIVE Alpaca deployment
    the live keys would have been sent to the PAPER endpoint, because a hardcoded URL does not
    follow the account. The metadata now comes from the cache (asset_meta) and the status ledger
    from the exec provider's declared `reference_assets` capability — a lane names no vendor."""
    offenders = {
        p.name: [v for v in _VENDOR_LITERALS if v in _code_only(p)]
        for p in _production_strategy_files()
        if p.name not in _VENDOR_LITERAL_ALLOWLIST
        and any(v in _code_only(p) for v in _VENDOR_LITERALS)
    }
    assert not offenders, (
        f"strategy modules hardcode vendor URLs/credentials: {offenders}. Route it through the "
        "provider layer (api/providers) or add an allowlist entry with a justification and the "
        "ticket that owns removing it (#647/#608)."
    )


def test_EVERY_lane_routes_its_pool_through_the_SAME_seam():
    """One seam for three lanes, or the rule drifts.

    `_lane_symbols` is where a lane's pool is finalised — the last point at which it is still just
    names. Found by mutation: momentum DEFINED it and never called it, so its own lane bypassed the
    shared path entirely while qc27 and qc345 used it, and deleting the call from momentum left the
    suite 304/304 green. A seam two of three lanes use is not a seam.
    """
    root = pathlib.Path(__file__).parent
    missing = [lane for lane in _LANES if "_lane_symbols(symbols)" not in _code_only(root / lane)]
    assert not missing, (
        f"these lanes do not route their pool through the shared seam: {missing}. Each then decides "
        f"independently what its tradable set is, which is how the three drift apart (#622)"
    )
