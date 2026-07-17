// Общие мелочи интерфейса: подтверждения форм и автообновление дашборда.

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
