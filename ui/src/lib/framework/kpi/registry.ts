/**
 * KPI registry — named display-stat definitions a tile binds to by id, mirroring `datasource/registry.ts`
 * and `checklist/registry.ts` exactly (`Map<name, T>`, populated by `@/config/*` registration calls, core
 * stays KPI-agnostic). #182 follow-up (watchlist KPI configuration).
 */
import type { KpiDef } from "./types";

const registry = new Map<string, KpiDef>();

export function registerKpi(def: KpiDef): void {
  registry.set(def.id, def);
}

export function getKpi(id: string): KpiDef | undefined {
  return registry.get(id);
}

export function allKpis(): KpiDef[] {
  return [...registry.values()];
}
