"use client";

/**
 * SettingsTile (#49) — the settings surface. Lists the registered domains (from the backend, one JSON
 * Schema each) and renders a schema-generated form per domain. No per-setting UI code; add a schema on the
 * backend and it appears here automatically.
 */
import { useQuery } from "@tanstack/react-query";
import { getSettings, listSettings, putSettings } from "@/lib/api/client";
import { SchemaForm } from "@/components/settings/SchemaForm";
import { AppearanceSetting } from "@/components/settings/AppearanceSetting";
import { TileState } from "@/components/board/TileState";
import type { TileProps } from "@/lib/framework/types";
import type { SettingsConfig } from "./definition";

function DomainForm({ domain }: { domain: string }) {
  const { data, status } = useQuery({
    queryKey: ["settings", domain],
    queryFn: () => getSettings(domain),
    staleTime: 30_000,
    refetchOnWindowFocus: false, // don't surprise-refetch mid-edit
  });
  return (
    <TileState status={status === "pending" ? "loading" : status === "error" ? "error" : "live"} isEmpty={false}>
      {data && <SchemaForm domain={domain} schema={data.schema} values={data.values} onSave={putSettings} />}
    </TileState>
  );
}

export function SettingsTile(_props: TileProps<SettingsConfig>) {
  const { data, status } = useQuery({
    queryKey: ["settings"],
    queryFn: listSettings,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
  const domains = data?.domains ?? [];

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-t2">Settings</h2>
        <span className="font-mono text-xs text-t3">{domains.length} domains</span>
      </div>

      {/* Client-side appearance (theme) — local, not a backend domain. */}
      <div className="mb-3">
        <AppearanceSetting />
      </div>

      <TileState
        status={status === "pending" ? "loading" : status === "error" ? "error" : "live"}
        isEmpty={domains.length === 0}
        emptyLabel="No settings domains"
      >
        <div className="flex flex-col gap-3">
          {domains.map((d) => (
            <DomainForm key={d} domain={d} />
          ))}
        </div>
      </TileState>

      {/* Build stamp — the git SHA baked into this bundle (NEXT_PUBLIC_BUILD_ID). Lets you confirm at a
          glance whether the browser is on the latest deploy vs a stale cache. */}
      <div className="mt-4 text-center font-mono text-[10px] text-t3">
        build {process.env.NEXT_PUBLIC_BUILD_ID || "dev"}
      </div>
    </div>
  );
}
