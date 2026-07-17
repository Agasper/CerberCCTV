"""Страницы админки. Все, кроме /login, требуют сессию."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from cerber_common import AgentConfig
from cerber_admin.auth import (
    SESSION_COOKIE,
    generate_token,
    hash_password,
    hash_token,
    make_session_cookie,
    require_user,
    verify_password,
    ws_user,
)
from cerber_admin.config import RetentionConfig, S3Config, load_config, save_config
from cerber_admin.db import SessionLocal, get_session
from cerber_admin.env import env
from cerber_admin.models import Agent, Event, User
from cerber_admin.s3 import S3Service, get_s3
from cerber_admin.web import templates

log = logging.getLogger(__name__)

router = APIRouter()

PAGE_SIZE = 24
ONLINE_THRESHOLD_S = 45


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------------- login


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Неверный логин или пароль"}, status_code=401
        )
    response = _redirect("/")
    response.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(user.id),
        max_age=env.session_max_age_s,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
async def logout():
    response = _redirect("/login")
    response.delete_cookie(SESSION_COOKIE)
    return response


# ------------------------------------------------------------------ dashboard


@router.get("/")
async def dashboard(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    agent = (await session.execute(select(Agent).order_by(Agent.id))).scalars().first()
    online = False
    if agent and agent.last_seen:
        online = (datetime.now(timezone.utc) - agent.last_seen).total_seconds() < ONLINE_THRESHOLD_S

    recent = (
        (await session.execute(select(Event).order_by(desc(Event.started_at)).limit(6)))
        .scalars()
        .all()
    )
    cfg, _ = await load_config(session)
    hints = []
    if not cfg.agent.camera.rtsp_main:
        hints.append("Не задан RTSP-адрес камеры — заполните раздел «Камера» в настройках.")
    if not cfg.s3.configured:
        hints.append("Не настроено S3-хранилище — клипы не будут сохраняться.")
    if agent is None or not agent.last_seen:
        hints.append("Агент ещё ни разу не выходил на связь.")

    has_snapshot = request.app.state.hub.get_snapshot() is not None
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "agent": agent,
            "online": online,
            "recent": recent,
            "hints": hints,
            "has_snapshot": has_snapshot,
            "active": "dashboard",
        },
    )


@router.get("/snapshot.jpg")
async def snapshot(request: Request, user: User = Depends(require_user)):
    snap = request.app.state.hub.get_snapshot()
    if snap is None:
        raise HTTPException(status_code=404, detail="Снапшота ещё нет")
    ts, jpeg = snap
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Snapshot-At": ts.isoformat()},
    )


# -------------------------------------------------------------------- events


@router.get("/events")
async def events_page(
    request: Request,
    day: str | None = None,
    page: int = 1,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    query = select(Event).order_by(desc(Event.started_at))
    selected_day: date | None = None
    if day:
        try:
            selected_day = date.fromisoformat(day)
        except ValueError:
            selected_day = None
    if selected_day:
        start = datetime.combine(selected_day, time.min).astimezone()
        query = query.where(
            Event.started_at >= start, Event.started_at < start + timedelta(days=1)
        )
    page = max(page, 1)
    rows = (
        (await session.execute(query.limit(PAGE_SIZE + 1).offset((page - 1) * PAGE_SIZE)))
        .scalars()
        .all()
    )
    has_next = len(rows) > PAGE_SIZE
    return templates.TemplateResponse(
        request,
        "events.html",
        {
            "events": rows[:PAGE_SIZE],
            "page": page,
            "has_next": has_next,
            "day": selected_day.isoformat() if selected_day else "",
            "active": "events",
        },
    )


async def _stored_event(session: AsyncSession, event_id: str) -> Event:
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    return event


@router.get("/events/{event_id}")
async def event_page(
    request: Request,
    event_id: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    event = await _stored_event(session, event_id)
    return templates.TemplateResponse(
        request, "event.html", {"event": event, "active": "events"}
    )


@router.get("/events/{event_id}/video")
async def event_video(
    event_id: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    event = await _stored_event(session, event_id)
    if event.status != "stored" or not event.s3_key_video:
        raise HTTPException(status_code=409, detail="Видео ещё не загружено")
    cfg, version = await load_config(session)
    s3 = get_s3(cfg.s3, version)
    return _redirect(await s3.presign_get(event.s3_key_video))


@router.get("/events/{event_id}/thumb")
async def event_thumb(
    event_id: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    event = await _stored_event(session, event_id)
    if not event.s3_key_thumb:
        raise HTTPException(status_code=404, detail="Превью нет")
    cfg, version = await load_config(session)
    s3 = get_s3(cfg.s3, version)
    return _redirect(await s3.presign_get(event.s3_key_thumb))


@router.post("/events/{event_id}/delete")
async def event_delete(
    event_id: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    event = await _stored_event(session, event_id)
    cfg, version = await load_config(session)
    if cfg.s3.configured:
        s3 = get_s3(cfg.s3, version)
        if event.upload_id and event.s3_key_video:
            await s3.abort_multipart(event.s3_key_video, event.upload_id)
        await s3.delete_objects([event.s3_key_video, event.s3_key_thumb])
    await session.delete(event)
    await session.commit()
    return _redirect("/events")


# ---------------------------------------------------------------------- live


@router.get("/live")
async def live_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "live.html", {"active": "live"})


@router.websocket("/ws/live")
async def live_viewer_ws(websocket: WebSocket) -> None:
    async with SessionLocal() as session:
        user = await ws_user(websocket, session)
    if user is None:
        await websocket.accept()
        await websocket.close(code=4401)
        return

    await websocket.accept()
    livehub = websocket.app.state.livehub
    await livehub.viewer_join(websocket)
    try:
        while True:
            # Зритель ничего не шлёт; ждём разрыва соединения
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await livehub.viewer_leave(websocket)


# ------------------------------------------------------------------ settings


def _form_bool(form, name: str) -> bool:
    return name in form


async def _render_settings(
    request: Request,
    session: AsyncSession,
    msg: str | None = None,
    err: str | None = None,
    new_token: str | None = None,
    status_code: int = 200,
):
    cfg, version = await load_config(session)
    agent = (await session.execute(select(Agent).order_by(Agent.id))).scalars().first()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "cfg": cfg,
            "version": version,
            "agent": agent,
            "msg": msg,
            "err": err,
            "new_token": new_token,
            "active": "settings",
        },
        status_code=status_code,
    )


@router.get("/settings")
async def settings_page(
    request: Request,
    msg: str | None = None,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    return await _render_settings(request, session, msg=msg)


async def _save_agent_section(request: Request, session: AsyncSession, update) -> None:
    """Обновить агентскую часть конфига и сразу дослать её агенту по WS."""
    cfg, _ = await load_config(session)
    update(cfg)
    # Валидация всей агентской части до сохранения
    cfg.agent = AgentConfig.model_validate(cfg.agent.model_dump())
    version = await save_config(session, cfg)
    await request.app.state.hub.push_config(version, cfg.agent.model_dump(mode="json"))


@router.post("/settings/camera")
async def settings_camera(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    try:
        def update(cfg):
            cfg.agent.camera.rtsp_main = str(form.get("rtsp_main", "")).strip()
            cfg.agent.camera.rtsp_sub = str(form.get("rtsp_sub", "")).strip()
            cfg.agent.camera.snapshot_interval_s = int(form.get("snapshot_interval_s", 30))

        await _save_agent_section(request, session, update)
    except (ValidationError, ValueError) as exc:
        return await _render_settings(request, session, err=f"Камера: {exc}", status_code=400)
    return _redirect("/settings?msg=Настройки камеры сохранены")


@router.post("/settings/motion")
async def settings_motion(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    try:
        def update(cfg):
            m = cfg.agent.motion
            m.enabled = _form_bool(form, "enabled")
            m.process_fps = float(form.get("process_fps", m.process_fps))
            m.sensitivity = int(form.get("sensitivity", m.sensitivity))
            m.min_area_pct = float(form.get("min_area_pct", m.min_area_pct))
            m.min_consecutive_frames = int(form.get("min_consecutive_frames", m.min_consecutive_frames))
            m.pre_roll_s = int(form.get("pre_roll_s", m.pre_roll_s))
            m.post_roll_s = int(form.get("post_roll_s", m.post_roll_s))
            m.cooldown_s = int(form.get("cooldown_s", m.cooldown_s))
            m.max_clip_s = int(form.get("max_clip_s", m.max_clip_s))

        await _save_agent_section(request, session, update)
    except (ValidationError, ValueError) as exc:
        return await _render_settings(request, session, err=f"Движение: {exc}", status_code=400)
    return _redirect("/settings?msg=Настройки детекции сохранены")


@router.post("/settings/zones")
async def settings_zones(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    try:
        parsed = json.loads(str(form.get("zones", "[]")))
        if not isinstance(parsed, list) or len(parsed) > 20:
            raise ValueError("не больше 20 зон")
        zones = []
        for poly in parsed:
            if not isinstance(poly, list) or not 3 <= len(poly) <= 100:
                raise ValueError("полигон — от 3 до 100 точек")
            zones.append(
                [(min(max(float(x), 0.0), 1.0), min(max(float(y), 0.0), 1.0)) for x, y in poly]
            )

        def update(cfg):
            cfg.agent.motion.zones = zones

        await _save_agent_section(request, session, update)
    except (ValidationError, ValueError, TypeError) as exc:
        return await _render_settings(request, session, err=f"Зоны: {exc}", status_code=400)
    if zones:
        return _redirect(f"/settings?msg=Сохранено зон: {len(zones)} — движение ищется только внутри них")
    return _redirect("/settings?msg=Зоны очищены — движение ищется по всему кадру")


@router.post("/settings/storage")
async def settings_storage(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    cfg, _ = await load_config(session)
    try:
        secret = str(form.get("secret_key", ""))
        cfg.s3 = S3Config(
            endpoint_url=str(form.get("endpoint_url", "")).strip(),
            public_url=str(form.get("public_url", "")).strip(),
            region=str(form.get("region", "")).strip(),
            bucket=str(form.get("bucket", "")).strip(),
            access_key=str(form.get("access_key", "")).strip(),
            # Пустое поле секрета означает «оставить прежний»
            secret_key=secret if secret else cfg.s3.secret_key,
            force_path_style=_form_bool(form, "force_path_style"),
            prefix=str(form.get("prefix", "cerber")).strip() or "cerber",
            presign_ttl_s=int(form.get("presign_ttl_s", 3600)),
        )
    except (ValidationError, ValueError) as exc:
        return await _render_settings(request, session, err=f"S3: {exc}", status_code=400)
    await save_config(session, cfg)
    return _redirect("/settings?msg=Настройки S3 сохранены")


@router.post("/settings/s3-test")
async def settings_s3_test(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    cfg, version = await load_config(session)
    if not cfg.s3.configured:
        return await _render_settings(request, session, err="S3: заполните bucket и ключи", status_code=400)
    try:
        await S3Service(cfg.s3).check()
    except Exception as exc:  # noqa: BLE001 — показываем причину пользователю
        return await _render_settings(request, session, err=f"S3 недоступен: {exc}", status_code=400)
    return _redirect("/settings?msg=S3 доступен, запись и удаление работают")


@router.post("/settings/retention")
async def settings_retention(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    cfg, _ = await load_config(session)
    try:
        cfg.retention = RetentionConfig(
            enabled=_form_bool(form, "enabled"),
            days=int(form.get("days", cfg.retention.days)),
        )
    except (ValidationError, ValueError) as exc:
        return await _render_settings(request, session, err=f"Ретенция: {exc}", status_code=400)
    await save_config(session, cfg)
    return _redirect("/settings?msg=Настройки хранения сохранены")


@router.post("/settings/password")
async def settings_password(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    current = str(form.get("current_password", ""))
    new = str(form.get("new_password", ""))
    confirm = str(form.get("confirm_password", ""))
    if not verify_password(current, user.password_hash):
        return await _render_settings(request, session, err="Текущий пароль неверен", status_code=400)
    if len(new) < 8:
        return await _render_settings(request, session, err="Новый пароль короче 8 символов", status_code=400)
    if new != confirm:
        return await _render_settings(request, session, err="Пароли не совпадают", status_code=400)
    row = await session.get(User, user.id)
    row.password_hash = hash_password(new)
    await session.commit()
    return _redirect("/settings?msg=Пароль изменён")


@router.post("/settings/agent-token")
async def settings_agent_token(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    agent = (await session.execute(select(Agent).order_by(Agent.id))).scalars().first()
    if agent is None:
        agent = Agent(name=env.agent_name, token_hash="")
        session.add(agent)
    token = generate_token()
    agent.token_hash = hash_token(token)
    await session.commit()
    return await _render_settings(
        request,
        session,
        msg="Токен перевыпущен — скопируйте его сейчас, больше он не показывается",
        new_token=token,
    )
