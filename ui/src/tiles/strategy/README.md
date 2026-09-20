# tiles/strategy

The strategy's own state — lifecycle, the last decision, what needs the operator (#212).

Every other tile here describes the ACCOUNT: what is held, what was ordered, what it is worth. This
one describes the MACHINE: whether it is trading, what it decided, whether intent matched outcome,
and which held positions have exits that cannot fire. Reads the `session` frame; renders only.

Does not contain: controls. Nothing here submits, cancels, pauses or arms. Display logic that can be
tested without a DOM belongs in `@/lib/strategy/summary`, not in the component.
