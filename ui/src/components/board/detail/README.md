# components/board/detail — detail surface variants

One component per focused-entity KIND, registered in `registrations.tsx` and resolved by the
`DetailRegistry` on `(kind, strategy)`.

- `PositionDetail` — a holding. Reads the trade-cycle plane first (state, cycle realized P&L), falling back
  to the flat positions source. Management actions (flatten/trim/stop) are #170, not here yet.
- `UnclaimedPositionDetail` — an unattributed broker position; composes the `position-transfer` tile.
- `OrderDetail` — one order.

A detail COMPOSES tiles rather than hand-rolling forms — see `UnclaimedPositionDetail`. Symbol focus is
served by `SymbolDetailSurface` one level up.

Not here: the registry itself (`@/lib/framework/detail`), or tiles (`@/tiles`).
