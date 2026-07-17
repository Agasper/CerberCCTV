"""Async-окружение Alembic. URL берётся из DATABASE_URL (как и у приложения)."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from cerber_admin.db import prepare_database_url
from cerber_admin.env import env as app_env
from cerber_admin.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url, _ = prepare_database_url(app_env.database_url)
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url, connect_args = prepare_database_url(app_env.database_url)
    engine = create_async_engine(url, connect_args=connect_args)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
