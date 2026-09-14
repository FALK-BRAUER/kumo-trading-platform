/**
 * Humanize an order deny/reject `reason` for display (#order-error-ux).
 *
 * The backend forwards a broker rejection verbatim — the Alpaca HTTP layer raises
 * `"Alpaca POST /v2/orders → 422: {\"code\":...,\"message\":...}"` and the exec client sets that whole
 * string as the Nautilus OrderRejected reason. Printed raw, that dumps JSON into the blotter. This pulls the
 * human `message` (and the market price, when Alpaca includes it) out of that envelope so the row reads like
 * "Stop price must be greater than current price (mkt $24.11)". Anything it can't parse falls through
 * unchanged, so engine-side deny reasons (already human) are untouched.
 *
 * NOTE: the proper fix is server-side (parse at the exec client so the reason is clean at the source), but
 * that is engine code; this keeps the UI readable without an engine change.
 */
function capitalize(s: string): string {
  return s.length ? s[0].toUpperCase() + s.slice(1) : s;
}

export function humanizeOrderReason(raw: string | null | undefined): string | null {
  if (!raw) return null;
  // Match a trailing "→ <status>: { …json… }" envelope (Alpaca error body forwarded by the backend).
  const m = raw.match(/→\s*\d{3}:\s*(\{[\s\S]*\})\s*$/);
  if (m) {
    try {
      const body = JSON.parse(m[1]) as { message?: unknown; market_price?: unknown };
      if (typeof body.message === "string" && body.message.trim()) {
        const px =
          typeof body.market_price === "string" || typeof body.market_price === "number"
            ? ` (mkt $${body.market_price})`
            : "";
        return capitalize(body.message.trim()) + px;
      }
    } catch {
      // Not JSON after all — fall through to the raw string.
    }
  }
  return raw;
}
