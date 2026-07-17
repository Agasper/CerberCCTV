"""Jinja2-шаблоны и фильтры отображения."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _parse_dt(value: datetime | str | None) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value


def format_dt(value: datetime | str | None) -> Markup | str:
    """Метка времени, которую браузер переводит в свой часовой пояс.

    Сервер не знает, из какой зоны смотрят админку, поэтому отдаём
    <time datetime="ISO">…UTC…</time>, а app.js переписывает текст
    в локальное время зрителя. Текст в UTC — только fallback без JS.
    """
    value = _parse_dt(value)
    if value is None:
        return "—"
    utc = value.astimezone(timezone.utc)
    return Markup(
        f'<time datetime="{escape(utc.isoformat())}">'
        f"{escape(utc.strftime('%d.%m.%Y %H:%M:%S UTC'))}</time>"
    )


def format_dt_plain(value: datetime | str | None) -> str:
    """Просто текст (для <title> и мест, где HTML недопустим)."""
    value = _parse_dt(value)
    if value is None:
        return "—"
    return value.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


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
templates.env.filters["dt_plain"] = format_dt_plain
templates.env.filters["filesize"] = format_size
templates.env.filters["duration"] = format_duration
