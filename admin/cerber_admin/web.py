"""Jinja2-шаблоны и фильтры отображения."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def format_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    # Показываем в локальной зоне контейнера (задаётся env TZ)
    return value.astimezone().strftime("%d.%m.%Y %H:%M:%S")


def format_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "—"
    value = float(num_bytes)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ"):
        if value < 1024 or unit == "ГиБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} ГиБ"


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    total = int(seconds)
    minutes, sec = divmod(total, 60)
    return f"{minutes}:{sec:02d}"


templates.env.filters["dt"] = format_dt
templates.env.filters["filesize"] = format_size
templates.env.filters["duration"] = format_duration
