"""Подключение к Postgres.

DATABASE_URL принимается в любом привычном виде (postgres://,
postgresql://, postgresql+asyncpg://). Параметр sslmode из URL asyncpg
не понимает — вырезаем его и превращаем в ssl-контекст:
  - require            — TLS без проверки сертификата (как в libpq)
  - verify-ca/full     — TLS с проверкой
DO Managed Postgres выдаёт URL именно с sslmode=require.
"""

from __future__ import annotations

import ssl
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
