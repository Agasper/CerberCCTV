"""API-контракт между агентом (Raspberry Pi) и админкой.

Эти модели — единственная точка правды о формате данных, которыми
обмениваются стороны. Агент никогда не видит настройки S3 и ретенции:
ему отдаётся только AgentConfig.

Сообщения control-канала (WebSocket /api/agent/ws), все — JSON с полем "type":
  агент -> админка:
    {"type": "hello", "config_version": int}
    {"type": "heartbeat", "status": HeartbeatStatus}
  админка -> агент:
    {"type": "heartbeat_ack", "config_version": int, "live_wanted": bool}
    {"type": "config", "version": int, "config": AgentConfig}
    {"type": "command", "action": "start_live" | "stop_live"}

Live-канал (WebSocket /api/agent/live) — только бинарные кадры:
первый — init-сегмент fMP4 (ftyp+moov), дальше — медиа-сегменты (moof+mdat).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CameraConfig(BaseModel):
    # Полные RTSP-URL, включая учётку камеры, например
    # rtsp://admin:pass@192.168.0.10:554/Streaming/Channels/101
    rtsp_main: str = ""
    rtsp_sub: str = ""
    snapshot_interval_s: int = Field(default=30, ge=5, le=3600)


class MotionConfig(BaseModel):
    enabled: bool = True
    # Сколько кадров субпотока в секунду реально анализировать
    process_fps: float = Field(default=5.0, ge=0.5, le=15.0)
    # Порог чувствительности вычитания фона (MOG2 varThreshold):
    # меньше — чувствительнее
    sensitivity: int = Field(default=25, ge=4, le=200)
    # Движение засчитывается, если суммарная площадь контуров больше
    # этого процента площади кадра
    min_area_pct: float = Field(default=0.5, ge=0.01, le=50.0)
    # Сколько «двигающихся» кадров подряд нужно для старта события
    min_consecutive_frames: int = Field(default=3, ge=1, le=50)
    pre_roll_s: int = Field(default=5, ge=0, le=60)
    post_roll_s: int = Field(default=5, ge=1, le=120)
    cooldown_s: int = Field(default=10, ge=0, le=600)
    max_clip_s: int = Field(default=120, ge=10, le=900)
    # Зоны детекции: полигоны в нормированных координатах кадра (0..1).
    # Пустой список — анализируется весь кадр. Площадь движения (min_area_pct)
    # считается от площади зон, а не всего кадра.
    zones: list[list[tuple[float, float]]] = Field(default_factory=list)


class BufferConfig(BaseModel):
    segment_s: int = Field(default=2, ge=2, le=10)
    # Минимальная глубина кольцевого буфера; фактическая берётся с запасом
    # под pre_roll + max_clip + post_roll
    buffer_s: int = Field(default=180, ge=60, le=1800)


class LiveConfig(BaseModel):
    # Автостоп трансляции, даже если админка не прислала stop_live
    max_duration_s: int = Field(default=600, ge=30, le=3600)
    fragment_ms: int = Field(default=500, ge=200, le=2000)


class AgentConfig(BaseModel):
    camera: CameraConfig = Field(default_factory=CameraConfig)
    motion: MotionConfig = Field(default_factory=MotionConfig)
    buffer: BufferConfig = Field(default_factory=BufferConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)


class ConfigEnvelope(BaseModel):
    version: int
    config: AgentConfig


class HeartbeatStatus(BaseModel):
    uptime_s: float = 0
    capture_ok: bool = False
    motion_ok: bool = False
    motion_fps: float = 0
    buffer_segments: int = 0
    outbox_events: int = 0
    outbox_bytes: int = 0
    disk_free_mb: int = 0
    live_active: bool = False
    last_motion_at: datetime | None = None
    config_version: int = 0


class EventCreate(BaseModel):
    # UUID, сгенерированный агентом: делает регистрацию идемпотентной,
    # повторный POST после обрыва связи вернёт то же событие и оффсет
    client_event_id: str
    started_at: datetime
    ended_at: datetime
    duration_s: float
    size_bytes: int
    motion_score: float = 0


class EventCreated(BaseModel):
    event_id: str
    # Сколько байт видео админка уже приняла — точка возобновления загрузки
    bytes_received: int = 0
    status: str = "uploading"
