const canvas = document.querySelector('canvas');
const ctx = canvas.getContext('2d');
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
const start = performance.now();
function frame(now) {
  const t = reduced ? 0 : (now - start) / 1000;
  ctx.fillStyle = '#222';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#eee';
  ctx.fillRect(0, 0, Math.floor(t * canvas.width * Number(canvas.dataset.rate)), canvas.height);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
