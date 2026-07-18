"""Live-ретранслятор: агент пушит fMP4 по WS, зрители получают его по WS.

Протокол для зрителя:
  текст  {"type": "waiting"}                    — поток ещё не идёт
  текст  {"type": "stream", "mime": "..."}      — дальше пойдут данные
  бинарь init-сегмент (ftyp+moov)
  бинарь медиа-сегменты (moof+mdat), каждый начинается с keyframe
  текст  {"type": "ended"}                      — агент отключился

Запуск/остановка трансляции управляются числом зрителей: первый зритель
включает live_wanted (команда агенту + флаг в heartbeat_ack на случай
потери команды), спустя STOP_GRACE_S после ухода последнего — выключает.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

from cerber_admin.hub import AgentHub

log = logging.getLogger(__name__)

STOP_GRACE_S = 20


def guess_mime(init_segment: bytes) -> str:
    """Собрать MSE codec string из init-сегмента.

    Для H.264 профиль/уровень лежат в боксе avcC сразу после fourcc:
    [version][profile][compat][level]. Для H.265 точную строку собрать
    сложнее — отдаём типовую, современные браузеры обычно принимают.
    Если в init есть аудиодорожка (бокс mp4a, агент кодирует звук в
    AAC-LC), она обязана попасть в codecs — иначе MSE отвергнет поток.
    """
    audio = ", mp4a.40.2" if b"mp4a" in init_segment else ""
    i = init_segment.find(b"avcC")
    if i != -1 and len(init_segment) >= i + 8:
        profile = init_segment[i + 5]
        compat = init_segment[i + 6]
        level = init_segment[i + 7]
        return f'video/mp4; codecs="avc1.{profile:02X}{compat:02X}{level:02X}{audio}"'
    if b"hvcC" in init_segment:
        return f'video/mp4; codecs="hvc1.1.6.L120.90{audio}"'
    return f'video/mp4; codecs="avc1.42E01E{audio}"'


def is_init_segment(data: bytes) -> bool:
    return len(data) >= 8 and data[4:8] == b"ftyp"


class LiveHub:
    def __init__(self, agent_hub: AgentHub) -> None:
        self.agent_hub = agent_hub
        self.viewers: set[WebSocket] = set()
        self.init_segment: bytes | None = None
        self.mime: str | None = None
        self.agent_streaming = False
        self.live_wanted = False
        self._stop_task: asyncio.Task | None = None

    # --- сторона агента ---

    async def attach_agent(self, ws: WebSocket) -> None:
        """Читает бинарный поток от агента до разрыва соединения."""
        self.agent_streaming = True
        try:
            while True:
                data = await ws.receive_bytes()
                if is_init_segment(data):
                    self.init_segment = data
                    self.mime = guess_mime(data)
                    await self._send_stream_start_to_all()
                elif self.init_segment is not None:
                    await self._broadcast_bytes(data)
        finally:
            self.agent_streaming = False
            self.init_segment = None
            self.mime = None
            await self._broadcast_json({"type": "ended"})

    # --- сторона зрителя ---

    async def viewer_join(self, ws: WebSocket) -> None:
        self.viewers.add(ws)
        if self._stop_task is not None:
            self._stop_task.cancel()
            self._stop_task = None
        if self.init_segment is not None:
            await self._send_stream_start(ws)
        else:
            await self._safe_send_json(ws, {"type": "waiting"})
        await self._ensure_started()

    async def viewer_leave(self, ws: WebSocket) -> None:
        self.viewers.discard(ws)
        if not self.viewers and self._stop_task is None:
            self._stop_task = asyncio.create_task(self._delayed_stop())

    # --- управление агентом ---

    async def _ensure_started(self) -> None:
        if not self.live_wanted:
            self.live_wanted = True
            for agent_id in self.agent_hub.connected_ids():
                await self.agent_hub.send(agent_id, {"type": "command", "action": "start_live"})

    async def _delayed_stop(self) -> None:
        try:
            await asyncio.sleep(STOP_GRACE_S)
        except asyncio.CancelledError:
            return
        if not self.viewers:
            self.live_wanted = False
            self._stop_task = None
            for agent_id in self.agent_hub.connected_ids():
                await self.agent_hub.send(agent_id, {"type": "command", "action": "stop_live"})

    # --- рассылка ---

    async def _send_stream_start(self, ws: WebSocket) -> None:
        await self._safe_send_json(ws, {"type": "stream", "mime": self.mime})
        try:
            await ws.send_bytes(self.init_segment)
        except Exception:  # noqa: BLE001
            self.viewers.discard(ws)

    async def _send_stream_start_to_all(self) -> None:
        for ws in list(self.viewers):
            await self._send_stream_start(ws)

    async def _broadcast_bytes(self, data: bytes) -> None:
        for ws in list(self.viewers):
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001 — зритель ушёл/завис
                self.viewers.discard(ws)

    async def _broadcast_json(self, message: dict) -> None:
        for ws in list(self.viewers):
            await self._safe_send_json(ws, message)

    async def _safe_send_json(self, ws: WebSocket, message: dict) -> None:
        try:
            await ws.send_json(message)
        except Exception:  # noqa: BLE001
            self.viewers.discard(ws)
