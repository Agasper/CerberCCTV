"""Ожидание готовности Postgres перед миграциями (нужно в dev-компоузе,
где админка стартует одновременно с БД)."""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from cerber_admin.db import engine

ATTEMPTS = 30
DELAY_S = 2


async def main() -> int:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            print("БД доступна")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"Ожидание БД ({attempt}/{ATTEMPTS}): {exc}")
            await asyncio.sleep(DELAY_S)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
