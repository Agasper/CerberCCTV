"""Конфигурация приложения, хранимая в БД (таблица settings, одна строка).

AppConfig = настройки агента (уезжают на Pi) + настройки S3 и ретенции
(остаются только на сервере). Любое сохранение увеличивает version —
по нему агент понимает, что пора перечитать свою часть.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cerber_common import AgentConfig, CameraConfig
from cerber_admin.env import env
from cerber_admin.models import Setting


class S3Config(BaseModel):
    endpoint_url: str = ""  # пусто = AWS S3
    # Отдельный базовый URL для presigned-ссылок, если из браузера хранилище
    # доступно по другому адресу, чем из админки (актуально для dev с MinIO)
    public_url: str = ""
    region: str = ""
    bucket: str = ""
    access_key: str = ""
    secret_key: str = ""
    force_path_style: bool = False
    prefix: str = "cerber"
    presign_ttl_s: int = Field(default=3600, ge=60, le=7 * 24 * 3600)

    @property
    def configured(self) -> bool:
        return bool(self.bucket and self.access_key and self.secret_key)


class RetentionConfig(BaseModel):
    enabled: bool = True
    days: int = Field(default=14, ge=1, le=3650)


class AppConfig(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    s3: S3Config = Field(default_factory=S3Config)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)


def default_config() -> AppConfig:
    """Стартовый конфиг для пустой БД, с затравкой из переменных окружения."""
    cfg = AppConfig()
    cfg.agent.camera = CameraConfig(
        rtsp_main=env.camera_rtsp_main, rtsp_sub=env.camera_rtsp_sub
    )
    cfg.s3 = S3Config(
        endpoint_url=env.s3_endpoint_url,
        public_url=env.s3_public_url,
        region=env.s3_region,
        bucket=env.s3_bucket,
        access_key=env.s3_access_key,
        secret_key=env.s3_secret_key,
        force_path_style=env.s3_force_path_style,
    )
    return cfg


async def load_config(session: AsyncSession) -> tuple[AppConfig, int]:
    row = (await session.execute(select(Setting).where(Setting.id == 1))).scalar_one_or_none()
    if row is None:
        cfg = default_config()
        session.add(Setting(id=1, data=cfg.model_dump(mode="json"), version=1))
        await session.commit()
        return cfg, 1
    return AppConfig.model_validate(row.data), row.version


async def save_config(session: AsyncSession, cfg: AppConfig) -> int:
    row = (await session.execute(select(Setting).where(Setting.id == 1))).scalar_one_or_none()
    if row is None:
        row = Setting(id=1, data=cfg.model_dump(mode="json"), version=1)
        session.add(row)
    else:
        row.data = cfg.model_dump(mode="json")
        row.version += 1
    await session.commit()
    return row.version
