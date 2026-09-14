// Which STACK is this? (#610). The PAPER/LIVE badge says which kind of account an order hits, not
// which tenant — test-alpaca and staging-ibkr rendered identically, and a staging screenshot was
// debugged as paper for several minutes on 2026-08-27. The instance name is the discriminator; it
// comes from the instances repo (instance.env INSTANCE=) baked at build like every NEXT_PUBLIC_*.
export function instanceBadgeLabel(raw: string | undefined): string {
  // `??` would keep "" — Docker bakes an omitted ARG to the empty string (same trap as
  // config.ts API_BASE). Unset must be LOUD: a blank badge reads as "fix not deployed".
  // `??` would keep "" — Docker bakes an omitted ARG to the empty string (same trap as
  // config.ts API_BASE). Unset must be LOUD: a blank badge reads as "fix not deployed".
  return raw && raw.length > 0 ? raw : "UNNAMED";
}
