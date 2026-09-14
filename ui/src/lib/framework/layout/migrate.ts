/**
 * Versioned layout migration. A persisted layout carries `schemaVersion`; on load we run each
 * step v(n)→v(n+1) until it reaches the latest, then validate. Unrecoverable input returns null —
 * the caller drops the view rather than crashing the board (graceful degradation, #7 acceptance).
 *
 * Add a migration when the schema changes: e.g. `1: (raw) => ({ ...raw, schemaVersion: 2, ... })`.
 */
import { layoutSchema, LAYOUT_SCHEMA_VERSION, type Layout } from "./schema";

type MigrationStep = (raw: Record<string, unknown>) => Record<string, unknown>;

const migrations: Record<number, MigrationStep> = {
  // 0: (raw) => ({ ...raw, schemaVersion: 1 }),  // example: stamp version onto a pre-versioned blob
};

export function migrateLayout(raw: unknown): Layout | null {
  if (raw == null || typeof raw !== "object") return null;
  let cur = raw as Record<string, unknown>;
  let version = typeof cur.schemaVersion === "number" ? cur.schemaVersion : 0;

  while (version < LAYOUT_SCHEMA_VERSION) {
    const step = migrations[version];
    if (!step) return null; // no path forward → unrecoverable
    cur = step(cur);
    const next = typeof cur.schemaVersion === "number" ? cur.schemaVersion : version;
    if (next <= version) return null; // migration didn't advance → bail (no infinite loop)
    version = next;
  }

  const parsed = layoutSchema.safeParse(cur);
  return parsed.success ? parsed.data : null;
}
