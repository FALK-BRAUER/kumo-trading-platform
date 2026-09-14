# tiles/orders

The **Orders blotter** tile (#33) — IBKR Orders-tab style working-order screen.

- `OrdersTile.tsx` — the blotter table (working orders first, then recent terminal) + inline Modify modal.
  Cancel/Modify call the command layer via `client.ts` (`cancelOrder`/`modifyOrder`).
- `definition.ts` — the tile contract (type `orders`, binds the `orders` DataSource, config schema).

What goes here: order-lifecycle UI (status, cancel/modify, later brackets). What doesn't: order *entry*
(that's `tiles/order` — the `order.vanilla` ticket), positions (that's `tiles/portfolio`), the data
plumbing (engine `_order_frame` → `orders` WS source in `@/config/datasources`).
