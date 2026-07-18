// Минимальный service worker: нужен для установки PWA.
// Ничего не кэширует — админка живая, устаревшие данные хуже медленных.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {
  // пусто: браузер выполняет обычный сетевой запрос
});
