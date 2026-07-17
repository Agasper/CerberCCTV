"""Фоновая очистка: события старше retention.days удаляются из S3 и БД.

Заодно приводятся в порядок зависшие загрузки: если событие висит
в статусе uploading дольше суток, multipart-загрузка прерывается
(незавершённые part'ы в S3 занимают место и стоят денег), а событие
помечается failed — так его видно в интерфейсе.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from cerber_admin.config import load_config
from cerber_admin.db import SessionLocal
from cerber_admin.models import Event
from cerber_admin.s3 import get_s3

log = logging.getLogger(__name__)

INTERVAL_S = 3600
STALE_UPLOAD_AGE = timedelta(hours=24)


async def _cleanup_once() -> None:
    async with SessionLocal() as session:
        cfg, version = await load_config(session)
        if not cfg.s3.configured:
            return
        s3 = get_s3(cfg.s3, version)
        now = datetime.now(timezone.utc)

        # Зависшие загрузки -> failed
        stale = (
            (
                await session.execute(
                    select(Event).where(
                        Event.status == "uploading", Event.created_at < now - STALE_UPLOAD_AGE
                    )
                )
            )
            .scalars()
            .all()
        )
        for event in stale:
            if event.upload_id and event.s3_key_video:
                await s3.abort_multipart(event.s3_key_video, event.upload_id)
            event.status = "failed"
            event.upload_id = None
            event.parts = []
            log.warning("Загрузка события %s зависла — помечено failed", event.id)
        if stale:
            await session.commit()

        if not cfg.retention.enabled:
            return
        cutoff = now - timedelta(days=cfg.retention.days)
        old = (
            (await session.execute(select(Event).where(Event.started_at < cutoff)))
            .scalars()
            .all()
        )
        if not old:
            return
        keys: list[str] = []
        for event in old:
            if event.upload_id and event.s3_key_video:
                await s3.abort_multipart(event.s3_key_video, event.upload_id)
            keys.extend(k for k in (event.s3_key_video, event.s3_key_thumb) if k)
            await session.delete(event)
        await s3.delete_objects(keys)
        await session.commit()
        log.info("Ретенция: удалено %d событий старше %d дн.", len(old), cfg.retention.days)


async def retention_loop() -> None:
    while True:
        try:
            await _cleanup_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — очистка не должна ронять приложение
            log.exception("Ошибка фоновой очистки")
        await asyncio.sleep(INTERVAL_S)
