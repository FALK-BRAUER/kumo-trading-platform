"""The ONE table of lane settings universes (#871).

Every lane whose universe lives in the `strategies` settings domain is declared here once — gate key
and universe key — so the IB connector's `load_contracts` and each lane's own symbol reader are two
readers of ONE list, never two lists. Measured on staging2 2026-09-10: `QC27_UNIVERSE` held 138 names,
the IB clients loaded contracts for the feed.toml snapshot ∪ pool only, and TECHIVOL-005 booted
"125 of 138 symbols have no instrument on this venue" while reading armed and TRADING. #511 fixed the
same failure for the POOL; the lanes whose universes are SETTINGS were forgotten.

Neutral module on purpose: the connector may not import a strategy module and a strategy module may
not import a connector (`test_import_boundary`, `test_no_lane_imports_a_broker`).
"""
from __future__ import annotations

#: (gate key, universe key) per settings-universe lane. ORDER IS NOT SIGNIFICANT.
LANE_UNIVERSES: tuple[tuple[str, str], ...] = (
    ("QC27_ENABLED", "QC27_UNIVERSE"),
    ("QC345_ENABLED", "QC345_UNIVERSE"),
    ("CRSI_ENABLED", "CRSI_UNIVERSE"),
)


def norm(symbol) -> str:
    """One spelling rule wherever a settings universe meets another set."""
    return str(symbol).strip().upper()


def universe_symbols(values: dict, universe_key: str) -> list[str]:
    """One lane's settings universe, normalised, sorted, de-duplicated. Empty is empty — a lane
    decides for itself whether that is a boot failure."""
    raw = (values or {}).get(universe_key) or []
    return sorted({norm(s) for s in raw if str(s).strip()})


def enabled_lane_universe_symbols(values: dict) -> dict[str, list[str]]:
    """{universe_key: symbols} for every lane whose GATE is on. A disabled lane's names are not
    loaded: contract resolution on IB is serial and paced, and 138 needless lookups cost the connect
    budget of the lanes that do run."""
    values = values or {}
    return {ukey: universe_symbols(values, ukey)
            for gkey, ukey in LANE_UNIVERSES if bool(values.get(gkey))}
