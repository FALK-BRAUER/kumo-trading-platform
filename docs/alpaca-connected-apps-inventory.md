# Alpaca Connected-Apps Inventory + fit assessment

> Captured 2026-07-03 from Alpaca's "Connected Apps" marketplace. Purpose: is there an **off-the-shelf**
> Alpaca app that covers kumo-cockpit's need (manual equity/ETF cockpit + light Nautilus automation),
> so we don't build?

## What kumo-cockpit actually needs (the yardstick)
- Manual, human-in-the-loop equity/ETF **cockpit UI** (config-driven tiles, charts, watchlist, portfolio, order ticket).
- A **Nautilus engine** underneath for the automation lanes (MANUAL / ETF_AUTO / BCT_AUTO), cross-lane risk, backtest↔live parity.
- **Bespoke analysis**: Ichimoku, fintrack gap-up / falling-knife, Blue-Flag 8-condition, sT10e entries.
- No-autonomous-BUY-without-unlock discipline.

An off-the-shelf app is "good for us" only if it covers a meaningful chunk of that **without** us giving up the differentiators (custom analysis + Nautilus lanes + config layouts).

## Inventory (categorized; ✅ relevant · ~ partial · ✗ not our need)

### Trading workstations / charting UIs — closest to the cockpit
| App | What | Fit |
|---|---|---|
| **Lama Trader** | Modern Alpaca **workstation**: AI research, advanced charting, watchlists, portfolio insights, analytics, order entry, customizable workspace | ✅ closest off-the-shelf to our manual UI |
| **Medved Trader** | Pro multi-asset terminal: charting, DOM/Level II trading, candle-pattern detection, real-time scanners, custom hotkeys, multi-monitor | ✅ serious manual terminal |
| **TradingView** (+ TradersPost / SignalStack for Alpaca exec) | Dominant charting + manual trade + alerts; 30M users | ✅ charting/manual, huge ecosystem |
| **Trade-Ideas Brokerage Plus** | AI decision support, scanners, chart-based trade assistant, risk mgmt | ~ decision support, not a full cockpit |
| **iVest Plus** | Visual stock+options platform, scoring algo, scanners, P/L, what-if | ~ leans options |
| **Market Gear** | Options charting + multi-leg ticketing + covered-call/IC scanners | ~ options (tradex domain) |
| **PowerX Optimizer** | Charting + scanner for the PowerX strategy | ✗ single-strategy |

### Algo / no-code bot builders (automate strategies — NOT a manual cockpit)
TradersPost · SignalStack · CandleX · Breaking Equity · MachineTrader (Node-RED) · StockHero · Trellis · Signal Mixer · Investfly · VibeTrader · TradeLab · Amaltash · QuantMage · AlgoBulls · TradeWithUs · incite → **✗** (bot automation, not our human-in-loop UI; overlaps only the future auto-lanes, which we do in Nautilus)

### AI signal / assistant services (signals or chat, you execute)
WaZii · TraderGPT · TraderVoice.ai · Ensemble AI · Thriving · QuantML · JEDI AI · Makecents · Atlas Trade AI · NexusTrade · Ai-Trading.biz · Aether Quant · Haiphen · CPZAI · Zero Wall Street · Riches Reach AI · Aizeum (voice) · Gogi (agent/chat) · tradervoice → **✗/~** (signal generators; not a cockpit — but a reference for the AI-chat trade UX direction, e.g. Gogi/Aizeum/Lama)

### Backtest / research engines (Nautilus/LEAN peers)
| App | Note |
|---|---|
| **QuantConnect** | Full research/backtest/live algo platform, supports Alpaca equities/options/crypto. **You already run LEAN (kumo-qc).** A Nautilus alternative for the *engine* layer. | ~ notable |
| Breaking Equity · CPZAI | IDE/algo-marketplace + systematic OS | ✗ |

### Portfolio mgmt / analytics
Feather Finance (pro portfolio analytics on Alpaca) · Ziggma · PortfolioShield (options risk) · DRAVO · PRAAMS · Algo Trade Analytics (TV↔Alpaca execution-quality) · Uncharted Investments → **~** (analytics tiles inspiration; not a cockpit)

### Options-specialist
PutHouse · Haiphen · PortfolioShield · AI Velocity Trading (30+ preset option strategies) · Market Gear · iVest Plus → **✗** (options = tradex domain, not kumo-cockpit)

### Consumer / roundup / thematic / niche
xinvest · SweepIQ · Compountr (dividend DRIP) · Arkonomy · MarketPlays · NeuralVest ×2 · Stockey (paper-trading learn) · Sigma Trade (Spanish) · Aimx (trader qualification) · Zoya (halal) · Muslim Xchange (halal) · Ancile Trading (loss insurance) · BLSH · Ainvest → **✗**

### Dev tools / infra / data / tax
Alpaca CLI · ENTON (AI API orchestration) · Finatic (broker aggregation) · TradePulse (order-flow data) · TraderFyles (tax Sch-D/8949) · Appius (OAuth trading app) → **✗** (infra/adjacent)

## The "good for us" shortlist (3)
1. **Lama Trader** — a ready-made *modern Alpaca workstation* (charts + watchlists + portfolio + AI research + order entry + custom workspace). This is the single closest thing to kumo-cockpit's **manual UI**. If the goal were "a nice manual terminal on Alpaca," this basically exists.
2. **Medved Trader** — pro manual terminal (DOM/L2/scanners/hotkeys) for a heavier manual-trading style.
3. **TradingView (+ TradersPost/SignalStack)** — if you want best-in-class charting + alerts and are OK routing execution to Alpaca via a connector.

Plus, engine-side note: **QuantConnect** is the off-the-shelf peer of Nautilus and you *already* use LEAN — relevant only if you ever reconsider the engine choice (you've chosen Nautilus).

## Verdict
- **The marketplace is ~80% no-code bot builders + AI signal services** — automation products, not a human-in-the-loop cockpit. Not our need.
- **A handful of real workstations (Lama Trader, Medved, TradingView) could replace the *manual-UI* part** — saving frontend build — **but only if you give up** the bespoke config-driven tiles + your custom analysis (Ichimoku / Blue-Flag / fintrack) + the Nautilus automation lanes + cross-lane risk. None of these apps do *your* strategy logic or host *your* engine.
- **So the decision hinges on where kumo-cockpit's value lives:**
  - If it's mainly *"a clean manual terminal on Alpaca"* → **Lama Trader / TradingView already exist**; building your own is hard to justify.
  - If it's the **differentiated analysis + Nautilus automation lanes + config layouts** → **no off-the-shelf app covers it; keep building** (and Alpaca is just the data/broker layer beneath).
- Middle path worth considering: use **TradingView** for charting/idea-gen (you likely already do) and keep kumo-cockpit focused on what's unique — the Nautilus lanes + custom risk/analysis — rather than re-building generic charting.

## Next step
Pick the framing: (a) kumo-cockpit = generic manual terminal → evaluate **Lama Trader** hands-on before building more UI; (b) kumo-cockpit = custom engine+analysis cockpit → keep building, Alpaca underneath (see `plan-alpaca-stack-migration.md`), and don't rebuild generic charting that TradingView already nails.
