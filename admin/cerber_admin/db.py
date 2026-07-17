"""Подключение к Postgres.

DATABASE_URL принимается в любом привычном виде (postgres://,
postgresql://, postgresql+asyncpg://). Параметр sslmode из URL asyncpg
не понимает — вырезаем его и превращаем в ssl-контекст:
  - require            — TLS без проверки сертификата (как в libpq)
  - verify-ca/full     — TLS с проверкой
DO Managed Postgres выдаёт URL именно с sslmode=require.

Подключение рассчитано на PgBouncer в transaction-режиме (пулы DO):
именованные prepared statements живут в сессии сервера, а pgbouncer
выдаёт сессию только на время транзакции, поэтому кэши prepared
statements отключены на обоих уровнях (SQLAlchemy и asyncpg), а имена
statement'ов уникальны — это рекомендация SQLAlchemy для PgBouncer.
Работает и при прямом подключении, просто чуть медленнее на разборе
повторяющихся запросов.
"""

from __future__ import annotations

import ssl
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cerber_admin.env import env


def prepare_database_url(raw: str) -> tuple[str, dict]:
    parts = urlsplit(raw)
    scheme = "postgresql+asyncpg"
    query = dict(parse_qsl(parts.query))
    connect_args: dict = {}

    sslmode = query.pop("sslmode", None)
    query.pop("sslrootcert", None)
    if sslmode in ("require", "prefer", "allow"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = ctx
    elif sslmode in ("verify-ca", "verify-full"):
        connect_args["ssl"] = ssl.create_default_context()

    # PgBouncer transaction mode: без кэша prepared statements
    # и с уникальными именами (см. docstring модуля)
    query["prepared_statement_cache_size"] = "0"  # кэш SQLAlchemy-диалекта
    connect_args["statement_cache_size"] = 0      # кэш asyncpg
    connect_args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid.uuid4()}__"

    url = urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return url, connect_args


_url, _connect_args = prepare_database_url(env.database_url) if env.database_url else ("", {})

engine = (
    create_async_engine(_url, connect_args=_connect_args, pool_pre_ping=True, pool_size=5, max_overflow=5)
    if _url
    else None
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False) if engine else None


async def get_session():
    async with SessionLocal() as session:
        yield session
