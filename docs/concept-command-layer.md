# Concept — Command layer (UI → engine control plane)

## Why
The #20 split (engine process ≠ api process, for crash isolation) has only a one-way pipe: engine → UI
(`ui:stream`, events). Everything the user *does* — view a symbol, buy/sell, enable a lane — needs the
reverse: **UI → engine commands**. Today there's no channel; the on-demand streaming "fix" is a poll-based
Redis-key patch. The command layer is the general control plane that:
- retires the streaming poll patch (stream-request becomes a command),
- carries **orders** (buy/sell/cancel/modify) — issue #5 rides on this,
- carries **lane control** (enable/pause MANUAL/ETF_AUTO/BCT_AUTO) + future automation ideas,
- is the reverse of the `ui:stream` data plane, built the same proven way.

Decision (Operator, 2026-07-08): **keep the #20 split, build the command layer.** (The one existing Nautilus
web UI, Black101081/Nautilus-Web-Interface, avoids it by running single-process + direct calls — not our
model; we chose isolation, this is its cost.)

## Native vs hand-built (wheel check, 1.229.0)
Nautilus IS message-driven; commands (`SubmitOrder`/`CancelOrder`/`ModifyOrder`/`SubscribeBars`) are
first-class bus messages. The wheel exposes the external-streaming machinery — MORE than the #19 note said:
- `MessageBusConfig.external_streams`, `stream_per_topic`, `streams_prefix`, `types_filter`
- Redis bus backing via `DatabaseConfig(type="redis", …)`
- `TradingNode.add_stream_processor(callback)` (ingress hook) + `publish_bus_message(BusMessage)`

**Open question (for Codex):** does configuring `external_streams` + Redis + `add_stream_processor`
actually AUTO-ROUTE an external command message onto the internal msgbus → exec engine (so a published
`SubmitOrder` executes)? Or does `add_stream_processor` just hand us raw `BusMessage`s we must translate
ourselves? Perplexity: "Rust-native factory wiring from config to a backing remains the caller's
responsibility" + no Python turnkey + no web-UI example → the native auto-route is uncertain.

- **A — Native external ingress.** Configure the engine's `MessageBusConfig` for Redis external streaming;
  the api publishes serialized Nautilus command `BusMessage`s; the node consumes them onto its bus. Most
  idiomatic IF the auto-route works. Risk: wiring uncertainty, schema coupling to Nautilus's serde types,
  Python turnkey gap.
- **B — Hand-built command consumer (recommended default).** Mirror our proven egress bridge in reverse:
  api XADDs typed commands to a Redis Stream `ui:commands`; a consumer in the engine `XREAD`s it and
  dispatches each typed command to the Nautilus action (`self.submit_order(...)`, `self.cancel_order(...)`,
  `_load(...)`). Full control, no wheel-wiring uncertainty, reuses the pattern we already run. Command
  *semantics* stay Nautilus-native (we build orders via `OrderFactory`/`SubmitOrder`); only the transport
  is ours.

Recommend **B** unless the wheel check proves native auto-route works cleanly (then reconsider A).

## Design (Option B)
- **Transport:** Redis Stream `ui:commands` (api producer, engine consumer) — the mirror of `ui:stream`.
- **Envelope:** `{id, type, payload, ts}` (JSON). `id` = client-generated, for ack + idempotency.
- **Command types:**
  - `stream_request` `{instrument_id}` → `_load` (retires the `ui:streamreq` poll key).
  - `submit_order` `{instrument_id, side, quantity, order_type, price?, trigger_price?, tif, client_order_id}`
    → build via `OrderFactory` on the **MANUAL** lane → `submit_order`.
  - `cancel_order` `{client_order_id}` → `cancel_order`.
  - `modify_order` `{client_order_id, quantity?, price?}` → `modify_order`.
  - (future) `lane_enable`/`lane_pause` `{lane}`; more as ideas land.
- **Engine consumer:** reads `ui:commands` (own thread `XREAD BLOCK`, like the writer thread), and
  dispatches each command **onto the trading loop** (`run_coroutine_threadsafe`) so Nautilus calls run
  on-loop. Unknown/malformed → logged + `command_ack` error (never crashes the loop).
- **Ack / results:** over the existing `ui:stream` — a `command_ack` `{id, status, error?}` frame; order
  lifecycle flows as **events** (accepted/filled/rejected) → the UI. Requires wiring **fills** back
  (`node.fills()` is `[]` today — the order path fills it in).

## Safety (non-negotiable — order commands)
- **Paper-only** on this stack (the exec/gateway is already off the api's reach).
- **No autonomous BUYs** — every order command originates from an explicit human action (a UI slide-to-buy
  confirm; carry the kumo-trader live-safety discipline).
- **Human unlock / arm** — orders gated behind an explicit arm toggle (default off, opt-in).
- **Idempotency** — `client_order_id` dedup so a command retry (or double-consume) can't double-submit.
- **Validation** — the engine validates every command against a schema; rejects unknowns.
- The command stream carries orders; keep it isolated (bus network only, no external exposure).

## Increments
1. **Command channel infra** — Redis `ui:commands` + engine consumer + `command_ack`, with `stream_request`
   as the first command (retires the poll patch; proves the channel end-to-end on a *benign* command).
2. **Orders (= #5)** — `submit_order`/`cancel_order`/`modify_order` + MANUAL-lane wiring + fills-back + the
   UI order ticket (`OrderModal`, currently inert) + slide-to-buy + the safety gates above.
3. **(future)** lane control + advanced automation commands.

## Questions (Perplexity + Codex)
1. Native (A) vs hand-built (B): does the 1.229.0 external-ingress auto-route commands to the exec engine,
   or is B the safer call? What exactly does `add_stream_processor` deliver?
2. Building orders in the engine: `OrderFactory` + `submit_order` from a Strategy — correct construction for
   a MANUAL discretionary order (market/limit/stop, TIF, optional bracket)? Which lane/StrategyId?
3. Ack/result: reuse `ui:stream` for `command_ack` + order events, or a dedicated response stream?
4. Idempotency + ordering guarantees on a Redis Stream consumer; exactly-once vs at-least-once for orders.
5. Reconcile with the existing exec client (Alpaca) — the order goes Nautilus MANUAL lane → Alpaca exec;
   confirm the path and the fills-back wiring.

## Out of scope (this concept)
- The full order-ticket UI polish + slide-to-buy (increment 2 / #5). Lane control (increment 3).
