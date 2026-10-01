"""CLI: python -m app.seed --sample-cafe [--reset] [--country EG] [--start 2026-10-04]"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date

from app.harness.audit import jsonable


async def _main(args: argparse.Namespace) -> None:
    from app import wiring
    from app.db.engine import init_engine
    from app.db.migrate import upgrade_head
    from app.graphs import runtime
    from app.seed.sample_cafe import reset_and_seed, seed

    init_engine()
    await upgrade_head()
    await runtime.init()
    wiring.register_all()
    start = date.fromisoformat(args.start) if args.start else None
    info = await (reset_and_seed(args.country, start) if args.reset else seed(args.country, start))
    print(json.dumps(jsonable(info), indent=2, ensure_ascii=False))
    await runtime.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Seed the sample cafe")
    p.add_argument("--sample-cafe", action="store_true", required=True)
    p.add_argument("--reset", action="store_true", help="delete all data first")
    p.add_argument("--country", default=None, help="country profile code (default: DEFAULT_COUNTRY, Egypt)")
    p.add_argument("--start", default=None, help="first live business date (default: today)")
    asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    main()
