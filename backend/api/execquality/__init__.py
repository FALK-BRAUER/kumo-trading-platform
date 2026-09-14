"""Execution quality (research/#210) — measured after the fact, never in the trading path.

Compares what we actually paid against the official opening auction print, because the backtest
fills at the 09:30 auction while the live runner submits at 09:35. Pure functions in `measure.py`
plus a CLI that appends to a CSV, so the sample accumulates toward the 30-60 sessions it needs.

Does not contain: anything that submits, cancels, or influences an order.
"""
