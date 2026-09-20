"""Run the API: `python -m api` (from backend/, with the venv active)."""

from __future__ import annotations

import uvicorn


def main() -> None:
    # 0.0.0.0 so the cockpit is reachable from a phone over LAN / tailscale (paper dev, no secrets).
    # An instance may narrow it (`KUMO_BIND_HOST=127.0.0.1`) without a rebuild; unset keeps today's
    # behaviour exactly. The PORT is the in-container one — the host mapping is compose's job (#486).
    import os

    uvicorn.run(
        "api.app:app",
        host=os.environ.get("KUMO_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("KUMO_BIND_PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
