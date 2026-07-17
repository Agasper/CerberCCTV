// MSE-плеер живой трансляции.
//
// Сервер шлёт по WebSocket: JSON-сообщения о состоянии, затем бинарные
// fMP4-сегменты (первый — init). Мы складываем их в SourceBuffer и
// держимся у «живого края» буфера. При обрыве — переподключаемся.

(function () {
  const video = document.getElementById('live-player');
  const statusEl = document.getElementById('live-status');

  let ws = null;
  let mediaSource = null;
  let sourceBuffer = null;
  let queue = [];
  let closedByUser = false;

  const badgeClass = { ok: 'text-bg-success', warn: 'text-bg-warning', err: 'text-bg-danger' };

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = 'badge ' + (badgeClass[kind] || badgeClass.warn);
  }

  function msClass() {
    return window.ManagedMediaSource || window.MediaSource || null;
  }

  function teardownPlayer() {
    queue = [];
    sourceBuffer = null;
    if (mediaSource) {
      try { URL.revokeObjectURL(video.src); } catch (e) { /* ignore */ }
      mediaSource = null;
    }
    video.removeAttribute('src');
    video.load();
  }

  function initPlayer(mime) {
    teardownPlayer();
    const MS = msClass();
    if (!MS) {
      setStatus('браузер не поддерживает MSE', 'err');
      return;
    }
    if (MS.isTypeSupported && !MS.isTypeSupported(mime)) {
      setStatus('кодек не поддерживается: ' + mime, 'err');
      return;
    }
    mediaSource = new MS();
    mediaSource.addEventListener('sourceopen', () => {
      if (sourceBuffer) return;
      sourceBuffer = mediaSource.addSourceBuffer(mime);
      sourceBuffer.mode = 'segments';
      sourceBuffer.addEventListener('updateend', pump);
      pump();
    });
    // ManagedMediaSource (iOS) требует disableRemotePlayback
    video.disableRemotePlayback = true;
    video.src = URL.createObjectURL(mediaSource);
    video.play().catch(() => { /* autoplay мог быть заблокирован — muted обычно спасает */ });
    setStatus('в эфире', 'ok');
  }

  function pump() {
    if (!sourceBuffer || sourceBuffer.updating || !queue.length) return;
    try {
      sourceBuffer.appendBuffer(queue.shift());
    } catch (e) {
      // Переполнение буфера — подчистим и попробуем ещё раз
      trim(true);
      return;
    }
    keepLiveEdge();
  }

  function keepLiveEdge() {
    if (!video.buffered.length) return;
    const end = video.buffered.end(video.buffered.length - 1);
    if (video.currentTime < end - 3) {
      video.currentTime = end - 0.5;
    }
  }

  function trim(force) {
    if (!sourceBuffer || sourceBuffer.updating || !video.buffered.length) return;
    const start = video.buffered.start(0);
    const end = video.buffered.end(video.buffered.length - 1);
    if (force || end - start > 60) {
      try { sourceBuffer.remove(start, Math.max(start, end - 30)); } catch (e) { /* ignore */ }
    }
  }

  setInterval(trim, 10000);

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host + '/ws/live');
    ws.binaryType = 'arraybuffer';

    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'waiting') setStatus('запуск камеры…');
        else if (msg.type === 'stream') initPlayer(msg.mime);
        else if (msg.type === 'ended') { setStatus('поток прерван, ждём…'); teardownPlayer(); }
      } else {
        // Нет плеера (браузер без MSE) — не копим фрагменты в памяти
        if (!mediaSource) return;
        queue.push(new Uint8Array(ev.data));
        // Страховка от зависшего SourceBuffer: каждый фрагмент начинается
        // с keyframe, поэтому сброс старых безопасен для декодера
        while (queue.length > 50) queue.shift();
        pump();
      }
    };

    ws.onclose = () => {
      if (closedByUser) return;
      setStatus('переподключение…');
      teardownPlayer();
      setTimeout(connect, 3000);
    };
  }

  window.addEventListener('beforeunload', () => {
    closedByUser = true;
    if (ws) ws.close();
  });

  connect();
})();
