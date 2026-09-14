"use client";

import { useState } from "react";
import { StatusBadge } from "@/components/ds/StatusBadge";
import { PositionDetailView } from "@/components/board/detail/PositionDetail";
import { THEME_TOKENS } from "@/lib/design/tokens";
import type { PositionDTO } from "@/lib/api/types";

const MOCK_LONG: PositionDTO = {
  instrument_id: "MPC.XNYS",
  side: "LONG",
  quantity: 15,
  avg_px_open: 285.1,
  realized_pnl: "0.00 USD",
  strategy_id: "MOMENTUM-001",
  ts_last: 0,
};
const MOCK_SHORT: PositionDTO = {
  instrument_id: "JNJ.XNYS",
  side: "SHORT",
  quantity: 40,
  avg_px_open: 257.0,
  realized_pnl: "12.40 USD",
  strategy_id: "MANUAL-001",
  ts_last: 0,
};

/**
 * /dev/ui — the design-system style-guide route (#114). Renders every ds primitive + state so the
 * system can be reviewed in isolation (the in-app counterpart to docs/design-system/mock-v2.html).
 * Grows as primitives land. Dev-only surface; not linked from the board.
 */

const ORDER_STATES = [
  "DENIED",
  "REJECTED",
  "WORKING",
  "ACCEPTED",
  "PARTIALLY_FILLED",
  "FILLED",
  "CANCELED",
  "EXPIRED",
];

function Swatch({ name, value }: { name: string; value: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="h-6 w-6 rounded ring-1 ring-ds-line2" style={{ background: value }} />
      <span className="font-mono text-[10px] text-t2">
        {name} <span className="text-t3">{value}</span>
      </span>
    </div>
  );
}

export default function DevUiPage() {
  const [day, setDay] = useState(false);
  return (
    <div className={day ? "day" : ""}>
      <main className="min-h-screen bg-ds-bg p-6 text-t1">
        <div className="mx-auto flex max-w-3xl flex-col gap-8">
          <header className="flex items-center justify-between">
            <h1 className="font-mono text-lg font-bold">design system · /dev/ui</h1>
            <button
              onClick={() => setDay((d) => !d)}
              className="rounded-md bg-ds-surf2 px-3 py-1.5 font-mono text-xs text-t2 ring-1 ring-ds-line2"
            >
              {day ? "☀ day" : "☾ night"}
            </button>
          </header>

          <section className="flex flex-col gap-3">
            <h2 className="font-mono text-[11px] uppercase tracking-widest text-t3">StatusBadge</h2>
            <div className="flex flex-wrap gap-2 rounded-lg bg-ds-surf p-4 ring-1 ring-ds-line">
              {ORDER_STATES.map((s) => (
                <StatusBadge key={s} status={s} />
              ))}
            </div>
            <p className="font-mono text-[10px] text-t3">
              Terminal error (DENIED/REJECTED) is prominent; benign states stay quiet.
            </p>
          </section>

          <section className="flex flex-col gap-3">
            <h2 className="font-mono text-[11px] uppercase tracking-widest text-t3">
              PositionDetailView (long / short)
            </h2>
            <div className="grid gap-3 md:grid-cols-2">
              <div className="h-[420px]">
                <PositionDetailView position={MOCK_LONG} price={283.69} />
              </div>
              <div className="h-[420px]">
                <PositionDetailView position={MOCK_SHORT} price={257.02} />
              </div>
            </div>
          </section>

          <section className="flex flex-col gap-3">
            <h2 className="font-mono text-[11px] uppercase tracking-widest text-t3">Tokens</h2>
            <div className="grid grid-cols-2 gap-2 rounded-lg bg-ds-surf p-4 ring-1 ring-ds-line sm:grid-cols-3">
              {Object.entries(THEME_TOKENS[day ? "day" : "night"]).map(([k, v]) => (
                <Swatch key={k} name={k} value={v} />
              ))}
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}
