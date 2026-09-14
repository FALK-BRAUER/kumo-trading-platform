/**
 * TileFrame — the card chrome every tile sits in. Title strip + content slot.
 * Matches the old UI's card language (rounded-xl, ring, card bg). `bleed` drops padding for charts.
 */
import type { ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface TileFrameProps {
  title: string;
  subtitle?: string;
  headerRight?: ReactNode;
  bleed?: boolean;
  children: ReactNode;
}

export function TileFrame({ title, subtitle, headerRight, bleed, children }: TileFrameProps) {
  return (
    <Card size="sm" className="min-w-0">
      <CardHeader className="flex flex-row items-center justify-between gap-2 pb-0">
        <div className="flex items-baseline gap-2 min-w-0">
          <CardTitle className="font-mono text-sm truncate">{title}</CardTitle>
          {subtitle && (
            <span className="text-[11px] font-mono text-muted-foreground truncate">{subtitle}</span>
          )}
        </div>
        {headerRight}
      </CardHeader>
      <CardContent className={cn("pt-0", bleed && "px-0")}>{children}</CardContent>
    </Card>
  );
}
