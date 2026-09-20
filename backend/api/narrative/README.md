# api/narrative/

Verification for the session-narrative feature (research stage, `research/session-narrative-prompt.md`).

Holds the CHECKER that any generated narrative must pass before it is sent — written before the
feature on purpose, because it doubles as the eval harness and is useful with no model wired in.
`check.py` is pure: a structured narrative plus a journal evidence bundle in, violations out.

Does not hold prompts, API clients, or anything that calls a model. If a claim cannot be traced to a
bundle field, it belongs nowhere near here.
