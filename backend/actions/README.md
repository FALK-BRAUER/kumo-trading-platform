# actions/

The Action Library — composable `trigger → condition → order` rules attachable to a position or lane.
A registry of parameterized actions (Zod/Pydantic schema each); strategies and the UI compose them.

Catalog (evidence-graded, see predecessor-repo `research/order-action-catalog-20260624.md`): time-stop, close-based
MOC, Kijun/cloud/TK exits, trailing variants, gap-up veto, pullback-to-Kijun entry, heat/correlation gates.
Native order types (brackets/OCO/trailing/MOC) prefer broker-held; engine-evaluated rules arm then hand off to
a native order where possible. All default `False`. Does NOT hold: the order transport (→ Nautilus/`adapters/`).
