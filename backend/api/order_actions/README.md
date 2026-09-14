# order_actions/

The order-action registry (#50) — pluggable order TYPES (WHAT order to build). Each action is a descriptor
`build(ctx, params) -> Order | OrderList` with a JSON-Schema `params_schema` (the future UI order-tile
contract). The engine's order-command path resolves an `action_id` and calls the descriptor; new types
register here with no core change.

- `registry.py` — `OrderBuildContext`, `OrderActionDescriptor`, `register_action`/`get_action`.
- `standard.py` — the standard 5 (market / limit / stop_market / stop_limit / bracket), extracted verbatim.

Descriptors are PURE: factory + resolved params → a Nautilus order object. They never fetch data, derive
prices (that's #65 prefill — WHERE prices sit), submit, or own strategy identity. Submission + the disarmed
gate stay in the engine.
