"""Переменные окружения админки.

Обязательные: DATABASE_URL, SECRET_KEY.
Остальные — bootstrap-значения: применяются один раз при пустой БД
(создание первого пользователя, агента, стартового конфига).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Env:
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    secret_key: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", ""))

    # Стартовая учётка админа (создаётся, только если пользователей ещё нет)
    admin_username: str = field(default_factory=lambda: os.environ.get("ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: os.environ.get("ADMIN_PASSWORD", ""))

    # Токен агента: если задан, при старте синхронизируется в БД —
    # удобно для dev-среды и первичной настройки без UI
    agent_token: str = field(default_factory=lambda: os.environ.get("AGENT_TOKEN", ""))
    agent_name: str = field(default_factory=lambda: os.environ.get("AGENT_NAME", "raspberry"))

    # Затравка стартового конфига (только при пустой таблице settings)
    s3_endpoint_url: str = field(default_factory=lambda: os.environ.get("S3_ENDPOINT_URL", ""))
    s3_public_url: str = field(default_factory=lambda: os.environ.get("S3_PUBLIC_URL", ""))
    s3_region: str = field(default_factory=lambda: os.environ.get("S3_REGION", ""))
    s3_bucket: str = field(default_factory=lambda: os.environ.get("S3_BUCKET", ""))
    s3_access_key: str = field(default_factory=lambda: os.environ.get("S3_ACCESS_KEY", ""))
    s3_secret_key: str = field(default_factory=lambda: os.environ.get("S3_SECRET_KEY", ""))
    s3_force_path_style: bool = field(default_factory=lambda: _bool("S3_FORCE_PATH_STYLE"))
    camera_rtsp_main: str = field(default_factory=lambda: os.environ.get("CAMERA_RTSP_MAIN", ""))
    camera_rtsp_sub: str = field(default_factory=lambda: os.environ.get("CAMERA_RTSP_SUB", ""))

    session_max_age_s: int = field(
        default_factory=lambda: int(os.environ.get("SESSION_MAX_AGE_S", str(14 * 24 * 3600)))
    )

    def validate(self) -> None:
        missing = [
            name
            for name, value in (("DATABASE_URL", self.database_url), ("SECRET_KEY", self.secret_key))
            if not value
        ]
        if missing:
            raise RuntimeError(f"Не заданы обязательные переменные окружения: {', '.join(missing)}")


env = Env()
