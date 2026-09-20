# adapters/

Custom NautilusTrader adapters living out-of-tree (so the engine is never forked). Implements Nautilus's
adapter interface — data/execution clients + instrument providers — registered with the node at runtime.

Planned: IBKR OAuth Web API adapter (gateway-free `api.ibkr.com`), built only if first-party OAuth self-service
works on the Pro account (gate = predecessor issue 787). Until then the built-in Nautilus IBKR adapter (IB Gateway,
ibapi) is used directly — no code here. Does NOT hold: strategy logic (→ `strategies/`).
