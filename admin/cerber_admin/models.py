from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # sha256 от токена; сам токен показывается один раз при выпуске
    token_hash: Mapped[str] = mapped_column(String(64))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Последний heartbeat-статус как есть (HeartbeatStatus из common)
    status: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    # id, назначенный агентом: повторная регистрация после обрыва — идемпотентна
    client_event_id: Mapped[str] = mapped_column(String(36), unique=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[float] = mapped_column(default=0)
    motion_score: Mapped[float] = mapped_column(default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)

    # uploading -> stored | failed
    status: Mapped[str] = mapped_column(String(16), default="uploading", index=True)
    s3_key_video: Mapped[str | None] = mapped_column(Text, nullable=True)
    s3_key_thumb: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Состояние S3 multipart-загрузки — переживает рестарт админки
    upload_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes_received: Mapped[int] = mapped_column(BigInteger, default=0)
    parts: Mapped[list | None] = mapped_column(JSONB, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # всегда 1
    data: Mapped[dict] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, default=1)
