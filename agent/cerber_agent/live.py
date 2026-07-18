"""Live-трансляция по запросу: RTSP -> fMP4 (stream copy) -> WebSocket.

ffmpeg фрагментирует поток по ключевым кадрам (frag_keyframe) — каждый
медиа-сегмент начинается с keyframe, поэтому зритель может подключиться
в любой момент. Поток из stdout режется на mp4-боксы:
  ftyp+moov              -> init-сегмент (первый бинарный кадр в WS)
  [styp?]+moof+mdat      -> медиа-сегмент (по одному кадру WS на сегмент)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import aiohttp

from cerber_agent.config import Bootstrap, ConfigStore

log = logging.getLogger(__name__)

# Защита от deadlock readexactly: буфер стрима должен вмещать целый бокс
STREAM_LIMIT = 64 * 1024 * 1024


class LiveStreamer:
    def __init__(self, bootstrap: Bootstrap, store: ConfigStore):
        self.bootstrap = bootstrap
        self.store = store
        self._task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.active:
            return
        url = self.store.config.camera.rtsp_main
        if not url:
            log.warning("Live запрошен, но RTSP-адрес камеры не настроен")
            return
        self._task = asyncio.create_task(self._run(url))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self, url: str) -> None:
        max_duration = self.store.config.live.max_duration_s
        proc: asyncio.subprocess.Process | None = None
        try:
            async with aiohttp.ClientSession(headers=self.bootstrap.auth_headers) as session:
                async with session.ws_connect(
                    f"{self.bootstrap.ws_url}/api/agent/live", heartbeat=20
                ) as ws:
                    proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-rtsp_transport", "tcp", "-timeout", "10000000",
                        "-i", url,
                        "-map", "0:v", "-map", "0:a?",
                        "-c:v", "copy",
                        # Звук камеры (обычно G.711) сразу в AAC — иначе его
                        # не положить в fMP4 и не сыграть в браузере
                        "-c:a", "aac", "-b:a", "48k",
                        "-f", "mp4",
                        "-movflags", "empty_moov+default_base_moof+frag_keyframe",
                        "pipe:1",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        limit=STREAM_LIMIT,
                    )
                    log.info("Live-трансляция запущена")
                    await asyncio.wait_for(
                        self._pump(proc.stdout, ws), timeout=max_duration
                    )
        except asyncio.TimeoutError:
            log.info("Live-трансляция остановлена по таймауту %d с", max_duration)
        except asyncio.CancelledError:
            log.info("Live-трансляция остановлена")
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Live-трансляция прервана: %s", exc)
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

    async def _pump(self, stream: asyncio.StreamReader, ws) -> None:
        pending: list[bytes] = []
        while True:
            box, box_type = await self._read_box(stream)
            pending.append(box)
            if box_type == b"moov":
                # ftyp+moov собраны — это init-сегмент
                await ws.send_bytes(b"".join(pending))
                pending = []
            elif box_type == b"mdat":
                # завершён медиа-сегмент (moof+mdat)
                await ws.send_bytes(b"".join(pending))
                pending = []

    @staticmethod
    async def _read_box(stream: asyncio.StreamReader) -> tuple[bytes, bytes]:
        header = await stream.readexactly(8)
        size = int.from_bytes(header[:4], "big")
        box_type = header[4:8]
        if size == 1:
            ext = await stream.readexactly(8)
            payload = await stream.readexactly(int.from_bytes(ext, "big") - 16)
            return header + ext + payload, box_type
        if size < 8:
            raise RuntimeError(f"Некорректный mp4-бокс: size={size}")
        payload = await stream.readexactly(size - 8)
        return header + payload, box_type
