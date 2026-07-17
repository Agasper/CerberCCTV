"""Кольцевой буфер записи: ffmpeg непрерывно пишет основной поток
сегментами по N секунд без перекодирования (stream copy).

Имена сегментов содержат unix-время начала (seg_<epoch>.ts) — по ним
events.py выбирает отрезок для клипа. Старые сегменты удаляются, но
глубина буфера всегда достаточна, чтобы к моменту окончания события
пре-ролл ещё лежал на диске.

Надёжность: ffmpeg перезапускается при падении и при «застывании»
(новые сегменты перестали появляться — камера зависла/сеть пропала).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from pathlib import Path

from cerber_agent.config import ConfigStore

log = logging.getLogger(__name__)

_SEG_RE = re.compile(r"^seg_(\d+)\.ts$")

STALL_TIMEOUT_S = 25
RESTART_BACKOFF_MAX_S = 60


class CaptureManager:
    def __init__(self, store: ConfigStore, data_dir: Path):
        self.store = store
        self.buffer_dir = data_dir / "buffer"
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self._restart_requested = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._started_at = 0.0
        store.subscribe(self._on_config_change)

    def _on_config_change(self, changed: set[str]) -> None:
        if "capture" in changed:
            self._restart_requested.set()

    # --- статус для heartbeat ---

    @property
    def ok(self) -> bool:
        newest = self._newest_segment_mtime()
        return newest is not None and time.time() - newest < STALL_TIMEOUT_S

    def segment_count(self) -> int:
        return sum(1 for p in self.buffer_dir.iterdir() if _SEG_RE.match(p.name))

    # --- выбор сегментов для клипа ---

    def segments_between(self, t0: float, t1: float) -> list[Path]:
        seg_s = self.store.config.buffer.segment_s
        result: list[tuple[int, Path]] = []
        for path in self.buffer_dir.iterdir():
            match = _SEG_RE.match(path.name)
            if not match:
                continue
            start = int(match[1])
            # Сегмент пересекается с интервалом события
            if start + seg_s >= t0 and start <= t1:
                result.append((start, path))
        result.sort()
        return [p for _, p in result]

    # --- основной цикл ---

    async def run(self) -> None:
        backoff = 2
        while True:
            url = self.store.config.camera.rtsp_main
            if not url:
                # Конфига ещё нет — ждём его появления
                self._restart_requested.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._restart_requested.wait(), timeout=10)
                continue

            self._restart_requested.clear()
            started = time.time()
            try:
                await self._run_ffmpeg(url)
            except asyncio.CancelledError:
                await self._terminate()
                raise
            except Exception:  # noqa: BLE001
                log.exception("Захват потока упал")

            # Быстрые падения — растущая пауза; долгая работа сбрасывает её
            backoff = 2 if time.time() - started > 60 else min(backoff * 2, RESTART_BACKOFF_MAX_S)
            if not self._restart_requested.is_set():
                log.info("Перезапуск захвата через %d с", backoff)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._restart_requested.wait(), timeout=backoff)

    async def _run_ffmpeg(self, url: str) -> None:
        seg_s = self.store.config.buffer.segment_s
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-rtsp_transport", "tcp", "-timeout", "10000000",
            "-i", url,
            "-map", "0", "-c", "copy",
            "-f", "segment",
            "-segment_time", str(seg_s),
            "-reset_timestamps", "1",
            "-segment_format", "mpegts",
            "-strftime", "1",
            str(self.buffer_dir / "seg_%s.ts"),
        ]
        log.info("Запуск записи: %s", url.split("@")[-1])  # не светим креды в логах
        self._proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        self._started_at = time.time()
        stderr_task = asyncio.create_task(self._drain_stderr(self._proc))
        try:
            while True:
                if self._proc.returncode is not None:
                    log.warning("ffmpeg записи завершился с кодом %s", self._proc.returncode)
                    return
                if self._restart_requested.is_set():
                    log.info("Перезапуск записи по изменению конфига")
                    return
                newest = self._newest_segment_mtime()
                age_limit = max(STALL_TIMEOUT_S, seg_s * 3)
                started_ago = time.time() - self._started_at
                if started_ago > age_limit and (newest is None or time.time() - newest > age_limit):
                    log.warning("Сегменты перестали появляться — перезапускаю ffmpeg")
                    return
                await asyncio.sleep(3)
        finally:
            await self._terminate()
            stderr_task.cancel()

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(Exception):
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                log.debug("ffmpeg: %s", line.decode(errors="replace").rstrip())

    async def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    def _newest_segment_mtime(self) -> float | None:
        newest = None
        for path in self.buffer_dir.iterdir():
            if _SEG_RE.match(path.name):
                mtime = path.stat().st_mtime
                if newest is None or mtime > newest:
                    newest = mtime
        return newest

    # --- очистка буфера ---

    async def cleanup_loop(self) -> None:
        while True:
            try:
                self._cleanup_once()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка очистки буфера")
            await asyncio.sleep(30)

    def _cleanup_once(self) -> None:
        cfg = self.store.config
        # Буфер должен доживать до конца самого длинного события:
        # пре-ролл + максимальный клип + пост-ролл + запас на сборку
        keep_s = max(
            cfg.buffer.buffer_s,
            cfg.motion.pre_roll_s + cfg.motion.max_clip_s + cfg.motion.post_roll_s + 60,
        )
        cutoff = time.time() - keep_s
        for path in self.buffer_dir.iterdir():
            match = _SEG_RE.match(path.name)
            if match and int(match[1]) < cutoff:
                path.unlink(missing_ok=True)
