# ds — design-system primitives

Shared, tile-agnostic UI primitives (#104): `DataTable`/`DataRow` (the unified list row), `Sparkline`,
`TrendStrip`, `StatusBadge`, `SlideToConfirm`, plus their pure logic helpers (`trend.ts`, `pnl.ts`,
`status.ts`) tested alongside the component that uses them.

Not here: concrete tile components (`@/tiles`) or per-tile layout wiring — those consume these primitives,
never the other way round.
