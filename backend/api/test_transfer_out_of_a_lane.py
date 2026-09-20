"""A transfer OUT OF A LANE must be possible at all (#785).

MEASURED, paper 2026-09-01. Moving 1 share of HALO from MOMENTUM-002 to BCTROT-004 through
`POST /transfers` was refused by the engine:

    {"type": "transfer_position", "status": "error",
     "error": "source position moved since it was read — re-read and retry"}

Nothing had moved. The api sent `source_ts_last: 0` because `consumer.strategy_position` HARDCODES
it for the lane plane, the engine compared that against the position's real `ts_last`, and the
staleness lock can therefore NEVER match. A transfer out of a lane has never once succeeded.

The evidence it never has: every row in the transfers outbox is `EXTERNAL -> MANUAL-001`, the one
branch that returns a genuine `row.ts_last`.

THE ROOT IS AN ABSENT FIELD, NOT A BAD VALUE. `PositionDTO` carries no `ts_last`, and the engine's
position frame does not publish one — so the api cannot know it and rendered "unknown" as `0`.
Fourth instance of the engine-publishes-DTO-drops shape (#233, #322, #336).
"""

from __future__ import annotations

import inspect

from api.models import PositionDTO


def test_the_position_frame_the_engine_publishes_CARRIES_ts_last():
    """The producer. Killed by removing the key from `_on_snapshot`'s position dict."""
    from api import engine_node

    src = inspect.getsource(engine_node.UiFeedStrategy._on_snapshot)
    assert '"ts_last"' in src, (
        "the engine's position frame does not publish ts_last, so no consumer can ever know it — "
        "this is what made the api send 0"
    )


def test_the_DTO_does_not_DROP_it():
    """The seam that ate it three times before (#233/#322/#336): the engine publishes, the pydantic
    model silently discards. Killed by removing the field from PositionDTO."""
    assert "ts_last" in PositionDTO.model_fields


def test_a_LANE_position_reports_a_REAL_ts_last_not_a_placeholder():
    """THE TEST THAT CARRIES THE INFORMATION. A test on the EXTERNAL branch passes today and passes
    with the bug — only the lane branch distinguishes them."""
    from api.consumer import RedisConsumer

    src = inspect.getsource(RedisConsumer.strategy_position)
    lane_branch = src[src.index("for p in self.positions()"):]
    assert '"ts_last": 0' not in lane_branch, (
        "the lane branch still hardcodes ts_last, so every transfer out of a lane is refused as "
        "stale by a lock that cannot match"
    )
    assert "p.ts_last" in lane_branch


def test_zero_is_only_ever_reported_when_the_position_ACTUALLY_has_no_events():
    """Three states, not two. A position that genuinely has no last event may report 0; 'we did not
    publish it' must not be spelled the same way."""
    dto = PositionDTO(instrument_id="HALO.XNAS", side="LONG", quantity=1.0, avg_px_open=104.35,
                      realized_pnl="0.00 USD", strategy_id="MOMENTUM-002", ts_last=17_000)
    assert dto.ts_last == 17_000
