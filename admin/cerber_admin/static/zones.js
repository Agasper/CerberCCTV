// Редактор зон детекции: рисование полигонов поверх снапшота камеры.
//
// Координаты хранятся нормированными (0..1 от ширины/высоты кадра),
// поэтому не зависят ни от размера canvas, ни от разрешения субпотока.

(function () {
  const canvas = document.getElementById('zones-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');

  let zones = (window.CERBER_ZONES || []).map((poly) => poly.map(([x, y]) => [x, y]));
  let current = [];   // недостроенный полигон
  let imgOk = false;

  const img = new Image();
  img.onload = () => { imgOk = true; resize(); };
  img.onerror = () => resize();
  img.src = '/snapshot.jpg?t=' + Date.now();

  function resize() {
    const w = Math.min(canvas.parentElement.clientWidth - 2, 860);
    const ratio = imgOk ? img.naturalHeight / img.naturalWidth : 9 / 16;
    canvas.width = w;
    canvas.height = Math.round(w * ratio);
    draw();
  }
  window.addEventListener('resize', resize);

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (imgOk) {
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    } else {
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#8b98a5';
      ctx.font = '14px sans-serif';
      ctx.fillText('Снапшота с камеры пока нет — зоны можно рисовать и без него', 16, 28);
    }
    ctx.lineWidth = 2;
    zones.forEach((poly) => drawPoly(poly, true));
    if (current.length) drawPoly(current, false);
  }

  function drawPoly(poly, closed) {
    ctx.beginPath();
    poly.forEach(([x, y], i) => {
      const px = x * canvas.width;
      const py = y * canvas.height;
      i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    });
    if (closed) {
      ctx.closePath();
      ctx.strokeStyle = '#3fb68b';
      ctx.stroke();
      ctx.fillStyle = 'rgba(63, 182, 139, 0.25)';
      ctx.fill();
    } else {
      ctx.strokeStyle = '#d29922';
      ctx.stroke();
      poly.forEach(([x, y]) => {
        ctx.beginPath();
        ctx.arc(x * canvas.width, y * canvas.height, 3.5, 0, 7);
        ctx.fillStyle = '#d29922';
        ctx.fill();
      });
    }
  }

  canvas.addEventListener('click', (e) => {
    const r = canvas.getBoundingClientRect();
    current.push([(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height]);
    draw();
  });

  document.getElementById('zone-finish').addEventListener('click', () => {
    if (current.length >= 3) {
      zones.push(current);
      current = [];
      draw();
    }
  });

  document.getElementById('zone-undo').addEventListener('click', () => {
    if (current.length) current.pop();
    else zones.pop();
    draw();
  });

  document.getElementById('zone-clear').addEventListener('click', () => {
    zones = [];
    current = [];
    draw();
  });

  document.getElementById('zones-form').addEventListener('submit', () => {
    // Недорисованный полигон из 3+ точек считаем законченным
    if (current.length >= 3) {
      zones.push(current);
      current = [];
    }
    document.getElementById('zones-input').value = JSON.stringify(zones);
  });

  resize();
})();
