"""Dump the OpenAPI schema to backend/openapi.json for `npm run gen:api:offline`."""

from __future__ import annotations

import json
from pathlib import Path

from app.main import create_app

if __name__ == "__main__":
    out = Path(__file__).resolve().parents[1] / "openapi.json"
    out.write_text(json.dumps(create_app().openapi(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out}")
