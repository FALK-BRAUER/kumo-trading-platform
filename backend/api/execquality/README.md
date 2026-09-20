# api/execquality/

Measures what we actually paid against the official opening auction print (research/#210) — after
the fact, from two read-only Alpaca endpoints. **Nothing here runs in the trading path.**

`measure.py` holds pure functions (official-open selection, partial-fill aggregation, lag, signed
drift, idempotent CSV append); `__main__.py` is the CLI that fetches and records. The sample needs
30-60 trading days before the drift number means anything, so the job is accumulation.

Does not contain: anything that submits, cancels, or influences an order.
