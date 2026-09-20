"""Verify a session narrative against the journal it claims to describe (#199 / research).

The feature this guards does not exist yet, and that is deliberate: the checker is the eval harness,
so it comes first. A narrative that fails these checks is not sent — which turns "trust the model"
into "verify, then send".

THE MODEL AUTHORS ALMOST NOTHING. The first version let it write exit reasons and numbers, then tried
to verify the prose after the fact. An adversarial review took that apart: an empty reason passed
because `"" in source` is true, "did not leave the ranking" passed on word overlap while reversing
the meaning, and "bought CGAU at 109.81" passed because 109.81 was in the bundle — as VCTR's price.
Verifying free text against a corpus is a losing game.

So the contract inverted. The model SELECTS; it does not compose:

  * `exits` / `entries_blocked` are symbol lists. Their reasons are rendered from the journal, which
    already writes them in plain English ("gave back all of a 0.7% peak and is 1.5% below entry").
  * `needs_attention` holds journal rows COPIED VERBATIM. Anything else is rejected.
  * `headline` is the one free sentence, and it may contain no digits at all — every count is
    rendered by us.

What is left for the model is judgement about salience: which facts matter this morning. That is the
part a human actually wants help with, and it cannot fabricate a number while doing it.

ORDERS ARE TRUTH. `decision.summary` states INTENT ("enter 8"); `orders` states what was submitted.
On 2026-08-10 those differed — CHEF and BRK.B were ranked and then cap-blocked, so six buys went out
against an intent of eight. The daily digest already made this mistake in the other direction,
reporting "entered 6" for a day whose true answer was 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Style, not safety. Word-boundary matched — a substring test rejects "insignificant" for containing
#: "significant", and a ticker like WILL for containing "will".
BANNED = frozenset({
    "risk-on", "risk-off", "headwinds", "tailwinds", "catalysts", "regime", "robust",
    "significant", "conviction", "bullish", "bearish",
})

#: Words asserting something no journal can support — it holds no price data and no news.
FORWARD_LOOKING = frozenset({"should", "expect", "expected", "likely", "will", "poised"})

#: Risk lines describing a DEGRADED SYSTEM rather than normal policy. A position cap doing its job is
#: not an incident; missing bars and unsubscribed symbols are. Used to decide whether an all-clear is
#: honest, so erring toward "this is actionable" is the safe direction.
_DEGRADED = ("missing", "not subscribed", "stale", "coverage", "unclaimed", "not this strategy's",
             "refusing", "halted", "breach")

_ORDER = re.compile(r"^(BUY|SELL)\s+[\d,.]+\s+([A-Z][A-Z0-9.]*)\b", re.IGNORECASE)
_DIGIT = re.compile(r"\d")
_TICKERISH = re.compile(r"\b[A-Z][A-Z0-9]{1,5}(?:\.[A-Z])?\b")

#: Uppercase words that are not tickers. Without this every headline naming the strategy or an order
#: side trips the invented-symbol rule.
_NOT_TICKERS = frozenset({
    "BUY", "SELL", "HOLD", "ET", "UTC", "US", "USD", "P&L", "PNL", "AM", "PM", "OK", "NO",
    "MOMENTUM", "MANUAL", "EXTERNAL", "TRADING", "PAUSED", "HALTED", "LIQUIDATING", "I",
})


@dataclass(frozen=True)
class Violation:
    """One reason this narrative must not be sent. `rule` is stable enough to assert on in tests."""

    rule: str
    detail: str

    def __str__(self) -> str:                                       # pragma: no cover - display only
        return f"{self.rule}: {self.detail}"


@dataclass(frozen=True)
class Narrative:
    """What the model returns. Note what is NOT here: prices, counts, and written reasons."""

    headline: str = ""
    exits: list[str] = field(default_factory=list)                  # symbols; reasons come from us
    entries_submitted: list[str] = field(default_factory=list)
    entries_blocked: list[str] = field(default_factory=list)
    needs_attention: list[str] = field(default_factory=list)        # verbatim journal rows
    nothing_needed: bool = False


def submitted(bundle: dict, side: str) -> list[str]:
    """Symbols the journal says were actually submitted on `side` — the source of truth here.

    Reads `order` rows, not the decision, because a decision can plan an order that the position cap,
    the risk engine, or a rejection then prevents. Duplicates are preserved: two sells of one symbol
    are two orders, and collapsing them would hide a partial-exit-then-exit.
    """
    out = []
    for line in bundle.get("orders") or []:
        m = _ORDER.match(str(line).strip())
        if m and m.group(1).upper() == side:
            out.append(m.group(2).upper())
    return out


def actionable(bundle: dict) -> list[str]:
    """Journal rows a human should see: every error, plus risk rows describing a degraded system."""
    rows = [str(e) for e in (bundle.get("errors") or [])]
    rows += [str(r) for r in (bundle.get("risk") or [])
             if any(k in str(r).lower() for k in _DEGRADED)]
    return rows


def render_reason(bundle: dict, symbol: str) -> str:
    """The journal's own words for why `symbol` exited. The model never writes this."""
    return str(((bundle.get("decision") or {}).get("reasons") or {}).get(symbol, "")).strip()


def render_block(bundle: dict, symbol: str) -> str:
    """The risk row that blocked `symbol`, verbatim, or empty if none did."""
    for row in bundle.get("risk") or []:
        text = str(row)
        if re.search(rf"\b{re.escape(symbol)}\b", text) and ("skip" in text.lower()
                                                             or "cap" in text.lower()):
            return text
    return ""


