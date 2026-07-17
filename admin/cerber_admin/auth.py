"""Авторизация: сессии пользователей (cookie) и токены агентов (Bearer).

Сессия — подписанный itsdangerous cookie с id пользователя, состояния
на сервере нет. Токен агента хранится в БД только как sha256-хэш.
"""

from __future__ import annotations

import hashlib
import secrets

import bcrypt
from fastapi import Depends, Request, WebSocket
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from cerber_admin.db import get_session
from cerber_admin.env import env
from cerber_admin.models import Agent, User

SESSION_COOKIE = "cerber_session"

_serializer = URLSafeTimedSerializer(env.secret_key or "dev-secret", salt="cerber.session")


class NotAuthenticated(Exception):
    """Перехватывается в main.py и превращается в redirect на /login."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def make_session_cookie(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session_cookie(value: str | None) -> int | None:
    if not value:
        return None
    try:
        data = _serializer.loads(value, max_age=env.session_max_age_s)
        return int(data["uid"])
    except (BadSignature, KeyError, ValueError, TypeError):
        return None


async def _user_from_cookie(cookie: str | None, session: AsyncSession) -> User | None:
    uid = read_session_cookie(cookie)
    if uid is None:
        return None
    return (await session.execute(select(User).where(User.id == uid))).scalar_one_or_none()


async def require_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    user = await _user_from_cookie(request.cookies.get(SESSION_COOKIE), session)
    if user is None:
        raise NotAuthenticated()
    return user


async def ws_user(websocket: WebSocket, session: AsyncSession) -> User | None:
    return await _user_from_cookie(websocket.cookies.get(SESSION_COOKIE), session)


async def _agent_by_token(auth_header: str | None, session: AsyncSession) -> Agent | None:
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[7:].strip()
    if not token:
        return None
    token_h = hash_token(token)
    agent = (await session.execute(select(Agent).where(Agent.token_hash == token_h))).scalar_one_or_none()
    return agent


async def require_agent(request: Request, session: AsyncSession = Depends(get_session)) -> Agent:
    agent = await _agent_by_token(request.headers.get("authorization"), session)
    if agent is None:
        raise HTTPException(status_code=401, detail="Невалидный токен агента")
    return agent


async def ws_agent(websocket: WebSocket, session: AsyncSession) -> Agent | None:
    return await _agent_by_token(websocket.headers.get("authorization"), session)
