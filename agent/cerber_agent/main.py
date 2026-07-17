"""Точка входа агента: собирает подсистемы и следит за их жизнью.

Подсистемы:
  CaptureManager — ffmpeg-запись основного потока в кольцевой буфер
  MotionDetector — OpenCV-анализ субпотока (отдельный поток)
  EventRecorder  — машина состояний событий + сборка клипов
  Uploader       — доставка клипов в админку
  ControlClient  — WS-канал: heartbeat, конфиг, команды, снапшоты
  LiveStreamer   — live-трансляция по запросу
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from cerber_common import HeartbeatStatus
from cerber_agent.capture import CaptureManager
from cerber_agent.config import Bootstrap, ConfigStore
from cerber_agent.control import ControlClient, disk_free_mb
from cerber_agent.events import EventRecorder
from cerber_agent.live import LiveStreamer
from cerber_agent.motion import MotionDetector
from cerber_agent.uploader import Uploader

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("cerber_agent")

# RTSP в OpenCV тоже должен ходить по TCP — UDP на Wi-Fi сыпется
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


async def run() -> None:
    bootstrap = Bootstrap.from_env()
    bootstrap.data_dir.mkdir(parents=True, exist_ok=True)
    store = ConfigStore(bootstrap.data_dir / "config.json")

    loop = asyncio.get_running_loop()
    motion_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    capture = CaptureManager(store, bootstrap.data_dir)
    detector = MotionDetector(store, loop, motion_queue)
    recorder = EventRecorder(store, capture, motion_queue, bootstrap.data_dir)
    live = LiveStreamer(bootstrap, store)
    uploader = Uploader(bootstrap, bootstrap.data_dir)

    def build_status() -> HeartbeatStatus:
        outbox_events, outbox_bytes = recorder.outbox_stats()
        last_motion = detector.last_motion_at
        return HeartbeatStatus(
            uptime_s=round(control.uptime_s(), 1),
            capture_ok=capture.ok,
            motion_ok=detector.ok,
            motion_fps=detector.fps,
            buffer_segments=capture.segment_count(),
            outbox_events=outbox_events,
            outbox_bytes=outbox_bytes,
            disk_free_mb=disk_free_mb(bootstrap.data_dir),
            live_active=live.active,
            last_motion_at=(
                datetime.fromtimestamp(last_motion, tz=timezone.utc) if last_motion else None
            ),
            config_version=store.version,
        )

    control = ControlClient(bootstrap, store, live, build_status, detector.latest_jpeg)

    detector.start()
    tasks = [
        asyncio.create_task(capture.run(), name="capture"),
        asyncio.create_task(capture.cleanup_loop(), name="cleanup"),
        asyncio.create_task(recorder.run(), name="events"),
        asyncio.create_task(uploader.run(), name="uploader"),
        asyncio.create_task(control.run(), name="control"),
    ]

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("Агент запущен, админка: %s", bootstrap.admin_url)
    await stop.wait()
    log.info("Останов агента…")
    detector.stop()
    await live.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
