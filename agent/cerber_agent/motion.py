"""Детекция движения на субпотоке камеры.

OpenCV блокирующий, поэтому работает в отдельном потоке и передаёт
результаты в asyncio-очередь через call_soon_threadsafe. Кадры
анализируются с ограничением process_fps (остальные только grab()'ятся,
чтобы не копился буфер декодера).

Алгоритм: вычитание фона MOG2 -> порог (отсекаем тени) -> морфология ->
контуры -> суммарная площадь в процентах кадра. Порог площади и
чувствительность настраиваются из админки на лету, без переподключения.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

from cerber_agent.config import ConfigStore

log = logging.getLogger(__name__)

RECONNECT_DELAY_S = 5
SNAPSHOT_ENCODE_INTERVAL_S = 1.0
JPEG_QUALITY = 80


@dataclass
class MotionSample:
    ts: float
    active: bool
    score: float  # площадь движения, % кадра
    jpeg: bytes | None  # кадр с движением (для превью события)


class MotionDetector:
    def __init__(self, store: ConfigStore, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.store = store
        self.loop = loop
        self.queue = queue
        self._thread = threading.Thread(target=self._run, name="motion", daemon=True)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._latest_jpeg_at = 0.0
        self._fps = 0.0
        self._connected = False
        self.last_motion_at: float | None = None

    # --- публичный интерфейс ---

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def ok(self) -> bool:
        return self._connected

    @property
    def fps(self) -> float:
        return self._fps

    def latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._latest_jpeg

    # --- рабочий поток ---

    def _run(self) -> None:
        while not self._stop.is_set():
            url = self.store.config.camera.rtsp_sub or self.store.config.camera.rtsp_main
            if not url:
                time.sleep(3)
                continue
            try:
                self._capture_loop(url)
            except Exception:  # noqa: BLE001
                log.exception("Детектор движения упал, переподключение")
            self._connected = False
            if not self._stop.is_set():
                time.sleep(RECONNECT_DELAY_S)

    def _capture_loop(self, url: str) -> None:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            log.warning("Не удалось открыть субпоток")
            cap.release()
            time.sleep(RECONNECT_DELAY_S)
            return

        log.info("Субпоток открыт: %s", url.split("@")[-1])
        self._connected = True
        sensitivity = self.store.config.motion.sensitivity
        subtractor = self._make_subtractor(sensitivity)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        last_processed = 0.0
        fps_window: list[float] = []
        zone_key: tuple | None = None
        zone_mask: np.ndarray | None = None
        zone_area: int | None = None

        while not self._stop.is_set():
            cfg = self.store.config
            if (cfg.camera.rtsp_sub or cfg.camera.rtsp_main) != url:
                log.info("Адрес субпотока изменился — переподключение")
                break
            if cfg.motion.sensitivity != sensitivity:
                sensitivity = cfg.motion.sensitivity
                subtractor = self._make_subtractor(sensitivity)

            if not cap.grab():
                log.warning("Субпоток оборвался")
                break
            now = time.time()
            if now - last_processed < 1.0 / cfg.motion.process_fps:
                continue
            last_processed = now

            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue

            # Маска зон пересобирается при смене конфига или размера кадра
            key = (repr(cfg.motion.zones), frame.shape[:2])
            if key != zone_key:
                zone_key = key
                zone_mask, zone_area = self._build_zone_mask(cfg.motion.zones, frame.shape)

            score, mask = self._detect(frame, subtractor, kernel, zone_mask, zone_area)
            active = bool(cfg.motion.enabled and score >= cfg.motion.min_area_pct)
            if active:
                self.last_motion_at = now

            jpeg = self._encode_if_needed(frame, now, active)
            self._emit(MotionSample(ts=now, active=active, score=score, jpeg=jpeg))

            fps_window.append(now)
            fps_window[:] = [t for t in fps_window if now - t <= 5]
            self._fps = round(len(fps_window) / 5.0, 1)

        cap.release()
        self._connected = False

    @staticmethod
    def _make_subtractor(sensitivity: int):
        return cv2.createBackgroundSubtractorMOG2(
            history=300, varThreshold=float(sensitivity), detectShadows=True
        )

    @staticmethod
    def _build_zone_mask(
        zones: list, shape: tuple
    ) -> tuple[np.ndarray | None, int | None]:
        """Полигоны (0..1) -> бинарная маска в пикселях кадра + её площадь."""
        if not zones:
            return None, None
        h, w = shape[:2]
        mask = np.zeros((h, w), dtype=np.uint8)
        for poly in zones:
            if len(poly) < 3:
                continue
            pts = np.array([[int(x * w), int(y * h)] for x, y in poly], dtype=np.int32)
            cv2.fillPoly(mask, [pts], 255)
        area = int(np.count_nonzero(mask))
        if area == 0:
            return None, None
        return mask, area

    @staticmethod
    def _detect(
        frame: np.ndarray,
        subtractor,
        kernel,
        zone_mask: np.ndarray | None = None,
        zone_area: int | None = None,
    ) -> tuple[float, np.ndarray]:
        mask = subtractor.apply(frame)
        # Тени у MOG2 = 127, движение = 255: порог 200 отсекает тени
        _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
        if zone_mask is not None:
            # Движение вне зон детекции игнорируется
            mask = cv2.bitwise_and(mask, zone_mask)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.dilate(mask, kernel, iterations=2)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        area = sum(cv2.contourArea(c) for c in contours)
        denom = zone_area if zone_area else frame.shape[0] * frame.shape[1]
        score = area / float(denom) * 100.0
        return round(score, 3), mask

    def _encode_if_needed(self, frame: np.ndarray, now: float, active: bool) -> bytes | None:
        """JPEG нужен раз в секунду для снапшотов и на активных кадрах —
        для превью события. Кодируем не чаще раза в секунду."""
        if now - self._latest_jpeg_at < SNAPSHOT_ENCODE_INTERVAL_S:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            return None
        jpeg = buf.tobytes()
        with self._lock:
            self._latest_jpeg = jpeg
            self._latest_jpeg_at = now
        return jpeg if active else None

    def _emit(self, sample: MotionSample) -> None:
        def put():
            try:
                self.queue.put_nowait(sample)
            except asyncio.QueueFull:
                pass  # обработчик отстал — старые кадры не критичны

        try:
            self.loop.call_soon_threadsafe(put)
        except RuntimeError:
            pass  # цикл уже остановлен
