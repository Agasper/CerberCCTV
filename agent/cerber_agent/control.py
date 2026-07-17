"""Канал управления: постоянный WebSocket к админке.

Через него агент шлёт heartbeat раз в 10 секунд, а получает конфиг
и команды start_live/stop_live. Ответ на heartbeat содержит актуальную
версию конфига и флаг live_wanted — даже если разовая команда потерялась
(обрыв связи), состояние выравнивается на следующем heartbeat.

Отдельно, обычным HTTP POST, уходят снапшоты — кадр субпотока раз в
snapshot_interval_s. Если WS долго не поднимается, конфиг запрашивается
резервным HTTP-запросом.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Callable

import aiohttp

from cerber_common import HeartbeatStatus
from cerber_agent.config import Bootstrap, ConfigStore
from cerber_agent.live import LiveStreamer

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 10
RECONNECT_DELAY_S = 5
HTTP_CONFIG_FALLBACK_S = 60


class ControlClient:
    def __init__(
        self,
        bootstrap: Bootstrap,
        store: ConfigStore,
        live: LiveStreamer,
        status_fn: Callable[[], HeartbeatStatus],
        snapshot_fn: Callable[[], bytes | None],
    ):
        self.bootstrap = bootstrap
        self.store = store
        self.live = live
        self.status_fn = status_fn
        self.snapshot_fn = snapshot_fn
        self._started_at = time.time()

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=60)
        last_http_fallback = 0.0
        async with aiohttp.ClientSession(
            timeout=timeout, headers=self.bootstrap.auth_headers
        ) as session:
            asyncio.create_task(self._snapshot_loop(session))
            while True:
                try:
                    await self._ws_session(session)
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    log.warning("Control-канал недоступен: %s", exc)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка control-канала")

                # WS не живёт — хотя бы конфиг таскаем по HTTP
                if time.time() - last_http_fallback > HTTP_CONFIG_FALLBACK_S:
                    last_http_fallback = time.time()
                    await self._fetch_config_http(session)
                await asyncio.sleep(RECONNECT_DELAY_S)

    # --- WebSocket-сессия ---

    async def _ws_session(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(
            f"{self.bootstrap.ws_url}/api/agent/ws", heartbeat=20
        ) as ws:
            log.info("Control-канал установлен")
            await ws.send_json({"type": "hello", "config_version": self.store.version})
            sender = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_message(msg.json(), session)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            finally:
                sender.cancel()
            log.info("Control-канал закрыт")

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            status = self.status_fn()
            await ws.send_json(
                {"type": "heartbeat", "status": status.model_dump(mode="json")}
            )
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    async def _handle_message(self, msg: dict, session: aiohttp.ClientSession) -> None:
        msg_type = msg.get("type")
        if msg_type == "config":
            self.store.apply(int(msg["version"]), msg["config"])
        elif msg_type == "command":
            action = msg.get("action")
            if action == "start_live":
                self.live.start()
            elif action == "stop_live":
                await self.live.stop()
        elif msg_type == "heartbeat_ack":
            if int(msg.get("config_version", 0)) != self.store.version:
                await self._fetch_config_http(session)
            # Сверка желаемого состояния live с фактическим
            wanted = bool(msg.get("live_wanted"))
            if wanted and not self.live.active:
                self.live.start()
            elif not wanted and self.live.active:
                await self.live.stop()

    # --- вспомогательное ---

    async def _fetch_config_http(self, session: aiohttp.ClientSession) -> None:
        try:
            async with session.get(f"{self.bootstrap.admin_url}/api/agent/config") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if int(data["version"]) != self.store.version:
                        self.store.apply(int(data["version"]), data["config"])
                else:
                    log.warning("Не удалось получить конфиг: HTTP %d", resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("Не удалось получить конфиг: %s", exc)

    async def _snapshot_loop(self, session: aiohttp.ClientSession) -> None:
        while True:
            interval = self.store.config.camera.snapshot_interval_s
            await asyncio.sleep(interval)
            jpeg = self.snapshot_fn()
            if not jpeg:
                continue
            try:
                async with session.post(
                    f"{self.bootstrap.admin_url}/api/agent/snapshot",
                    data=jpeg,
                    headers={"Content-Type": "image/jpeg"},
                ) as resp:
                    if resp.status >= 400:
                        log.debug("Снапшот не принят: HTTP %d", resp.status)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                pass  # не страшно, следующий снапшот через interval

    def uptime_s(self) -> float:
        return time.time() - self._started_at


def disk_free_mb(path) -> int:
    try:
        return int(shutil.disk_usage(path).free / 1048576)
    except OSError:
        return 0
