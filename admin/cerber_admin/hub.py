"""Хаб control-соединений агентов.

Держит открытые WebSocket'ы агентов, последние снапшоты (в памяти —
инстанс один, а снапшот и так обновляется каждые ~30 с) и умеет
доставлять агенту конфиг и команды.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import WebSocket

log = logging.getLogger(__name__)


class AgentHub:
    def __init__(self) -> None:
        self._conns: dict[int, WebSocket] = {}
        self._snapshots: dict[int, tuple[datetime, bytes]] = {}

    def register(self, agent_id: int, ws: WebSocket) -> None:
        self._conns[agent_id] = ws

    def unregister(self, agent_id: int, ws: WebSocket) -> None:
        if self._conns.get(agent_id) is ws:
            self._conns.pop(agent_id, None)

    def connected_ids(self) -> list[int]:
        return list(self._conns)

    def is_connected(self, agent_id: int) -> bool:
        return agent_id in self._conns

    async def send(self, agent_id: int, message: dict) -> bool:
        ws = self._conns.get(agent_id)
        if ws is None:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception:  # noqa: BLE001 — соединение умерло, агент переподключится
            log.warning("Не удалось отправить сообщение агенту %s", agent_id)
            self._conns.pop(agent_id, None)
            return False

    async def broadcast(self, message: dict) -> None:
        for agent_id in list(self._conns):
            await self.send(agent_id, message)

    async def push_config(self, version: int, agent_config: dict) -> None:
        await self.broadcast({"type": "config", "version": version, "config": agent_config})

    # --- снапшоты ---

    def set_snapshot(self, agent_id: int, jpeg: bytes) -> None:
        self._snapshots[agent_id] = (datetime.now(timezone.utc), jpeg)

    def get_snapshot(self, agent_id: int | None = None) -> tuple[datetime, bytes] | None:
        if agent_id is not None:
            return self._snapshots.get(agent_id)
        # одна камера — берём самый свежий из имеющихся
        if not self._snapshots:
            return None
        return max(self._snapshots.values(), key=lambda item: item[0])
