// Общая логика интерфейса: локальное время, тултипы, вкладки настроек,
// фильтр событий, подтверждения форм, автообновление дашборда.

// --- Время: сервер отдаёт UTC в <time datetime>, показываем локальное ---
const dtFormat = new Intl.DateTimeFormat(undefined, {
  day: '2-digit', month: '2-digit', year: 'numeric',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});
document.querySelectorAll('time[datetime]').forEach((el) => {
  const d = new Date(el.getAttribute('datetime'));
  if (!isNaN(d)) el.textContent = dtFormat.format(d);
});

// --- Bootstrap-тултипы (вопросики у настроек) ---
document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
  new bootstrap.Tooltip(el);
});

// --- Подтверждение опасных действий ---
document.addEventListener('submit', (ev) => {
  const form = ev.target.closest('form[data-confirm]');
  if (form && !window.confirm(form.dataset.confirm)) {
    ev.preventDefault();
  }
});

// --- Вкладки настроек: открытие по #hash и запись hash при переключении ---
(function () {
  const tabs = document.getElementById('settings-tabs');
  if (!tabs) return;
  const fromHash = location.hash &&
    tabs.querySelector(`[data-bs-target="#pane-${location.hash.slice(1)}"]`);
  if (fromHash) new bootstrap.Tab(fromHash).show();
  tabs.addEventListener('shown.bs.tab', (ev) => {
    const pane = ev.target.getAttribute('data-bs-target') || '';
    history.replaceState(null, '', '#' + pane.replace('#pane-', ''));
    // Canvas зон в скрытой вкладке имеет нулевую ширину — пересчитать
    window.dispatchEvent(new Event('resize'));
  });
})();

// --- Фильтр событий: datetime-local в поясе браузера -> UTC в запросе ---
(function () {
  const form = document.getElementById('events-filter');
  if (!form) return;
  const visFrom = document.getElementById('filter-from');
  const visTo = document.getElementById('filter-to');
  const utcFrom = document.getElementById('filter-from-utc');
  const utcTo = document.getElementById('filter-to-utc');

  function toLocalInput(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
           `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  visFrom.value = toLocalInput(utcFrom.value);
  visTo.value = toLocalInput(utcTo.value);

  form.addEventListener('submit', () => {
    utcFrom.value = visFrom.value ? new Date(visFrom.value).toISOString() : '';
    utcTo.value = visTo.value ? new Date(visTo.value).toISOString() : '';
  });

  form.querySelectorAll('[data-preset]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const now = new Date();
      let from;
      if (btn.dataset.preset === 'today') {
        from = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      } else if (btn.dataset.preset === '24h') {
        from = new Date(now.getTime() - 24 * 3600 * 1000);
      } else {
        from = new Date(now.getTime() - 7 * 24 * 3600 * 1000);
      }
      utcFrom.value = from.toISOString();
      utcTo.value = '';
      visFrom.value = toLocalInput(utcFrom.value);
      visTo.value = '';
      form.submit();
    });
  });
})();

// --- Дашборд: обновление снапшота и статуса ---
if (document.body.dataset.page === 'dashboard') {
  const img = document.getElementById('snapshot');
  if (img) {
    setInterval(() => {
      img.src = '/snapshot.jpg?t=' + Date.now();
    }, 10000);
  }
  setTimeout(() => {
    if (!document.hidden) window.location.reload();
  }, 60000);
}
