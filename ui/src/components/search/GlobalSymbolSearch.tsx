"use client";

/**
 * GlobalSymbolSearch (#25) — the cockpit's header search. Lives in the top bar (where the dev strapline
 * used to be), NOT the tile grid, so it never steals space from a view's tiles. Type → ranked matches in
 * a dropdown overlay → keyboard/click select → sets `focusedSymbol` in the store (a future chart/detail
 * tile reacts to it). No layout mutation.
 *
 * Design (Perplexity + Codex reviewed): 250ms debounce, min length 1, AbortController-cancelled requests,
 * `keepPreviousData`, IME-composition guard, distinct "search unavailable" (backend 503) state.
 */
import { useEffect, useRef, useState } from "react";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Eye, Plus } from "lucide-react";
import { addToWatchlist, searchInstruments, SearchUnavailableError } from "@/lib/api/client";
import type { InstrumentMatch } from "@/lib/api/types";
import { useCockpitStore } from "@/lib/framework/store";
import { searchPresentation, SLOW_MS } from "./searchPresentation";

const DEBOUNCE_MS = 250;

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

export function GlobalSymbolSearch() {
  const [input, setInput] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const composing = useRef(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const debounced = useDebounced(input, DEBOUNCE_MS);
  const q = debounced.trim();

  const focusedInstrument = useCockpitStore((s) => s.focusedInstrument);
  const openDetailForSymbol = useCockpitStore((s) => s.openDetailForSymbol);
  const queryClient = useQueryClient();
  const [added, setAdded] = useState<Set<string>>(new Set());
  const addWatch = useMutation({
    mutationFn: addToWatchlist,
    onSuccess: (res, instrumentId) => {
      queryClient.setQueryData(["watchlist"], res);
      setAdded((prev) => new Set(prev).add(instrumentId));
    },
  });

  const query = useQuery({
    queryKey: ["instrument-search", q],
    enabled: q.length >= 1,
    // THE TERM TRAVELS WITH THE ROWS (#391). `keepPreviousData` is deliberate — it stops the dropdown
    // flickering empty between keystrokes — but it means a query that never resolves leaves the
    // PREVIOUS term's rows on screen, presented exactly as if they answered what was typed. Recording
    // the term inside the cached value is what makes that detectable at all: the rendered rows always
    // know which search they answer, and comparing that against `q` is a fact rather than an inference.
    queryFn: async ({ signal }) => ({ term: q, ...(await searchInstruments(q, 20, signal)) }),
    placeholderData: keepPreviousData,
    staleTime: 5_000,
    retry: false,
  });

  // How long the current fetch has been in flight — for the "still waiting" state only. Ticks while
  // fetching and resets when it stops, so it never reports a stale duration.
  const [pendingMs, setPendingMs] = useState(0);
  useEffect(() => {
    if (!query.isFetching) {
      setPendingMs(0);
      return;
    }
    const started = Date.now();
    const id = setInterval(() => setPendingMs(Date.now() - started), 250);
    return () => clearInterval(id);
  }, [query.isFetching]);

  const results: InstrumentMatch[] = query.data?.results ?? [];
  const presentation = searchPresentation({
    term: q,
    dataTerm: query.data?.term ?? null,
    isFetching: query.isFetching,
    pendingMs,
    hasResults: results.length > 0,
  });
  const unavailable = query.error instanceof SearchUnavailableError;
  const showDropdown = open && q.length >= 1;

  useEffect(() => setActive(0), [q]);

  // Close the dropdown on an outside click.
  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);

  function select(match: InstrumentMatch | undefined) {
    if (!match) return;
    openDetailForSymbol({ ...match, context: { tab: "search" } }); // focus + open the detail surface (#27/#69)
    setInput("");
    setOpen(false);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (composing.current) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const m = results[active];
      if (m) addWatch.mutate(m.instrument_id); // Enter adds to the watchlist (the primary action)
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div ref={rootRef} className="relative min-w-0 flex-1 sm:flex-none">
      <div className="flex min-w-0 items-center gap-2">
        <input
          value={input}
          onChange={(e) => {
            setInput(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          onCompositionStart={() => (composing.current = true)}
          onCompositionEnd={() => (composing.current = false)}
          placeholder="Search ticker or company…"
          autoComplete="off"
          spellCheck={false}
          // Mobile font ≥16px stops iOS auto-zoom-on-focus even if the viewport meta is ignored; desktop keeps 12px.
          // Fluid below `sm` so a narrow header shrinks the SEARCH rather than colliding with the env
          // badge; fixed widths (and the focus-expand) return once there is room for them.
          className="w-full min-w-0 rounded-md border border-ds-line bg-ds-surf/60 px-2.5 py-1 font-mono text-[16px] text-t1 placeholder:text-t3 focus:border-ds-line2 focus:outline-none sm:w-56 sm:text-[12px] sm:focus:w-72"
        />
        {focusedInstrument && (
          <span className="whitespace-nowrap font-mono text-[11px] text-status-bull" title="Focused symbol">
            ◎ {focusedInstrument.symbol}
          </span>
        )}
      </div>

      {showDropdown && (
        // Mobile: fixed, full viewport width below the header (so nothing clips off the edge). Desktop:
        // anchored under the input.
        <div className="fixed inset-x-2 top-[3.25rem] z-50 overflow-hidden rounded-lg border border-ds-line bg-ds-bg/95 shadow-xl backdrop-blur sm:absolute sm:inset-x-auto sm:left-0 sm:top-full sm:mt-1 sm:w-[26rem]">
          {unavailable ? (
            <div className="px-3 py-3 text-center font-mono text-[11px] text-status-watch/80">
              Search unavailable — no instrument provider
            </div>
          ) : query.isError ? (
            <div className="px-3 py-3 text-center font-mono text-[11px] text-status-bear/80">Search failed</div>
          ) : presentation.kind === "empty" ? (
            <div className="px-3 py-3 text-center font-mono text-[11px] text-t3">No matches for “{q}”</div>
          ) : presentation.kind === "slow" ? (
            <div className="px-3 py-3 text-center font-mono text-[11px] text-status-watch/80">
              Still searching “{q}”…
            </div>
          ) : (
            <>
            {/* THE ROWS DO NOT ANSWER WHAT IS TYPED, AND MUST SAY SO (#391). Operator: "the search result
                is from last search." The root cause is not reproducible on desktop and #391 stays open
                for it — but presenting an earlier term's matches as the answer to this one is the part
                that is fixable now, and it is the part that misleads. */}
            {presentation.kind === "stale" && (
              <div
                className="border-b border-ds-line bg-status-watch/10 px-3 py-1.5 font-mono text-[10px] text-status-watch"
                title={`These rows answer “${presentation.showing}”. The search for “${q}” has not come back.`}
              >
                showing results for “{presentation.showing}” — still fetching “{q}”
              </div>
            )}
            <ul className="max-h-[60vh] divide-y divide-ds-line/70 overflow-y-auto">
              {results.map((m, i) => {
                const isAdded = added.has(m.instrument_id);
                return (
                  <li
                    key={m.instrument_id}
                    onMouseEnter={() => setActive(i)}
                    className={`flex items-center gap-1 pr-1.5 transition-colors ${
                      i === active ? "bg-ds-surf2/70" : "hover:bg-ds-surf2/40"
                    }`}
                  >
                    {/* Primary tap = add to watchlist (the whole row). */}
                    <button
                      type="button"
                      onClick={() => addWatch.mutate(m.instrument_id)}
                      aria-label={`Add ${m.symbol} to watchlist`}
                      className="flex min-w-0 flex-1 items-center gap-2.5 px-3 py-2.5 text-left"
                    >
                      <span className="min-w-0 flex-1">
                        <span className="font-mono text-[13px] font-bold text-t1">{m.symbol}</span>
                        <span className="ml-2 font-mono text-[10px] text-t3">{m.name}</span>
                      </span>
                      <span className="whitespace-nowrap font-mono text-[9px] text-t3">{m.venue}</span>
                      <span
                        className={`flex shrink-0 items-center gap-1 rounded-full px-2 py-1 font-mono text-[10px] font-bold ${
                          isAdded
                            ? "bg-status-bull/15 text-status-bull"
                            : "bg-ds-surf2 text-status-bull"
                        }`}
                      >
                        {isAdded ? <Check size={12} /> : <Plus size={12} />}
                        {isAdded ? "Added" : "Watch"}
                      </span>
                    </button>
                    {/* Secondary: preview (detail surface). */}
                    <button
                      type="button"
                      onClick={() => select(m)}
                      aria-label={`View ${m.symbol}`}
                      title="View"
                      className="shrink-0 rounded p-1.5 text-t3 hover:bg-ds-surf2 hover:text-t1"
                    >
                      <Eye size={15} />
                    </button>
                  </li>
                );
              })}
            </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}
