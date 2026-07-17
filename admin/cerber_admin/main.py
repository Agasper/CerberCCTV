"""Приложение админки: FastAPI + фоновые задачи.

При старте (после миграций, которые запускает entrypoint):
  - создаётся первый пользователь из ADMIN_USERNAME/ADMIN_PASSWORD,
    если пользователей ещё нет;
  - создаётся запись агента; если задан AGENT_TOKEN — его хэш
    синхронизируется (удобно для dev и первичной настройки);
  - создаётся стартовый конфиг с затравкой из окружения;
  - запускается фоновая ретенция.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from cerber_admin.api import agent as agent_api
from cerber_admin.api import ui as ui_api
from cerber_admin.auth import NotAuthenticated, hash_password, hash_token
from cerber_admin.config import load_config
from cerber_admin.db import SessionLocal
from cerber_admin.env import env
from cerber_admin.hub import AgentHub
from cerber_admin.live import LiveHub
from cerber_admin.models import Agent, User
from cerber_admin.retention import retention_loop

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger(__name__)


async def bootstrap() -> None:
    async with SessionLocal() as session:
        users_exist = (await session.execute(select(User.id).limit(1))).first() is not None
        if not users_exist:
            if not env.admin_password:
                raise RuntimeError(
                    "Пользователей нет, а ADMIN_PASSWORD не задан — задайте его для первого запуска"
                )
            session.add(
                User(username=env.admin_username, password_hash=hash_password(env.admin_password))
            )
            log.info("Создан пользователь %s", env.admin_username)

        agent = (await session.execute(select(Agent).order_by(Agent.id))).scalars().first()
        if agent is None:
            agent = Agent(name=env.agent_name, token_hash="")
            session.add(agent)
            log.info("Создана запись агента %s", env.agent_name)
        if env.agent_token:
            agent.token_hash = hash_token(env.agent_token)
        elif not agent.token_hash:
            log.warning(
                "Токен агента не задан: перевыпустите его на странице настроек "
                "или передайте AGENT_TOKEN"
            )
        await session.commit()
        await load_config(session)  # создаст стартовый конфиг при пустой таблице


@asynccontextmanager
async def lifespan(app: FastAPI):
    env.validate()
    await bootstrap()
    task = asyncio.create_task(retention_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="CerberCCTV Admin", lifespan=lifespan, docs_url=None, redoc_url=None)
app.state.hub = AgentHub()
app.state.livehub = LiveHub(app.state.hub)

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(agent_api.router)
app.include_router(ui_api.router)


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
