"""Session-narrative verification (research stage).

The narrative feature itself is not built. This package holds the CHECKER that any generated
narrative must pass before it is sent anywhere — written first on purpose, because it doubles as the
eval harness and is useful with no model wired in at all.

Contains: `check.py` (pure verification of a structured narrative against a journal evidence bundle).
Does not contain: prompts, API clients, or anything that calls a model.
"""
