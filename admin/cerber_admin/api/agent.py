"""API для агента на Raspberry Pi. Авторизация — Bearer-токен агента.

Загрузка видео: агент шлёт чанки по ~8 МиБ с заголовком Content-Range,
каждый чанк сразу уходит как part в S3 multipart upload — диск админки
не используется (в App Platform он эфемерный). Состояние загрузки
хранится в строке события и переживает рестарт админки.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from cerber_common import EventCreate, EventCreated, HeartbeatStatus
from cerber_admin.auth import require_agent, ws_agent
from cerber_admin.config import load_config
from cerber_admin.db import SessionLocal, get_session
from cerber_admin.models import Agent, Event
from cerber_admin.s3 import get_s3

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent")

# Минимальный размер part в S3 multipart (кроме последнего)
MIN_PART = 5 * 1024 * 1024
MAX_CHUNK = 32 * 1024 * 1024

_content_range_re = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


async def _get_s3_service(session: AsyncSession):
    cfg, version = await load_config(session)
    if not cfg.s3.configured:
        raise HTTPException(status_code=503, detail="S3 не настроен в админке")
    return cfg, get_s3(cfg.s3, version)


# ---------------------------------------------------------------- control WS


@router.websocket("/ws")
async def agent_control_ws(websocket: WebSocket) -> None:
    async with SessionLocal() as session:
        agent = await ws_agent(websocket, session)
    if agent is None:
        await websocket.accept()
        await websocket.close(code=4401)
        return

    await websocket.accept()
    hub = websocket.app.state.hub
    livehub = websocket.app.state.livehub
    hub.register(agent.id, websocket)
    log.info("Агент %s подключил control-канал", agent.name)
    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")
            if msg_type == "hello":
                async with SessionLocal() as session:
                    cfg, version = await load_config(session)
                if int(msg.get("config_version", -1)) != version:
                    await websocket.send_json(
                        {"type": "config", "version": version,
                         "config": cfg.agent.model_dump(mode="json")}
                    )
            elif msg_type == "heartbeat":
                status = HeartbeatStatus.model_validate(msg.get("status") or {})
                async with SessionLocal() as session:
                    row = await session.get(Agent, agent.id)
                    row.last_seen = datetime.now(timezone.utc)
                    row.status = status.model_dump(mode="json")
                    await session.commit()
                    _, version = await load_config(session)
                await websocket.send_json(
                    {"type": "heartbeat_ack", "config_version": version,
                     "live_wanted": livehub.live_wanted}
                )
    except WebSocketDisconnect:
        pass
    finally:
        hub.unregister(agent.id, websocket)
        log.info("Агент %s отключил control-канал", agent.name)


# ------------------------------------------------------------------- live WS


@router.websocket("/live")
async def agent_live_ws(websocket: WebSocket) -> None:
    async with SessionLocal() as session:
        agent = await ws_agent(websocket, session)
    if agent is None:
        await websocket.accept()
        await websocket.close(code=4401)
        return

    await websocket.accept()
    log.info("Агент %s начал live-трансляцию", agent.name)
    try:
        await websocket.app.state.livehub.attach_agent(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        log.info("Агент %s завершил live-трансляцию", agent.name)


# ---------------------------------------------------------------------- REST


@router.get("/config")
async def get_agent_config(
    agent: Agent = Depends(require_agent), session: AsyncSession = Depends(get_session)
):
    cfg, version = await load_config(session)
    return {"version": version, "config": cfg.agent.model_dump(mode="json")}


@router.post("/snapshot")
async def post_snapshot(
    request: Request,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    body = await request.body()
    if not body or len(body) > 4 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Ожидается JPEG до 4 МиБ")
    request.app.state.hub.set_snapshot(agent.id, body)
    row = await session.get(Agent, agent.id)
    row.last_seen = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True}


@router.post("/events", response_model=EventCreated)
async def create_event(
    payload: EventCreate,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    existing = (
        await session.execute(select(Event).where(Event.client_event_id == payload.client_event_id))
    ).scalar_one_or_none()
    if existing is not None:
        return EventCreated(
            event_id=existing.id, bytes_received=existing.bytes_received, status=existing.status
        )

    event = Event(
        id=str(uuid.uuid4()),
        agent_id=agent.id,
        client_event_id=payload.client_event_id,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
        duration_s=payload.duration_s,
        motion_score=payload.motion_score,
        size_bytes=payload.size_bytes,
        status="uploading",
        bytes_received=0,
        parts=[],
    )
    session.add(event)
    await session.commit()
    return EventCreated(event_id=event.id, bytes_received=0, status="uploading")


async def _load_event(session: AsyncSession, event_id: str, agent: Agent) -> Event:
    event = await session.get(Event, event_id)
    if event is None or event.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Событие не найдено")
    return event


@router.get("/events/{event_id}/upload-status")
async def upload_status(
    event_id: str,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    event = await _load_event(session, event_id, agent)
    return {"bytes_received": event.bytes_received, "status": event.status}


@router.put("/events/{event_id}/video")
async def upload_video_chunk(
    event_id: str,
    request: Request,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    event = await _load_event(session, event_id, agent)
    if event.status == "stored":
        return {"bytes_received": event.bytes_received, "status": "stored"}
    if event.status != "uploading":
        raise HTTPException(status_code=409, detail=f"Событие в статусе {event.status}")

    match = _content_range_re.match(request.headers.get("content-range", ""))
    if not match:
        raise HTTPException(status_code=400, detail="Нужен заголовок Content-Range: bytes a-b/total")
    start, end, total = int(match[1]), int(match[2]), int(match[3])

    if total != event.size_bytes:
        raise HTTPException(status_code=400, detail="Размер не совпадает с заявленным при регистрации")
    if start != event.bytes_received:
        # Агент рассинхронизировался (например, после рестарта) — сообщаем оффсет
        return JSONResponse(
            status_code=409,
            content={"bytes_received": event.bytes_received, "status": event.status},
        )

    body = await request.body()
    if len(body) != end - start + 1 or len(body) > MAX_CHUNK:
        raise HTTPException(status_code=400, detail="Длина тела не совпадает с Content-Range")
    is_last = end + 1 >= total
    if not is_last and len(body) < MIN_PART:
        raise HTTPException(status_code=400, detail=f"Непоследний чанк должен быть ≥ {MIN_PART} байт")

    cfg, s3 = await _get_s3_service(session)

    if event.upload_id is None:
        day = event.started_at.strftime("%Y/%m/%d")
        key = f"{cfg.s3.prefix}/videos/{day}/{event.id}.mp4"
        event.s3_key_video = key
        event.upload_id = await s3.create_multipart(key, "video/mp4")
        event.parts = []
        await session.commit()

    part_number = len(event.parts or []) + 1
    etag = await s3.upload_part(event.s3_key_video, event.upload_id, part_number, body)
    event.parts = list(event.parts or []) + [{"PartNumber": part_number, "ETag": etag}]
    event.bytes_received = end + 1
    await session.commit()

    if is_last:
        await s3.complete_multipart(event.s3_key_video, event.upload_id, event.parts)
        event.status = "stored"
        event.upload_id = None
        event.parts = []
        await session.commit()
        log.info("Событие %s загружено в S3 (%d байт)", event.id, total)

    return {"bytes_received": event.bytes_received, "status": event.status}


@router.put("/events/{event_id}/thumb")
async def upload_thumb(
    event_id: str,
    request: Request,
    agent: Agent = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
):
    event = await _load_event(session, event_id, agent)
    body = await request.body()
    if not body or len(body) > 4 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Ожидается JPEG до 4 МиБ")

    cfg, s3 = await _get_s3_service(session)
    day = event.started_at.strftime("%Y/%m/%d")
    key = f"{cfg.s3.prefix}/thumbs/{day}/{event.id}.jpg"
    await s3.put_object(key, body, "image/jpeg")
    event.s3_key_thumb = key
    await session.commit()
    return {"ok": True}
