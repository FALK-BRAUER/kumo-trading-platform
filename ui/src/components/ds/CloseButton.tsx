/**
 * The one way to dismiss a panel.
 *
 * There were five dismiss buttons across the cockpit and no two matched: lucide `X` at 15, 16 and 18px,
 * plus the Unicode `✕` character twice — a different glyph shape and weight entirely, at whatever size
 * the surrounding font happened to be. Hover was `rounded + bg` on some and colour-only on others. They
 * all do the same thing, so they should all look like the same thing.
 *
 * `sm` is for a nested/secondary header (an order ticket inside a detail panel); `md` for a top-level
 * panel header. Anything destructive — removing a watchlist row, cancelling an order — is NOT this
 * component: it dismisses a surface, it never deletes data.
 */
import { X } from "lucide-react";

export function CloseButton({
  onClick,
  label = "Close",
  size = "md",
}: {
  onClick: () => void;
  /** Say WHAT closes when there is more than one dismissible thing on screen ("Close order ticket"). */
  label?: string;
  size?: "sm" | "md";
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      className="rounded p-1 text-t3 transition-colors hover:bg-ds-surf2 hover:text-t1"
    >
      <X size={size === "sm" ? 16 : 18} />
    </button>
  );
}
