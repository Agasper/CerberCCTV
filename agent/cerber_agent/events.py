"""Машина состояний события движения и сборка клипа.

Событие начинается после min_consecutive_frames подряд «двигающихся»
кадров и заканчивается, когда движения нет post_roll_s секунд (или клип
упёрся в max_clip_s). Затем из кольцевого буфера забираются сегменты
[начало - pre_roll; конец] и склеиваются ffmpeg'ом без перекодирования
в mp4 с faststart (чтобы играть в браузере с первого байта).

Готовый клип + превью + метаданные кладутся в outbox отдельным
каталогом; их подхватывает uploader. Сборка идёт в .tmp-каталоге и
атомарно переименовывается — uploader не увидит недописанное событие.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cerber_common import EventCreate
from cerber_agent.capture import CaptureManager
from cerber_agent.config import ConfigStore
from cerber_agent.motion import MotionSample

log = logging.getLogger(__name__)


class EventRecorder:
    def __init__(
        self,
        store: ConfigStore,
        capture: CaptureManager,
        queue: asyncio.Queue,
        data_dir: Path,
    ):
        self.store = store
        self.capture = capture
        self.queue = queue
        self.outbox = data_dir / "outbox"
        self.outbox.mkdir(parents=True, exist_ok=True)

        self._active = False
        self._started_at = 0.0
        self._last_motion = 0.0
        self._consecutive = 0
        self._cooldown_until = 0.0
        self._best_jpeg: bytes | None = None
        self._best_score = 0.0

    async def run(self) -> None:
        while True:
            sample: MotionSample = await self.queue.get()
            try:
                self._handle(sample)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработки кадра движения")

    def _handle(self, sample: MotionSample) -> None:
        cfg = self.store.config.motion
        now = sample.ts

        if not self._active:
            if sample.active and now >= self._cooldown_until:
                self._consecutive += 1
                if sample.jpeg and sample.score >= self._best_score:
                    self._best_jpeg, self._best_score = sample.jpeg, sample.score
                if self._consecutive >= cfg.min_consecutive_frames:
                    self._start_event(now)
            elif not sample.active:
                self._consecutive = 0
                self._best_jpeg, self._best_score = None, 0.0
            return

        # Событие идёт
        if sample.active:
            self._last_motion = now
            self._best_score = max(self._best_score, sample.score)
            if sample.jpeg and sample.score >= self._best_score:
                self._best_jpeg = sample.jpeg
        if (
            now - self._last_motion >= cfg.post_roll_s
            or now - self._started_at >= cfg.max_clip_s
        ):
            self._finish_event(now)

    def _start_event(self, now: float) -> None:
        cfg = self.store.config.motion
        # Начало события — первый кадр серии, а не момент срабатывания порога
        self._started_at = now - self._consecutive / max(self.store.config.motion.process_fps, 0.5)
        self._last_motion = now
        self._active = True
        log.info(
            "Движение: начало события (score=%.2f%%, пре-ролл %d с)",
            self._best_score, cfg.pre_roll_s,
        )

    def _finish_event(self, now: float) -> None:
        cfg = self.store.config.motion
        started, best_jpeg, best_score = self._started_at, self._best_jpeg, self._best_score
        self._active = False
        self._consecutive = 0
        self._best_jpeg, self._best_score = None, 0.0
        self._cooldown_until = now + cfg.cooldown_s

        clip_start = started - cfg.pre_roll_s
        clip_end = now
        log.info("Движение: конец события, длительность %.1f с", clip_end - clip_start)
        asyncio.create_task(self._assemble(clip_start, clip_end, best_jpeg, best_score))

    async def _assemble(
        self, clip_start: float, clip_end: float, thumb: bytes | None, score: float
    ) -> None:
        seg_s = self.store.config.buffer.segment_s
        # Последний сегмент ещё пишется ffmpeg'ом — ждём его закрытия
        await asyncio.sleep(seg_s + 2)

        segments = self.capture.segments_between(clip_start, clip_end)
        if not segments:
            log.warning("Для события не нашлось сегментов — буфер пуст?")
            return

        event_id = str(uuid.uuid4())
        tmp_dir = self.outbox / f"{event_id}.tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        clip_path = tmp_dir / "clip.mp4"
        try:
            await self._concat(segments, clip_path)
            if thumb:
                (tmp_dir / "thumb.jpg").write_bytes(thumb)
            else:
                await self._extract_thumb(clip_path, tmp_dir / "thumb.jpg")

            meta = EventCreate(
                client_event_id=event_id,
                started_at=datetime.fromtimestamp(clip_start, tz=timezone.utc),
                ended_at=datetime.fromtimestamp(clip_end, tz=timezone.utc),
                duration_s=round(clip_end - clip_start, 1),
                size_bytes=clip_path.stat().st_size,
                motion_score=round(score, 2),
            )
            (tmp_dir / "meta.json").write_text(meta.model_dump_json(indent=2))
            tmp_dir.rename(self.outbox / event_id)
            log.info(
                "Клип собран: %s (%.1f МиБ из %d сегментов)",
                event_id, meta.size_bytes / 1048576, len(segments),
            )
        except Exception:
            log.exception("Не удалось собрать клип события")
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _concat(self, segments: list[Path], out_path: Path) -> None:
        list_path = out_path.parent / "list.txt"
        list_path.write_text("".join(f"file '{p}'\n" for p in segments))
        base = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
        ]
        # Три попытки по нисходящей:
        #  1) всё stream copy — камеры с AAC (bitstream-фильтр нужен для переноса
        #     ADTS-заголовков из mpegts в mp4);
        #  2) видео копией, звук перекодировать в AAC — камеры с G.711
        #     (pcm_mulaw/alaw в mp4 не кладётся и браузером не играется);
        #  3) совсем без звука.
        attempts = [
            base + ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out_path)],
            base + ["-c:v", "copy", "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart", str(out_path)],
            base + ["-c:v", "copy", "-an", "-movflags", "+faststart", str(out_path)],
        ]
        last_error = b""
        for cmd in attempts:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                list_path.unlink(missing_ok=True)
                return
            last_error = stderr
        list_path.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg concat не удался: {last_error.decode(errors='replace')[:500]}")

    @staticmethod
    async def _extract_thumb(clip_path: Path, out_path: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(clip_path), "-vf", "thumbnail", "-frames:v", "1",
            str(out_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    # --- статистика для heartbeat ---

    def outbox_stats(self) -> tuple[int, int]:
        count, total = 0, 0
        for event_dir in self.outbox.iterdir():
            if not event_dir.is_dir() or event_dir.name.endswith(".tmp"):
                continue
            count += 1
            total += sum(f.stat().st_size for f in event_dir.iterdir() if f.is_file())
        return count, total
