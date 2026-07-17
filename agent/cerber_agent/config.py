"""Конфигурация агента.

Два уровня:
  1. Bootstrap — минимум для выхода на связь (URL админки, токен),
     задаётся через переменные окружения и не меняется на лету.
  2. AgentConfig — всё остальное; приезжает из админки и кэшируется
     в JSON-файл, чтобы агент работал сразу после перезагрузки,
     даже когда админка недоступна.

Компоненты подписываются на изменения: ConfigStore сообщает, какие
подсистемы затронуты («capture», «motion»), чтобы перезапускались
только они.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from cerber_common import AgentConfig

log = logging.getLogger(__name__)


@dataclass
class Bootstrap:
    admin_url: str
    agent_token: str
    data_dir: Path
    outbox_max_mb: int = 2048

    @classmethod
    def from_env(cls) -> "Bootstrap":
        admin_url = os.environ.get("ADMIN_URL", "").rstrip("/")
        token = os.environ.get("AGENT_TOKEN", "")
        if not admin_url or not token:
            raise RuntimeError("Задайте переменные окружения ADMIN_URL и AGENT_TOKEN")
        return cls(
            admin_url=admin_url,
            agent_token=token,
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            outbox_max_mb=int(os.environ.get("OUTBOX_MAX_MB", "2048")),
        )

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.admin_url.startswith("https") else "ws"
        return scheme + ":" + self.admin_url.split(":", 1)[1]

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.agent_token}"}


class ConfigStore:
    def __init__(self, path: Path):
        self._path = path
        self._config = AgentConfig()
        self._version = 0
        self._listeners: list[Callable[[set[str]], None]] = []
        self._load()

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def version(self) -> int:
        return self._version

    def subscribe(self, listener: Callable[[set[str]], None]) -> None:
        self._listeners.append(listener)

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
            self._config = AgentConfig.model_validate(raw["config"])
            self._version = int(raw["version"])
            log.info("Загружен кэш конфига, версия %d", self._version)
        except FileNotFoundError:
            log.info("Кэша конфига нет — работаем с дефолтами до связи с админкой")
        except Exception:  # noqa: BLE001 — битый кэш не должен мешать старту
            log.exception("Кэш конфига повреждён, использую дефолты")

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"version": self._version, "config": self._config.model_dump(mode="json")},
                ensure_ascii=False,
                indent=2,
            )
        )
        tmp.replace(self._path)

    def apply(self, version: int, config_data: dict) -> None:
        """Применить конфиг из админки; уведомить затронутые подсистемы."""
        new = AgentConfig.model_validate(config_data)
        old = self._config
        changed: set[str] = set()
        if (
            new.camera.rtsp_main != old.camera.rtsp_main
            or new.buffer != old.buffer
        ):
            changed.add("capture")
        if new.camera.rtsp_sub != old.camera.rtsp_sub:
            changed.add("motion")

        self._config = new
        self._version = version
        self._save()
        log.info("Применён конфиг версии %d (перезапуск: %s)", version, ", ".join(changed) or "не нужен")
        for listener in self._listeners:
            try:
                listener(changed)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработчика изменения конфига")
