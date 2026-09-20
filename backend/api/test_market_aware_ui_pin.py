"""The UI's market-aware vocabulary is the backend's (#873) — pinned ACROSS THE LANGUAGE SEAM.

Two derivations of one vocabulary (Python `api.market_aware`, TypeScript `marketAwareBadge.ts`) will
drift; the bps rounding case already showed the JS/Python seam is where that happens. The TS file is
parsed as text here so a state, action or label added on one side without the other fails this test
rather than rendering blank or, worse, calm.
"""
from __future__ import annotations

import re
from pathlib import Path

from api.market_aware import ACTIONS, LABELS, READING_STATES

_TS = Path(__file__).resolve().parents[2] / "ui" / "src" / "tiles" / "book" / "marketAwareBadge.ts"


def _ts_array(name: str) -> list[str]:
    m = re.search(rf"export const {name} = \[([^\]]*)\] as const;", _TS.read_text())
    assert m, f"{name} not found in {_TS.name}"
    return re.findall(r'"([^"]+)"', m.group(1))


def _ts_yes_labels() -> dict[str, str]:
    m = re.search(r"export const YES_LABEL = \{([^}]*)\} as const;", _TS.read_text(), re.S)
    assert m
    return dict(re.findall(r'(\w+): "([^"]+)"', m.group(1)))


def test_FIXTURE_the_ts_file_exists_and_declares_the_three_arrays():
    assert _TS.exists()
    assert _ts_array("READING_STATES") and _ts_array("ACTIONS") and _ts_array("CONTAINER_STATES")


def test_reading_states_and_actions_are_the_same_on_both_sides():
    assert tuple(_ts_array("READING_STATES")) == READING_STATES
    assert tuple(_ts_array("ACTIONS")) == ACTIONS


def test_the_badges_acting_labels_are_the_contracts_labels():
    labels = _ts_yes_labels()
    assert labels["entries_blocked"] == LABELS["entries_blocked"]
    assert labels["emergency_exit"] == LABELS["emergency_exit"]
    assert "stand" in labels["self_assessment"] and "quarantine" not in labels["self_assessment"]


def test_the_tone_map_names_every_state_on_both_lists():
    src = _TS.read_text()
    tone = re.search(r"export const TONE: Record<BadgeState, string> = \{([^}]*)\};", src, re.S)
    assert tone
    keys = set(re.findall(r"^\s*(\w+):", tone.group(1), re.M))
    assert keys == set(READING_STATES) | set(_ts_array("CONTAINER_STATES")), keys
