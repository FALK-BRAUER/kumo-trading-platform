# api/

FastAPI routes, WebSocket bridge and the Nautilus node lifecycle.

- **Goes in:** one module per resource; `app.py` builds the app
- **Stays out:** business logic that belongs in a strategy or an adapter
