"""Outbox-загрузчик: доставляет собранные клипы в админку.

Событие лежит в outbox, пока админка не подтвердит полную загрузку —
обрыв связи, рестарт агента или админки не теряют данные:
  1. POST /api/agent/events (идемпотентно по client_event_id) —
     в ответе оффсет уже принятых байт;
  2. видео чанками по 8 МиБ с Content-Range с этого оффсета;
  3. превью;
  4. каталог события удаляется.

При недоступности админки — экспоненциальная пауза. При переполнении
outbox (диск не резиновый) удаляются самые старые события.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

import aiohttp

from cerber_agent.config import Bootstrap

log = logging.getLogger(__name__)

CHUNK_SIZE = 8 * 1024 * 1024
BACKOFF_MIN_S = 5
BACKOFF_MAX_S = 300


class PermanentUploadError(Exception):
    """Админка ответила 4xx — повторять бессмысленно."""


class Uploader:
    def __init__(self, bootstrap: Bootstrap, data_dir: Path):
        self.bootstrap = bootstrap
        self.outbox = data_dir / "outbox"
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.failed_dir = self.outbox / "failed"

    async def run(self) -> None:
        backoff = BACKOFF_MIN_S
        timeout = aiohttp.ClientTimeout(total=600, connect=20)
        async with aiohttp.ClientSession(
            timeout=timeout, headers=self.bootstrap.auth_headers
        ) as session:
            while True:
                self._enforce_disk_cap()
                event_dir = self._oldest_ready()
                if event_dir is None:
                    await asyncio.sleep(3)
                    continue
                try:
                    await self._upload_event(session, event_dir)
                    shutil.rmtree(event_dir, ignore_errors=True)
                    backoff = BACKOFF_MIN_S
                except PermanentUploadError as exc:
                    log.error("Событие %s отвергнуто админкой: %s", event_dir.name, exc)
                    self._move_to_failed(event_dir)
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    log.warning(
                        "Админка недоступна (%s) — повтор через %d с", exc, backoff
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX_S)

    # --- одна загрузка ---

    async def _upload_event(self, session: aiohttp.ClientSession, event_dir: Path) -> None:
        meta = json.loads((event_dir / "meta.json").read_text())
        clip = event_dir / "clip.mp4"
        base = self.bootstrap.admin_url

        async with session.post(f"{base}/api/agent/events", json=meta) as resp:
            self._raise_for_status(resp)
            created = await resp.json()
        event_id = created["event_id"]
        offset = int(created.get("bytes_received", 0))
        status = created.get("status", "uploading")

        total = clip.stat().st_size
        if status != "stored":
            offset = await self._upload_video(session, event_id, clip, offset, total)
            if offset < total:
                raise PermanentUploadError("админка приняла меньше байт, чем отправлено")

        thumb = event_dir / "thumb.jpg"
        if thumb.exists():
            async with session.put(
                f"{base}/api/agent/events/{event_id}/thumb",
                data=thumb.read_bytes(),
                headers={"Content-Type": "image/jpeg"},
            ) as resp:
                # превью не критично — не валим загрузку из-за него
                if resp.status >= 400:
                    log.warning("Превью не принято: HTTP %d", resp.status)

        log.info("Событие %s доставлено в админку", event_id)

    async def _upload_video(
        self,
        session: aiohttp.ClientSession,
        event_id: str,
        clip: Path,
        offset: int,
        total: int,
    ) -> int:
        url = f"{self.bootstrap.admin_url}/api/agent/events/{event_id}/video"
        with clip.open("rb") as f:
            while offset < total:
                f.seek(offset)
                chunk = f.read(CHUNK_SIZE)
                end = offset + len(chunk) - 1
                headers = {
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                    "Content-Type": "application/octet-stream",
                }
                async with session.put(url, data=chunk, headers=headers) as resp:
                    if resp.status == 409:
                        # Рассинхрон (например, рестарт агента) — админка говорит,
                        # сколько уже принято, продолжаем оттуда
                        payload = await resp.json()
                        offset = int(payload["bytes_received"])
                        log.info("Продолжение загрузки с %d байт", offset)
                        continue
                    self._raise_for_status(resp)
                    payload = await resp.json()
                    offset = int(payload["bytes_received"])
        return offset

    @staticmethod
    def _raise_for_status(resp: aiohttp.ClientResponse) -> None:
        if 400 <= resp.status < 500:
            raise PermanentUploadError(f"HTTP {resp.status}")
        if resp.status >= 500:
            raise aiohttp.ClientError(f"HTTP {resp.status}")

    # --- обслуживание outbox ---

    def _oldest_ready(self) -> Path | None:
        dirs = [
            d
            for d in self.outbox.iterdir()
            if d.is_dir()
            and not d.name.endswith(".tmp")
            and d.name != "failed"
            and (d / "meta.json").exists()
            and (d / "clip.mp4").exists()
        ]
        if not dirs:
            return None
        return min(dirs, key=lambda d: (d / "meta.json").stat().st_mtime)

    def _move_to_failed(self, event_dir: Path) -> None:
        self.failed_dir.mkdir(exist_ok=True)
        target = self.failed_dir / event_dir.name
        shutil.rmtree(target, ignore_errors=True)
        event_dir.rename(target)
        # держим не больше десятка неудачных — только для разбора полётов
        failed = sorted(self.failed_dir.iterdir(), key=lambda d: d.stat().st_mtime)
        for extra in failed[:-10]:
            shutil.rmtree(extra, ignore_errors=True)

    def _enforce_disk_cap(self) -> None:
        limit = self.bootstrap.outbox_max_mb * 1024 * 1024
        entries = []
        total = 0
        for d in self.outbox.iterdir():
            if not d.is_dir() or d.name == "failed":
                continue
            size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
            entries.append((d.stat().st_mtime, size, d))
            total += size
        entries.sort()
        while total > limit and entries:
            _, size, oldest = entries.pop(0)
            log.warning("Outbox переполнен — удаляю старое событие %s", oldest.name)
            shutil.rmtree(oldest, ignore_errors=True)
            total -= size