def check(n: Narrative, bundle: dict) -> list[Violation]:
    """Every way this narrative could mislead, in the order the operator would care about."""
    v: list[Violation] = []
    buys, sells = submitted(bundle, "BUY"), submitted(bundle, "SELL")
    known = _known_symbols(bundle) | set(buys) | set(sells)

    # 1. ORDERS ARE TRUTH — the rule this module exists for. Multiset comparison, so a duplicated
    #    symbol in the narrative cannot pass against a single order.
    if sorted(s.upper() for s in n.entries_submitted) != sorted(buys):
        v.append(Violation("entries_mismatch",
                           f"claims {sorted(n.entries_submitted)}, journal submitted {sorted(buys)}"))
    if sorted(s.upper() for s in n.exits) != sorted(sells):
        v.append(Violation("exits_mismatch",
                           f"claims {sorted(n.exits)}, journal submitted {sorted(sells)}"))

    # 2. Every exit must have journal wording to render. A symbol we sold with no recorded reason is
    #    a gap in the journal, and the narrative must not paper over it with a sentence of its own.
    for sym in n.exits:
        if not render_reason(bundle, sym.upper()):
            v.append(Violation("reason_missing", f"{sym}: the journal records no reason for exiting"))

    # 3. A blocked entry must be one the journal actually blocked — and must not also be submitted.
    for sym in n.entries_blocked:
        s = sym.upper()
        if s in buys:
            v.append(Violation("blocked_but_submitted", f"{sym} was submitted, not blocked"))
        elif not render_block(bundle, s):
            v.append(Violation("block_not_grounded", f"no risk row shows {sym} being skipped"))

    # 4. No invented symbols, including in the free sentence.
    for sym in {*(s.upper() for s in n.entries_submitted), *(s.upper() for s in n.exits),
                *(s.upper() for s in n.entries_blocked), *_tickers_in(n.headline)}:
        if sym not in known:
            v.append(Violation("ungrounded_symbol", f"{sym} appears nowhere in the journal"))

    # 5. The model states no numbers at all. Counts and prices are rendered from the bundle, so a
    #    digit in the one free field can only be an assertion we cannot check in context — the review
    #    that prompted this found "bought CGAU at 109.81" passing on VCTR's price.
    if _DIGIT.search(_strip_identifiers(n.headline)):
        v.append(Violation("number_in_free_text",
                           "the headline states a number; counts and prices are rendered, not written"))

    # 6. Attention items are journal rows, copied. Not summaries, not paraphrases.
    rows = set(actionable(bundle)) | {str(r) for r in (bundle.get("risk") or [])}
    for item in n.needs_attention:
        if item.strip() not in rows:
            v.append(Violation("attention_not_verbatim", f"{item!r} is not a journal row"))

    # 7. The most dangerous claim in the whole feature. An all-clear must answer to the journal, not
    #    to what the model chose to report — the earlier version let a model stay silent about a risk
    #    row and then declare all-clear, which is precisely how a missed incident happens.
    missed = [r for r in actionable(bundle) if r not in n.needs_attention]
    if n.nothing_needed and missed:
        v.append(Violation("false_all_clear",
                           f"said nothing is needed while {len(missed)} row(s) need attention: "
                           f"{missed[0][:80]}"))
    if n.nothing_needed and n.needs_attention:
        v.append(Violation("contradiction", "flagged items and also said nothing is needed"))

    # 8. Style and tense, word-boundary matched.
    low = n.headline.lower()
    for word in sorted(BANNED):
        if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", low):
            v.append(Violation("banned_word", word))
    for word in sorted(FORWARD_LOOKING):
        if re.search(rf"\b{re.escape(word)}\b", low):
            v.append(Violation("forward_looking", f"{word!r} — the journal cannot support a forecast"))
    return v


def render(n: Narrative, bundle: dict) -> str:
    """Turn a CHECKED narrative into text. Every fact here comes from the bundle, not the model.

    Callers must run `check` first and refuse to send on any violation; this function trusts its
    input by design.
    """
    lines = [n.headline.strip()]
    if n.exits:
        lines += ["", "Exits"]
        lines += [f"  {s.upper()} — {render_reason(bundle, s.upper())}" for s in n.exits]
    if n.entries_submitted:
        lines += ["", "Entries",
                  f"  {len(n.entries_submitted)} submitted: "
                  f"{', '.join(s.upper() for s in n.entries_submitted)}."]
    if n.entries_blocked:
        lines += [f"  {len(n.entries_blocked)} ranked and not bought: "
                  f"{', '.join(s.upper() for s in n.entries_blocked)}."]
        for s in n.entries_blocked:
            lines.append(f"    {render_block(bundle, s.upper())}")
    if n.needs_attention:
        lines += ["", "Needs you"] + [f"  {item}" for item in n.needs_attention]
    elif n.nothing_needed:
        lines += ["", "Nothing needs you."]
    return "\n".join(lines)


def _known_symbols(bundle: dict) -> set[str]:
    out = {str(s).upper() for s in ((bundle.get("decision") or {}).get("reasons") or {})}
    for row in bundle.get("trail") or []:
        if isinstance(row, dict) and row.get("sym"):
            out.add(str(row["sym"]).upper())
    for row in bundle.get("risk") or []:
        out |= {m for m in _TICKERISH.findall(str(row))} - _NOT_TICKERS
    return out


def _strip_identifiers(text: str) -> str:
    """Drop word-tokens that START with a letter, so a digit inside a name is not read as a number.

    `MOMENTUM-002` is an identifier, not a claim about quantity, and rejecting a headline for naming
    its own strategy is exactly the false positive that makes a verifier get switched off. A bare
    `8` or `109.81` still survives this and is caught.
    """
    return re.sub(r"\b[A-Za-z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)*", " ", text or "")


def _tickers_in(text: str) -> set[str]:
    return {m for m in _TICKERISH.findall(text or "") if m not in _NOT_TICKERS}
