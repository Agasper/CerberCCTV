// Общие мелочи интерфейса: локальное время, подтверждения форм,
// автообновление дашборда.

// Сервер отдаёт время в UTC внутри <time datetime="ISO">;
// здесь переводим его в часовой пояс браузера.
const dtFormat = new Intl.DateTimeFormat(undefined, {
  day: '2-digit', month: '2-digit', year: 'numeric',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});
document.querySelectorAll('time[datetime]').forEach((el) => {
  const d = new Date(el.getAttribute('datetime'));
  if (!isNaN(d)) el.textContent = dtFormat.format(d);
});

document.addEventListener('submit', (ev) => {
  const form = ev.target.closest('form[data-confirm]');
  if (form && !window.confirm(form.dataset.confirm)) {
    ev.preventDefault();
  }
});

if (document.body.dataset.page === 'dashboard') {
  // Снапшот обновляем каждые 10 с, всю страницу (статус агента) — раз в минуту
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
