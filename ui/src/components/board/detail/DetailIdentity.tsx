/**
 * The identity block every detail header opens with: SYMBOL on its own line, the company name and
 * venue on a second line beneath it.
 *
 * All four detail surfaces used to put symbol, badges, name and venue on ONE baseline row. On a phone
 * that row cannot fit, and because the badges sit in a separate flex group from the identity text the
 * two overlap rather than wrap — the venue rendered UNDERNEATH the status pill, which reads as a
 * rendering fault, not a narrow screen.
 *
 * Stacking is also the honest hierarchy: the symbol is what you are looking at; the name and venue are
 * what disambiguate it. They are not peers of the ticker and should not sit on its baseline.
 */
export function DetailIdentity({
  symbol,
  name,
  venue,
  children,
}: {
  symbol: string;
  /** Company name. Absent on surfaces that only know an instrument id. */
  name?: string | null;
  /** MIC (XNYS, XNAS…). Derive with `venueOf(instrument_id)` when not otherwise held. */
  venue?: string | null;
  /** Badges that belong ON the symbol line — side, state, UNCLAIMED. */
  children?: React.ReactNode;
}) {
  const sub = [name, venue].filter(Boolean).join(" · ");
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <div className="flex min-w-0 flex-wrap items-baseline gap-2">
        <span className="font-mono text-base font-bold text-t1">{symbol}</span>
        {children}
      </div>
      {sub && <span className="truncate font-mono text-[10px] text-t3">{sub}</span>}
    </div>
  );
}

/** `MPLX.XNYS` → `XNYS`. Empty when the id carries no venue suffix. */
export function venueOf(instrumentId: string): string {
  return instrumentId.split(".")[1] ?? "";
}
