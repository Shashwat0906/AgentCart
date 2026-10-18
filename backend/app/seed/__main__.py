"""Usage: python -m app.seed [--reset]"""

from __future__ import annotations

import argparse

from app.db.session import get_sessionmaker
from app.seed import seed_catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the AgentCart product catalogue")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="restore seed prices, stock and specs for existing products",
    )
    args = parser.parse_args()
    with get_sessionmaker()() as session:
        result = seed_catalog(session, reset_prices_and_stock=args.reset)
    print(f"Seed complete: {result['inserted']} inserted, {result['updated']} reset, {result['total']} products total")


if __name__ == "__main__":
    main()
