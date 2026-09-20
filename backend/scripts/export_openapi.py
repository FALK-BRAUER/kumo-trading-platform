"""Dump the FastAPI OpenAPI schema to JSON — the source of truth for the generated TS client.

Run from backend/ (venv active):  python -m scripts.export_openapi [out_path]
Default out: ../ui/openapi.json  (consumed by the UI's `npm run gen:api`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from api.app import app

DEFAULT_OUT = Path(__file__).resolve().parents[2] / "ui" / "openapi.json"


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(app.openapi(), indent=2) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
