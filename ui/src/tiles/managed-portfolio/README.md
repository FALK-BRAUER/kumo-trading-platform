# managed-portfolio/

The Managed Portfolio tile (#77) — the managed book. A net-anchor row per instrument that drills down to its
per-strategy trade cycles (from the `trades` DataSource / TradeDTO), with the dual accounting lens (deployed
vs engaged). Replaces the flat `portfolio` position list as the default Portfolio view.

- `ManagedPortfolioTile.tsx` — the tree-table component (net-anchor → cycle drill-down + dual-lens header).
- `definition.ts` — tile type, `dataSources: ["trades","account"]`, `chrome: false`.

Holds only this tile. Shared card/table primitives live in `@/components`; the flat position list is the
separate `portfolio` tile.
