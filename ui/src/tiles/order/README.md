# tiles/order

Order-entry tiles — the placeable order ticket variants over the shared order-core (`@/lib/order/payload`).

- `StrategyOrderForm.tsx` — **the strategy-first ticket (#51)**: strategy is the first selection and scopes
  the visible fields (the operator's contract). v1 = MANUAL only (MOMENTUM/ETF_AUTO shown disabled). The MANUAL body
  is assisted — entry mechanism + stop mechanism + risk % → levels/size PREFILL from the catalog (#65),
  compact + hand-editable. Rendered on-demand by the symbol detail surface (BUY/SELL). Recovers the retired
  OrderModal's assistance. Submits MANUAL via the #39 ack flow.
- `OrderVanillaTile.tsx` / `definition.ts` — **`order.vanilla`**: the plain broker ticket (side · market/limit/stop · qty · price/trigger · TIF · optional bracket). No prefill/AUTO — you type it. The raw variant; follows the focused instrument (or a pinned `instrument_id`).

Goes here: order-entry surfaces (strategy-first #51; vanilla; assisted #67 later) + their definitions.
Does NOT go here: the shared order-core (payload/validation/level-derivation live in `@/lib/order`, prefill
orchestration in `@/lib/prefill`) or the Orders blotter (`@/tiles/orders`). The legacy `OrderModal` is deleted
(#71); `VanillaForm` remains the raw body behind `order.vanilla`.
