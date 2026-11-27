/* Meniscus front end. Plain script (no modules, no build step). */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (tag, props = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') n.className = v;
    else if (k === 'style') Object.assign(n.style, v);
    else if (k.startsWith('--')) n.style.setProperty(k, v);
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (k === 'html') n.innerHTML = v;
    else if (v !== undefined && v !== null && v !== false) n.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) n.append(k.nodeType ? k : document.createTextNode(k));
  return n;
};
const icon = (id) => { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('aria-hidden', 'true'); s.innerHTML = `<use href="#${id}"/>`; return s; };
const api = {
  get: (p) => fetch(p, { cache: 'no-store' }).then((r) => r.json()),
  post: (p, b) => fetch(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b || {}) }).then((r) => r.json()),
};
const store = {
  get(k, d) { try { const v = localStorage.getItem('meniscus.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('meniscus.' + k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const URL_RE = /https?:\/\/[^\s<>"'`]+/gi;
const findUrls = (t) => [...new Set((String(t || '').match(URL_RE) || []).map((u) => u.replace(/[.,;)\]}]+$/, '')))];
const hue = (s) => { let h = 7; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h % 360; };
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const fmtBytes = (n) => { if (!n) return '0 B'; const u = ['B', 'KB', 'MB', 'GB', 'TB']; const i = Math.min(4, Math.floor(Math.log(n) / Math.log(1024))); return (n / 1024 ** i).toFixed(i > 1 ? 1 : 0) + ' ' + u[i]; };
const fmtDur = (s) => { if (!s) return ''; s = Math.round(s); const m = Math.floor(s / 60), h = Math.floor(m / 60); return h ? `${h}:${String(m % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` : `${m}:${String(s % 60).padStart(2, '0')}`; };
const ago = (t) => { const d = Date.now() / 1000 - t; if (d < 60) return 'just now'; if (d < 3600) return Math.floor(d / 60) + 'm ago'; if (d < 86400) return Math.floor(d / 3600) + 'h ago'; return Math.floor(d / 86400) + 'd ago'; };
const shortUrl = (u) => u.replace(/^https?:\/\/(www\.)?/, '');
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ------------------------------------------------------------- state ---- */
const S = {
  view: 'reservoir',
  text: '',          // free search typed in the omnibar
  kind: '',          // lens chip (is:video, ...)
  filters: [],       // tokens added by clicking sites / creators / tags
  favs: false,
  sort: store.get('sort', 'new'),
  items: [], total: 0, offset: 0, loading: false, done: false, token: 0,
  jobs: new Map(),
  settings: {}, base: '', versions: {}, gdlConfig: '', presets: [], exportDir: '',
  mode: 'auto',
  vIdx: -1,
  pendingNew: 0,
};
const query = () => [S.text, S.kind, ...S.filters, S.favs ? 'is:fav' : ''].filter(Boolean).join(' ');

/* ----------------------------------------------------------- ambience ---- */
/* The room takes its colour from the media in front of you. */
function hexToHsl(hex) {
  const m = /^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(hex || '');
  if (!m) return null;
  let [r, g, b] = m.slice(1).map((x) => parseInt(x, 16) / 255);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
  let h = 0, s = 0;
  if (mx !== mn) {
    const d = mx - mn; s = l > .5 ? d / (2 - mx - mn) : d / (mx + mn);
    h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4; h *= 60;
  }
  return [h, s, l];
}
const glow = (h, s = .75, l = .55) => `hsl(${Math.round(h)} ${Math.round(Math.max(.5, s) * 100)}% ${Math.round(Math.min(.62, Math.max(.42, l)) * 100)}%)`;
function setAmbient(hues) {
  const root = document.documentElement.style;
  hues.slice(0, 4).forEach(([h, s, l], i) => root.setProperty(`--a${i + 1}`, glow(h, s, l)));
}
function ambientFromItems(items) {
  const hs = items.map((i) => hexToHsl(i.color)).filter((x) => x && x[1] > .18);
  if (hs.length < 2) return;
  // choose four hues that are far apart, weighted toward saturated ones
  hs.sort((a, b) => b[1] - a[1]);
  const pick = [hs[0]];
  while (pick.length < 4 && pick.length < hs.length) {
    let best = null, bestD = -1;
    for (const c of hs) {
      const d = Math.min(...pick.map((p) => { const x = Math.abs(p[0] - c[0]); return Math.min(x, 360 - x); }));
      if (d > bestD) { bestD = d; best = c; }
    }
    pick.push(best);
  }
  while (pick.length < 4) pick.push([(pick[0][0] + 90 * pick.length) % 360, .7, .5]);
  setAmbient(pick);
}
function ambientFromColor(hex) {
  const c = hexToHsl(hex);
  if (!c) return;
  const [h, s, l] = c;
  setAmbient([[h, s, l], [h + 28, s, l], [h - 32, s, l * .9], [h + 180, s * .6, l]]);
}
let libraryAmbient = () => {};

/* ------------------------------------------------------ pointer light ---- */
document.addEventListener('pointermove', (e) => {
  const t = e.target.closest?.('.glass, .tile');
  if (!t) return;
  const r = t.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  t.style.setProperty('--mx', x + 'px');
  t.style.setProperty('--my', y + 'px');
  if (t.classList.contains('glass')) t.style.setProperty('--ang', (Math.atan2(y - r.height / 2, x - r.width / 2) * 180 / Math.PI + 90) + 'deg');
}, { passive: true });

/* -------------------------------------------------------- liquid lens ---- */
/* Chromium can feed an SVG filter into backdrop-filter. For each glass surface we paint a
   displacement map from the signed distance to its rounded edge, so the backdrop bends
   only near the rim, like a thick slab of glass, and split it into three slightly different
   scales per colour channel for a faint prismatic fringe. */
const lensOK = /Chrome\/|Chromium|Electron/.test(navigator.userAgent) && !/Firefox/.test(navigator.userAgent);
let lensSeq = 0;
const lensDefs = $('svg.defs defs');
function paintLens(node) {
  const w = Math.round(node.offsetWidth), h = Math.round(node.offsetHeight);
  if (w < 20 || h < 20) return;
  const bezel = +node.dataset.lens || 24;
  const cs = getComputedStyle(node);
  const rad = Math.min(parseFloat(cs.borderTopLeftRadius) || 0, w / 2, h / 2);
  const blur = parseFloat(cs.getPropertyValue('--blur')) || 20;
  const k = 2; // paint at half resolution, the filter scales it back up
  const cw = Math.ceil(w / k), ch = Math.ceil(h / k);
  const cv = document.createElement('canvas'); cv.width = cw; cv.height = ch;
  const ctx = cv.getContext('2d');
  const img = ctx.createImageData(cw, ch);
  const hw = w / 2, hh = h / 2;
  for (let j = 0; j < ch; j++) {
    for (let i = 0; i < cw; i++) {
      const px = i * k + k / 2 - hw, py = j * k + k / 2 - hh;
      const qx = Math.abs(px) - (hw - rad), qy = Math.abs(py) - (hh - rad);
      const ox = Math.max(qx, 0), oy = Math.max(qy, 0);
      const sd = Math.hypot(ox, oy) + Math.min(Math.max(qx, qy), 0) - rad; // <0 inside
      const dist = -sd;
      let dx = 0, dy = 0;
      if (dist >= 0 && dist < bezel) {
        const t = 1 - dist / bezel;
        const mag = 1 - Math.sqrt(1 - t * t); // circular bevel profile: steep at the very edge
        let nx, ny;
        if (qx > 0 && qy > 0) { const L = Math.hypot(ox, oy) || 1; nx = ox / L * Math.sign(px); ny = oy / L * Math.sign(py); }
        else if (qx > qy) { nx = Math.sign(px); ny = 0; } else { nx = 0; ny = Math.sign(py); }
        dx = -nx * mag; dy = -ny * mag;
      }
      const o = (j * cw + i) * 4;
      img.data[o] = 128 + dx * 127; img.data[o + 1] = 128 + dy * 127; img.data[o + 2] = 128; img.data[o + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  const id = node.dataset.lensId || ('lens-' + (++lensSeq));
  node.dataset.lensId = id;
  $('#' + id)?.remove();
  const sc = Math.min(70, bezel * 1.7);
  const NS = 'http://www.w3.org/2000/svg';
  const f = document.createElementNS(NS, 'filter');
  f.id = id;
  for (const [a, v] of Object.entries({ x: 0, y: 0, width: w, height: h, filterUnits: 'userSpaceOnUse', 'color-interpolation-filters': 'sRGB' })) f.setAttribute(a, v);
  f.innerHTML = `
    <feImage href="${cv.toDataURL()}" x="0" y="0" width="${w}" height="${h}" preserveAspectRatio="none" result="map"/>
    <feDisplacementMap in="SourceGraphic" in2="map" scale="${sc}" xChannelSelector="R" yChannelSelector="G" result="d1"/>
    <feColorMatrix in="d1" type="matrix" values="1 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 1 0" result="r"/>
    <feDisplacementMap in="SourceGraphic" in2="map" scale="${sc * .9}" xChannelSelector="R" yChannelSelector="G" result="d2"/>
    <feColorMatrix in="d2" type="matrix" values="0 0 0 0 0  0 1 0 0 0  0 0 0 0 0  0 0 0 1 0" result="g"/>
    <feDisplacementMap in="SourceGraphic" in2="map" scale="${sc * .8}" xChannelSelector="R" yChannelSelector="G" result="d3"/>
    <feColorMatrix in="d3" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 1 0 0  0 0 0 1 0" result="b"/>
    <feBlend in="r" in2="g" mode="screen" result="rg"/>
    <feBlend in="rg" in2="b" mode="screen"/>`;
  lensDefs.append(f);
  node.style.backdropFilter = `blur(${Math.round(blur * .7)}px) saturate(1.8) brightness(1.05) url(#${id})`;
}
const lensRO = lensOK ? new ResizeObserver(debounce((entries) => entries.forEach((e) => paintLens(e.target)), 120)) : null;
function enableLens(node) { if (lensOK && !node.dataset.lensOn) { node.dataset.lensOn = '1'; lensRO.observe(node); } }
$$('.lens').forEach(enableLens);
document.documentElement.classList.toggle('refract', lensOK);

/* ------------------------------------------------------------ masonry ---- */
const stage = $('.stage');
const grid = $('#masonry');
const M = {
  cols: [], heights: [], n: 0,
  width() { return parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--tile')) || 250; },
  build() {
    const n = Math.max(1, Math.floor((grid.clientWidth + 12) / (this.width() + 12)));
    this.n = n; grid.replaceChildren(); this.cols = []; this.heights = [];
    for (let i = 0; i < n; i++) { const c = el('div', { class: 'col' }); grid.append(c); this.cols.push(c); this.heights.push(0); }
  },
  add(node, ratio) {
    let k = 0; for (let i = 1; i < this.n; i++) if (this.heights[i] < this.heights[k] - .01) k = i;
    this.cols[k].append(node); this.heights[k] += 1 / ratio + .06;
  },
};
const ratioOf = (it) => (it.w && it.h ? Math.min(2.4, Math.max(.42, it.w / it.h)) : 1);

function tileFor(it, idx) {
  const site = it.site || 'loose';
  const t = el('button', { class: 'tile' + (it.kind === 'audio' ? ' audio' : ''), 'data-id': it.id, 'data-idx': idx,
    'aria-label': (it.title || it.rel.split('/').pop()).slice(0, 120), '--c': it.color || `hsl(${hue(site)} 40% 30%)` });
  t.style.aspectRatio = ratioOf(it);
  if (it.kind === 'audio') t.append(it.ext.toUpperCase());
  else {
    const img = el('img', { alt: '', loading: 'lazy', decoding: 'async', src: `/thumb/${it.id}` });
    img.onload = () => img.classList.add('ready');
    img.onerror = () => { img.remove(); };
    t.append(img);
  }
  if (it.kind === 'video') t.append(el('span', { class: 'badge-k' }, icon('i-play'), fmtDur(it.duration)));
  if (it.kind === 'gif') t.append(el('span', { class: 'badge-k' }, 'GIF'));
  if (it.fav) t.append(el('span', { class: 'fav-dot' }, icon('i-heart')));
  t.append(el('span', { class: 'meta' }, el('i', { class: 'site-dot', '--h': hue(site) }), el('span', { class: 'who' }, it.creator || site)));
  return t;
}

function renderBatch(items, startIdx) {
  items.forEach((it, i) => M.add(tileFor(it, startIdx + i), ratioOf(it)));
}
function relayout() {
  M.build();
  renderBatch(S.items, 0);
}

async function loadMore() {
  if (S.loading || S.done) return;
  S.loading = true;
  const token = S.token;
  const params = new URLSearchParams({ q: query(), sort: S.sort, offset: S.offset, limit: 120 });
  try {
    const res = await api.get('/api/library?' + params);
    if (token !== S.token) return;
    const start = S.items.length;
    S.items.push(...res.items);
    S.total = res.total;
    S.offset += res.items.length;
    S.done = S.items.length >= res.total || res.items.length === 0;
    renderBatch(res.items, start);
    updateCounts();
    if (start === 0) { libraryAmbient = () => ambientFromItems(S.items.slice(0, 40)); if (S.view === 'reservoir') libraryAmbient(); }
  } finally {
    if (token === S.token) S.loading = false;
  }
}
function reload() {
  S.token++; S.items = []; S.offset = 0; S.done = false; S.loading = false; S.pendingNew = 0;
  $('#pill-new').hidden = true;
  M.build();
  renderFilters();
  return loadMore();
}
function updateCounts() {
  const q = query();
  $('#res-count').textContent = S.total ? `${S.total.toLocaleString()} ${S.total === 1 ? 'piece' : 'pieces'}` : '';
  $('#res-label').textContent = S.favs ? 'Loved' : 'Reservoir';
  $('#empty').hidden = S.total > 0;
  $('#empty h2').textContent = q ? 'Nothing matches that' : 'The reservoir is still';
}
new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) loadMore(); }, { root: stage, rootMargin: '1400px' }).observe($('#sentinel'));
new ResizeObserver(debounce(() => { const n = Math.max(1, Math.floor((grid.clientWidth + 12) / (M.width() + 12))); if (n !== M.n) relayout(); }, 150)).observe(grid);

/* hover a video tile and it starts breathing */
let hoverTimer;
grid.addEventListener('pointerover', (e) => {
  const t = e.target.closest('.tile');
  if (!t || t.querySelector('video')) return;
  const it = S.items[+t.dataset.idx];
  if (!it || it.kind !== 'video') return;
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => {
    if (!t.matches(':hover')) return;
    const v = el('video', { muted: true, loop: true, playsinline: true, autoplay: true, preload: 'auto', src: `/media/${it.id}` });
    v.muted = true;
    t.append(v);
    v.play().catch(() => {});
  }, 280);
});
grid.addEventListener('pointerout', (e) => {
  const t = e.target.closest('.tile');
  if (!t || t.contains(e.relatedTarget)) return;
  clearTimeout(hoverTimer);
  t.querySelector('video')?.remove();
});
grid.addEventListener('click', (e) => {
  const t = e.target.closest('.tile');
  if (t) openViewer(+t.dataset.idx, t);
});

/* ------------------------------------------------------------ filters ---- */
function renderFilters() {
  const box = $('#active-filters');
  box.replaceChildren(...S.filters.map((f) => el('button', { class: 'chip', title: 'Remove filter', onclick: () => { S.filters = S.filters.filter((x) => x !== f); reload(); } },
    f.replace(/^site:/, '◉ ').replace(/^by:/, '@').replace(/^tag:/, '#').replace(/"/g, ''), icon('i-x'))));
  $$('#lenses .chip[data-q]').forEach((c) => c.setAttribute('aria-pressed', String(c.dataset.q === S.kind)));
  $('#btn-favs').setAttribute('aria-pressed', String(S.favs));
}
function addFilter(tok) {
  if (!S.filters.includes(tok)) S.filters.push(tok);
  if ($('#viewer').open) $('#viewer').close();
  setView('reservoir');
  reload();
  stage.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' });
}
const quote = (v) => (/\s/.test(v) ? `"${v}"` : v);
$('#lenses').addEventListener('click', (e) => {
  const c = e.target.closest('.chip[data-q]');
  if (!c) return;
  S.kind = c.dataset.q; reload();
});
$('#sort').value = S.sort;
$('#sort').addEventListener('change', (e) => { S.sort = e.target.value; store.set('sort', S.sort); reload(); });
const density = $('#density');
density.value = store.get('tile', 250);
document.documentElement.style.setProperty('--tile', density.value + 'px');
density.addEventListener('input', () => { document.documentElement.style.setProperty('--tile', density.value + 'px'); store.set('tile', +density.value); });
density.addEventListener('change', relayout);

/* --------------------------------------------------------------- views ---- */
function setView(v) {
  if (S.view === v) return;
  const swap = () => {
    S.view = v;
    $('#reservoir').hidden = v !== 'reservoir';
    $('#constellation').hidden = v !== 'constellation';
    $$('.rail-btn[data-view]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === v)));
    $('#lenses').style.visibility = v === 'reservoir' ? '' : 'hidden';
    if (v === 'constellation') renderSky(); else libraryAmbient();
  };
  if (document.startViewTransition && !reduced) document.startViewTransition(swap).finished.finally(() => $('#' + v)?.focus({ preventScroll: true }));
  else { swap(); $('#' + v)?.focus({ preventScroll: true }); }
}
$$('.rail-btn[data-view]').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));
$('#btn-favs').addEventListener('click', () => { S.favs = !S.favs; setView('reservoir'); reload(); });
function toggleCurrents(force) {
  const show = force ?? document.body.classList.contains('no-currents');
  document.body.classList.toggle('no-currents', !show);
  $('#btn-currents').setAttribute('aria-pressed', String(show));
  store.set('currents', show);
}
$('#btn-currents').addEventListener('click', () => toggleCurrents());
if (store.get('currents', true) === false) toggleCurrents(false);

/* ----------------------------------------------------- the meniscus bar ---- */
const omni = $('#omni'), input = $('#omni-input'), hint = $('#omni-hint');
const absorbBtn = $('#absorb-btn'), modeChip = $('#mode-chip');
const classifyHint = debounce(async (urls) => {
  const res = await api.post('/api/classify', { urls: urls.slice(0, 8) }).catch(() => []);
  if (findUrls(input.value).length !== urls.length) return;
  const sites = [...new Set(res.map((r) => r.site))];
  hint.replaceChildren(...sites.slice(0, 3).map((s) => el('span', { class: 'tag' }, s)), urls.length > 1 ? el('span', {}, `${urls.length} links`) : '');
}, 180);
const searchLater = debounce(() => { S.text = input.value.trim(); reload(); }, 220);
input.addEventListener('input', () => {
  const urls = findUrls(input.value);
  const has = urls.length > 0;
  omni.classList.toggle('has-urls', has);
  absorbBtn.hidden = modeChip.hidden = !has;
  $('#omni-glyph').setAttribute('href', has ? '#i-drop' : '#i-search');
  if (has) { classifyHint(urls); if (S.text) { S.text = ''; reload(); } }
  else { hint.replaceChildren(); searchLater(); }
});
modeChip.addEventListener('click', () => {
  S.mode = S.mode === 'auto' ? 'video' : 'auto';
  modeChip.textContent = S.mode === 'auto' ? 'auto' : 'yt-dlp';
  modeChip.classList.toggle('video', S.mode === 'video');
});
omni.addEventListener('submit', (e) => {
  e.preventDefault();
  const urls = findUrls(input.value);
  if (urls.length) {
    absorb(urls, omni);
    input.value = ''; input.dispatchEvent(new Event('input'));
  } else { S.text = input.value.trim(); reload(); }
});

async function absorb(urls, fromNode, mode = S.mode) {
  if (!urls.length) return;
  if (document.body.classList.contains('no-currents')) toggleCurrents(true);
  flyDrop(fromNode);
  await api.post('/api/jobs', { urls, mode });
}
function flyDrop(fromNode) {
  if (reduced || !fromNode) return;
  const a = fromNode.getBoundingClientRect(), b = $('#currents').getBoundingClientRect();
  const sx = a.left + a.width * .7, sy = a.top + a.height / 2;
  const tx = b.left + b.width / 2, ty = b.top + 90;
  const d = el('div', { class: 'flying', style: { left: sx + 'px', top: sy + 'px' } });
  document.body.append(d);
  d.animate([
    { transform: 'translate(0,0) rotate(-45deg) scale(.4)', opacity: 0 },
    { transform: `translate(${(tx - sx) * .5}px, ${Math.min(ty - sy, 0) - 90}px) rotate(-45deg) scale(1.15)`, opacity: 1, offset: .45 },
    { transform: `translate(${tx - sx}px, ${ty - sy}px) rotate(-45deg) scale(.25)`, opacity: .2 },
  ], { duration: 850, easing: 'cubic-bezier(.45,0,.55,1)' }).onfinish = () => d.remove();
  setTimeout(() => d.remove(), 1200);
}

/* paste anywhere, or drop a link on the window */
document.addEventListener('paste', (e) => {
  if (e.target.closest('input, textarea, [contenteditable]')) return;
  const urls = findUrls(e.clipboardData.getData('text'));
  if (urls.length) { e.preventDefault(); absorb(urls, omni); }
});
let dragDepth = 0;
const dragHasLink = (e) => [...(e.dataTransfer?.types || [])].some((t) => t === 'text/uri-list' || t === 'text/plain');
window.addEventListener('dragenter', (e) => { if (!dragHasLink(e)) return; dragDepth++; document.body.classList.add('dragging'); });
window.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove('dragging'); } });
window.addEventListener('dragover', (e) => { if (dragHasLink(e)) e.preventDefault(); });
window.addEventListener('drop', (e) => {
  dragDepth = 0; document.body.classList.remove('dragging');
  const urls = findUrls(e.dataTransfer.getData('text/uri-list') + '\n' + e.dataTransfer.getData('text/plain'));
  if (urls.length) { e.preventDefault(); absorb(urls, omni); }
});

/* ------------------------------------------------------------ currents ---- */
const vials = $('#vials');
const WAVE = '<path d="M0 7 Q 12.5 0 25 7 T 50 7 T 75 7 T 100 7 T 125 7 T 150 7 T 175 7 T 200 7 V14 H0Z" fill="currentColor"/>';
const statusWord = { queued: 'waiting', running: 'flowing', done: 'settled', error: 'blocked', cancelled: 'drained' };
function levelOf(j) {
  if (j.status === 'running') return j.progress != null ? Math.max(.06, j.progress) : .14 + .74 * (1 - 1 / (1 + (j.files + j.skipped) / 6));
  return { queued: .04, done: 1, error: 1, cancelled: 0 }[j.status] ?? 0;
}
function upsertJob(j) {
  S.jobs.set(j.id, j);
  let li = vials.querySelector(`[data-job="${j.id}"]`);
  if (!li) {
    li = el('li', { class: 'vial', 'data-job': j.id, '--h': hue(j.site) });
    li.innerHTML = `<div class="liquid"><svg class="wave" viewBox="0 0 200 14" preserveAspectRatio="none">${WAVE}</svg><svg class="wave w2" viewBox="0 0 200 14" preserveAspectRatio="none">${WAVE}</svg></div>`;
    li.append(
      el('div', { class: 'vial-row' }, el('span', { class: 'vial-site' }, el('i', { class: 'site-dot', '--h': hue(j.site) }), j.site), el('span', { class: 'vial-status' })),
      el('div', { class: 'vial-url', title: j.url }, shortUrl(j.url)),
      el('div', { class: 'vial-foot' }, el('span', { class: 'n' })),
      el('div', { class: 'vial-err', hidden: true }),
      el('div', { class: 'vial-thumbs' }),
    );
    const older = [...vials.children].find((c) => (S.jobs.get(c.dataset.job)?.created ?? 0) < j.created);
    vials.insertBefore(li, older || null);
  }
  const indet = j.status === 'running' && j.progress == null;
  li.className = `vial ${j.status}${indet ? ' indet' : ''}`;
  li.style.setProperty('--level', levelOf(j).toFixed(3));
  const pct = j.status === 'running' && j.progress != null ? ` ${Math.round(j.progress * 100)}%` : '';
  li.querySelector('.vial-status').textContent = statusWord[j.status] + pct + (j.status === 'done' && j.finished ? ' · ' + ago(j.finished) : '');
  const bits = [];
  if (j.files) bits.push(`${j.files} new`);
  if (j.skipped) bits.push(`${j.skipped} already held`);
  if (j.bytes) bits.push(fmtBytes(j.bytes));
  if (j.status === 'running' && j.speed) bits.push(j.speed);
  if (j.mode === 'video') bits.push('yt-dlp');
  if (j.kind === 'encode') {
    bits.length = 0;
    if (j.status === 'done') bits.push(fmtBytes(j.bytes));
    else if (j.status === 'running') bits.push('encoding');
  }
  li.querySelector('.n').textContent = bits.join(' · ') || (j.status === 'queued' ? 'in line' : j.status === 'running' ? 'reaching…' : '');
  let note = li.querySelector('.vial-note');
  if (j.kind === 'encode' && j.note) {
    if (!note) { note = el('div', { class: 'vial-note' }); li.querySelector('.vial-foot').before(note); }
    note.textContent = j.note;
  }
  const foot = li.querySelector('.vial-foot');
  foot.querySelectorAll('.icon-btn').forEach((b) => b.remove());
  const btn = (ic, title, act) => el('button', { class: 'icon-btn', title, 'aria-label': title, onclick: () => api.post(`/api/jobs/${j.id}/${act}`) }, icon(ic));
  if (j.status === 'running' || j.status === 'queued') foot.append(btn('i-x', 'Cancel', 'cancel'));
  else {
    if (j.kind === 'encode' && j.status === 'done' && j.export) {
      foot.append(exportBtn('i-folder', 'Show in folder', j.export, 'reveal'), exportBtn('i-link', 'Copy file (paste into Discord)', j.export, 'copy'));
    }
    foot.append(btn('i-retry', 'Run again', 'retry'));
    foot.append(btn('i-x', 'Remove from list', 'remove'));
  }
  if (j.kind === 'encode' && S.vIdx >= 0 && S.items[S.vIdx]?.id === j.items?.[0]) refreshExports();
  const err = li.querySelector('.vial-err');
  const showErr = j.error && (j.status === 'error' || (j.status === 'done' && !j.files && !j.skipped));
  err.hidden = !showErr; err.textContent = showErr ? j.error.replace(/^\[[\w.-]+\]\[\w+\]\s*/, '') : '';
  li.querySelector('.vial-login')?.remove();
  if (showErr && j.kind !== 'encode' && NEEDS_LOGIN.test(j.error)) {
    const host = (() => { try { return new URL(j.url).hostname.replace(/^www\./, ''); } catch { return j.site; } })();
    err.after(el('button', { class: 'act vial-login', title: 'Opens a window to sign in. Run the download again afterwards.',
      onclick: () => startLogin(j.url) }, `Sign in to ${host}`));
  }
  const th = li.querySelector('.vial-thumbs');
  const want = (j.items || []).slice(-7);
  if (th.dataset.sig !== want.join(',')) {
    th.dataset.sig = want.join(',');
    th.replaceChildren(...want.map((id) => el('img', { src: `/thumb/${id}`, alt: '', title: 'Open', onerror: (e) => { e.target.style.visibility = 'hidden'; }, onclick: () => openById(id) })));
  }
  updateCurrentsHead();
}
function exportBtn(ic, title, exportId, act) {
  const b = el('button', { class: 'icon-btn', title, 'aria-label': title });
  b.addEventListener('click', async () => {
    const r = await api.post(`/api/exports/${exportId}/${act}`).catch(() => ({ error: 'failed' }));
    if (act === 'copy' && !r.error) { b.classList.add('ok'); b.title = 'Copied: paste into Discord'; setTimeout(() => b.classList.remove('ok'), 1600); }
    if (r.error) b.title = r.error;
    if (act === 'delete') refreshExports();
  });
  b.append(icon(ic));
  return b;
}
function updateCurrentsHead() {
  const all = [...S.jobs.values()];
  const run = all.filter((j) => j.status === 'running').length, wait = all.filter((j) => j.status === 'queued').length;
  const badge = $('#active-count');
  badge.hidden = !(run + wait); badge.textContent = run + wait;
  $('#cur-stats').textContent = run || wait ? [run && `${run} flowing`, wait && `${wait} waiting`].filter(Boolean).join(' · ') : all.length ? `${all.length} settled` : '';
}
$('#clear-done').addEventListener('click', () => api.post('/api/jobs/clear'));
function renderClipStatus() {
  const cs = $('#clip-status');
  cs.classList.toggle('on', !!S.settings.clipboard);
  cs.replaceChildren(el('i'), S.settings.clipboard ? (S.settings.auto_absorb ? 'Clipboard: auto-absorbing recognised links' : 'Watching the clipboard for links') : 'Clipboard watch is off');
}

/* ------------------------------------------------------------ droplets ---- */
function droplet(c) {
  const box = $('#droplets');
  const d = el('div', { class: 'droplet glass', '--h': hue(c.site), role: 'status' });
  const leave = () => { d.classList.add('leaving'); setTimeout(() => d.remove(), 450); };
  d.append(el('i', { class: 'site-dot', '--h': hue(c.site) }),
    el('div', { class: 'txt' }, el('b', {}, c.absorbed ? `Absorbed a ${c.site} link` : c.supported ? `Caught a ${c.site} link` : 'Caught a link (yt-dlp may handle it)'), el('span', {}, shortUrl(c.url))));
  if (!c.absorbed) {
    d.append(el('button', { class: 'absorb', onclick: () => { absorb([c.url], d); leave(); } }, icon('i-drop'), 'Absorb'),
      el('button', { class: 'icon-btn', 'aria-label': 'Dismiss', onclick: leave }, icon('i-x')));
  } else setTimeout(() => flyDrop(d), 200);
  box.append(d);
  while (box.children.length > 4) box.firstElementChild.remove();
  setTimeout(leave, c.absorbed ? 4000 : 14000);
}

/* -------------------------------------------------------------- viewer ---- */
const viewer = $('#viewer'), vStage = $('#v-stage');
let trashArmed = 0;
async function openViewer(idx, fromTile) {
  if (idx < 0 || idx >= S.items.length) return;
  const first = !viewer.open;
  const show = () => {
    if (fromTile) fromTile.style.viewTransitionName = '';
    if (!viewer.open) viewer.showModal();
    renderViewer(idx);
  };
  if (first && fromTile && document.startViewTransition && !reduced && S.items[idx].kind !== 'audio') {
    fromTile.style.viewTransitionName = 'hero';
    document.startViewTransition(show);
  } else show();
}
async function openById(id) {
  let idx = S.items.findIndex((i) => i.id === id);
  if (idx < 0) {
    const it = await api.get(`/api/items/${id}`).catch(() => null);
    if (!it) return;
    S.items.unshift(it); relayout(); idx = 0;
  }
  openViewer(idx);
}
function renderViewer(idx) {
  S.vIdx = idx; trashArmed = 0;
  const it = S.items[idx];
  const site = it.site || 'loose';
  viewer.style.setProperty('--vc', it.color || `hsl(${hue(site)} 50% 40%)`);
  ambientFromColor(it.color);
  vStage.querySelectorAll('video,audio').forEach((m) => m.pause());
  let media;
  if (it.kind === 'video') {
    media = el('video', { src: `/media/${it.id}`, poster: `/thumb/${it.id}`, controls: true, autoplay: true, loop: true, playsinline: true });
    media.style.aspectRatio = ratioOf(it);
  } else if (it.kind === 'audio') {
    media = el('audio', { src: `/media/${it.id}`, controls: true, autoplay: true });
  } else {
    // show the cached thumbnail instantly (it is what the transition morphs), then swap in the original
    media = el('img', { src: `/thumb/${it.id}`, alt: it.title || '' });
    media.style.aspectRatio = ratioOf(it);
    media.style.height = 'min(calc(100dvh - 56px), 100%)';
    const full = new Image();
    full.onload = () => { if (S.vIdx === idx) { media.src = full.src; media.style.height = ''; media.style.aspectRatio = ''; } };
    full.src = `/media/${it.id}`;
  }
  vStage.replaceChildren(media);
  $('#v-site-dot').style.setProperty('--h', hue(site));
  $('#v-site').textContent = site;
  $('#v-site').onclick = () => addFilter('site:' + site);
  $('#v-creator').textContent = it.creator || '—';
  $('#v-creator').onclick = () => it.creator && addFilter('by:' + quote(it.creator.toLowerCase()));
  const name = it.rel.split('/').pop();
  $('#v-title').textContent = it.title || name;
  const facts = [['Type', `${it.kind} · ${it.ext}`], ['Size', fmtBytes(it.size)]];
  if (it.w && it.h) facts.push(['Pixels', `${it.w} × ${it.h}`]);
  if (it.duration) facts.push(['Length', fmtDur(it.duration)]);
  facts.push(['Added', new Date(it.added * 1000).toLocaleString()]);
  facts.push(['Place', `${idx + 1} of ${S.total.toLocaleString()}`]);
  $('#v-facts').replaceChildren(...facts.flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, v)]));
  const tags = (it.tags || '').split(' ').filter(Boolean).slice(0, 80);
  $('#v-tags').replaceChildren(...tags.map((t) => el('button', { class: 'chip', onclick: () => addFilter('tag:' + t) }, '#' + t)));
  const fav = $('#v-fav');
  fav.classList.toggle('on', !!it.fav); fav.querySelector('span').textContent = it.fav ? 'Loved' : 'Love';
  const src = $('#v-source');
  if (it.source && /^https?:/.test(it.source)) { src.href = it.source; src.removeAttribute('aria-disabled'); }
  else { src.removeAttribute('href'); src.setAttribute('aria-disabled', 'true'); }
  $('#v-trash').querySelector('span').textContent = 'Trash';
  $('#v-path').textContent = it.rel;
  const share = $('#v-share');
  share.hidden = !(it.kind === 'video' || it.kind === 'gif');
  $('#v-exports').replaceChildren();
  if (!share.hidden) refreshExports();
  $('#v-prev').hidden = idx === 0;
  $('#v-next').hidden = idx >= S.total - 1;
  if (idx > S.items.length - 6) loadMore();
}
const refreshExports = debounce(async () => {
  const it = S.items[S.vIdx];
  if (!it || $('#v-share').hidden) return;
  const id = it.id;
  const full = await api.get(`/api/items/${id}`).catch(() => null);
  if (!full || S.items[S.vIdx]?.id !== id) return;
  const label = (p) => S.presets.find((x) => x.id === p)?.label || p;
  const jobs = [...S.jobs.values()].filter((j) => j.kind === 'encode' && j.items?.[0] === id).sort((a, b) => b.created - a.created);
  const newestExport = Math.max(0, ...full.exports.map((e) => e.created));
  const rowsOut = [];
  for (const j of jobs) {
    if (j.status === 'running' || j.status === 'queued') {
      const pct = j.status === 'running' && j.progress != null ? ` ${Math.round(j.progress * 100)}%` : '';
      rowsOut.push(el('li', { class: 'busy' }, el('div', { class: 'x' }, el('b', {}, j.status === 'queued' ? 'Waiting to encode…' : 'Encoding' + pct),
        el('small', {}, [label(j.mode), j.note].filter(Boolean).join(' · ')))));
    } else if (j.status === 'error' && (j.finished || 0) > newestExport) {
      rowsOut.push(el('li', { class: 'busy' }, el('div', { class: 'x' }, el('b', { style: { color: 'var(--danger)' } }, 'Could not make a copy'), el('small', { title: j.error }, j.error))));
    }
  }
  for (const e of full.exports) {
    rowsOut.push(el('li', {}, el('div', { class: 'x' }, el('b', { title: e.path }, e.name), el('small', { title: e.note }, `${label(e.preset)} · ${fmtBytes(e.size)}`)),
      exportBtn('i-link', 'Copy file (paste into Discord)', e.id, 'copy'), exportBtn('i-folder', 'Show in folder', e.id, 'reveal'), exportBtn('i-trash', 'Move copy to trash', e.id, 'delete')));
  }
  $('#v-exports').replaceChildren(...rowsOut);
}, 150);
$('#v-encode').addEventListener('click', async () => {
  const it = S.items[S.vIdx]; if (!it) return;
  const preset = $('#v-preset').value;
  store.set('preset', preset);
  const j = await api.post(`/api/items/${it.id}/encode`, { preset });
  if (j && j.id) upsertJob(j);
  refreshExports();
});
function step(d) {
  const n = S.vIdx + d;
  if (n >= 0 && n < S.items.length) renderViewer(n);
}
$('#v-prev').addEventListener('click', () => step(-1));
$('#v-next').addEventListener('click', () => step(1));
viewer.addEventListener('close', () => {
  vStage.querySelectorAll('video,audio').forEach((m) => { m.pause(); m.removeAttribute('src'); m.load(); });
  vStage.replaceChildren();
  S.vIdx = -1;
  if (S.view === 'reservoir') libraryAmbient(); else renderSky(true);
});
$('#v-fav').addEventListener('click', async () => {
  const it = S.items[S.vIdx]; if (!it) return;
  const r = await api.post(`/api/items/${it.id}/fav`);
  it.fav = r.fav;
  renderViewer(S.vIdx);
  const t = grid.querySelector(`.tile[data-id="${it.id}"]`);
  if (t) { t.querySelector('.fav-dot')?.remove(); if (it.fav) t.append(el('span', { class: 'fav-dot' }, icon('i-heart'))); }
});
$('#v-reveal').addEventListener('click', () => S.items[S.vIdx] && api.post(`/api/items/${S.items[S.vIdx].id}/reveal`));
$('#v-open').addEventListener('click', () => S.items[S.vIdx] && api.post(`/api/items/${S.items[S.vIdx].id}/open`));
$('#v-trash').addEventListener('click', async () => {
  const it = S.items[S.vIdx]; if (!it) return;
  if (Date.now() - trashArmed > 3000) { trashArmed = Date.now(); $('#v-trash span').textContent = 'Sure?'; return; }
  const r = await api.post(`/api/items/${it.id}/trash`);
  if (r.error) { $('#v-trash span').textContent = 'Failed'; return; }
  const idx = S.vIdx;
  S.items.splice(idx, 1); S.total--; S.offset--;
  grid.querySelector(`.tile[data-id="${it.id}"]`)?.remove();
  $$('.tile', grid).forEach((t) => { if (+t.dataset.idx > idx) t.dataset.idx = +t.dataset.idx - 1; });
  updateCounts();
  if (!S.items.length) viewer.close(); else renderViewer(Math.min(idx, S.items.length - 1));
});
if (!('closedBy' in HTMLDialogElement.prototype)) {
  for (const d of $$('dialog[closedby="any"]')) {
    d.addEventListener('click', (e) => {
      if (e.target !== d) return;
      const r = d.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) d.close();
    });
  }
}
/* the viewer covers the whole window, so clicks on empty stage space should close it too */
vStage.addEventListener('click', (e) => { if (e.target === vStage) viewer.close(); });

/* -------------------------------------------------------- constellation ---- */
let skyData = null;
async function renderSky(ambientOnly = false) {
  if (!ambientOnly || !skyData) skyData = await api.get('/api/facets');
  const sites = skyData.sites;
  setAmbient(sites.slice(0, 4).map((s) => [hue(s.site), .8, .55]).concat([[200, .7, .5], [280, .7, .5], [330, .7, .5], [160, .7, .5]]).slice(0, 4));
  if (ambientOnly) return;
  $('#con-count').textContent = sites.length ? `${sites.length} sources · ${fmtBytes(skyData.total.bytes)}` : '';
  const sky = $('#sky');
  sky.replaceChildren();
  const W = Math.min(sky.parentElement.clientWidth - 24, 1300);
  sky.style.width = W + 'px';
  if (!sites.length) { sky.style.height = '0px'; $('#tagcloud').replaceChildren(el('p', { class: 'sub' }, 'No sources yet. Absorb something first.')); return; }
  const max = sites[0].n;
  const placed = [];
  const cx = W / 2;
  for (const [i, s] of sites.slice(0, 40).entries()) {
    const r = Math.max(44, Math.min(130, 130 * Math.sqrt(s.n / max)));
    let x = cx, y = r + 30;
    if (placed.length) {
      // walk an outward spiral from the centre until the orb fits without touching
      for (let a = 0, rr = 0; rr < 4000; a += .35, rr += 2.2) {
        x = cx + Math.cos(a) * rr * 1.35; y = placed[0].y + Math.sin(a) * rr * .85;
        if (x - r < 0 || x + r > W || y - r < 10) continue;
        if (placed.every((p) => Math.hypot(p.x - x, p.y - y) > p.r + r + 46)) break;
      }
    } else y = 150;
    placed.push({ x, y, r, s, i });
  }
  const minY = Math.min(...placed.map((p) => p.y - p.r)) - 20;
  let H = 0;
  for (const p of placed) {
    p.y -= minY;
    H = Math.max(H, p.y + p.r + 60);
    const { s } = p;
    const orb = el('button', { class: 'orb', title: `${s.site}: ${s.n} pieces`, '--h': hue(s.site), '--size': p.r * 2 + 'px',
      '--dur': (7 + Math.random() * 6).toFixed(1) + 's', '--delay': (-Math.random() * 8).toFixed(1) + 's', '--in': (p.i * .05) + 's',
      '--fx': (Math.random() * 14 - 7).toFixed(0) + 'px', '--fy': (-6 - Math.random() * 10).toFixed(0) + 'px',
      onclick: () => addFilter('site:' + s.site) });
    orb.style.left = p.x - p.r + 'px'; orb.style.top = p.y - p.r + 'px';
    const body = el('div', { class: 'body' }, ...s.sample.slice(0, p.r > 70 ? 4 : 1).map((id) => el('img', { src: `/thumb/${id}`, alt: '', loading: 'lazy' })));
    const ring = el('div', { class: 'creators' });
    s.creators.slice(0, 6).forEach((c, k, arr) => {
      const ang = (k / arr.length) * Math.PI * 2 - Math.PI / 2, rad = p.r + 26;
      ring.append(el('span', { style: { transform: `translate(-50%,-50%) translate(${Math.cos(ang) * rad}px, ${Math.sin(ang) * rad}px)` } }, c.creator));
    });
    orb.append(body, el('div', { class: 'shine' }), ring, el('div', { class: 'label' }, el('b', {}, s.site), el('small', {}, `${s.n.toLocaleString()} · ${fmtBytes(s.bytes)}`)));
    sky.append(orb);
  }
  sky.style.height = H + 'px';
  const tmax = skyData.tags[0]?.n || 1;
  $('#tagcloud').replaceChildren(...skyData.tags.map((t) => el('button', { class: 'chip', style: { fontSize: (11 + 7 * Math.sqrt(t.n / tmax)).toFixed(1) + 'px' }, onclick: () => addFilter('tag:' + t.tag) }, '#' + t.tag)));
}

/* ------------------------------------------------------------ settings ---- */
const sDlg = $('#settings');
$('#btn-settings').addEventListener('click', () => {
  $('#s-clip').checked = !!S.settings.clipboard;
  $('#s-auto').checked = !!S.settings.auto_absorb;
  $('#s-conc').value = S.settings.concurrency || 2;
  $('#s-encode').value = S.settings.encode || 'original';
  $('#s-export').value = S.settings.export_dir || S.exportDir;
  const v = S.versions || {};
  $('#s-info').innerHTML = '';
  $('#s-info').append(
    el('div', {}, el('b', {}, 'Library  '), S.base),
    el('div', {}, el('b', {}, 'Config   '), S.gdlConfig),
    el('div', {}, el('b', {}, 'gallery-dl '), v['gallery-dl'] || 'missing', '   ', el('b', {}, 'yt-dlp '), v['yt-dlp'] || 'missing'),
    el('div', {}, el('b', {}, 'ffmpeg '), (v.ffmpeg || 'missing').replace(/^ffmpeg version /, '').split(' ')[0]),
  );
  sDlg.showModal();
  requestAnimationFrame(() => paintLens($('.s-body')));
});
const saveSettings = async (patch) => { S.settings = await api.post('/api/settings', patch); renderClipStatus(); };
$('#s-clip').addEventListener('change', (e) => saveSettings({ clipboard: e.target.checked }));
$('#s-auto').addEventListener('change', (e) => saveSettings({ auto_absorb: e.target.checked }));
$('#s-conc').addEventListener('change', (e) => saveSettings({ concurrency: +e.target.value }));
$('#s-encode').addEventListener('change', (e) => saveSettings({ encode: e.target.value }));
$('#s-export').addEventListener('change', (e) => saveSettings({ export_dir: e.target.value }));
$('#s-open-config').addEventListener('click', () => api.post('/api/open-config', { what: 'config' }));
$('#s-open-base').addEventListener('click', () => api.post('/api/open-config', { what: 'base' }));
$('#s-rescan').addEventListener('click', () => api.post('/api/rescan'));

/* --------------------------------------------------------- site logins ---- */
const NEEDS_LOGIN = /\b(401|403)\b|unauthori[sz]ed|forbidden|log ?in|logged in|sign in|cookies|private|authenticat/i;
let loginErr = '';
function renderLogin(st) {
  if (!st || st.error === undefined) return;
  const sites = st.sites || [];
  const bits = [];
  if (st.active) bits.push('Sign-in window open. Log in there, then close it.');
  bits.push(sites.length ? `Saved cookies for ${sites.join(', ')}.` : 'No site logins yet.');
  if (!st.browser) bits.push('Signing in needs Chrome, Edge or Brave.');
  if (loginErr || st.error) bits.push(loginErr || st.error);
  $('#s-login-status').textContent = bits.join(' ');
  $('#s-login-forget').hidden = !sites.length || st.active;
}
async function startLogin(url) {
  const r = await api.post('/api/login', { url });
  loginErr = r.error || '';
  renderLogin(r.error ? await api.get('/api/login') : r);
  if (r.error) alert(r.error);
}
$('#s-login-go').addEventListener('click', () => startLogin($('#s-login-site').value));
$('#s-login-site').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); startLogin(e.target.value); } });
$('#s-login-forget').addEventListener('click', async () => {
  const r = await api.post('/api/login/forget');
  loginErr = r.error || '';
  renderLogin(r.error ? await api.get('/api/login') : r);
});
$('#btn-settings').addEventListener('click', () => { loginErr = ''; api.get('/api/login').then(renderLogin); });

/* ------------------------------------------------------------ keyboard ---- */
document.addEventListener('keydown', (e) => {
  const typing = e.target.closest('input, textarea, select, [contenteditable]');
  if (viewer.open) {
    if (e.key === 'ArrowRight') { step(1); e.preventDefault(); }
    else if (e.key === 'ArrowLeft') { step(-1); e.preventDefault(); }
    else if (e.key.toLowerCase() === 'l' && !typing) $('#v-fav').click();
    return;
  }
  if (typing) { if (e.key === 'Escape') { e.target.blur(); } return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === '/') { e.preventDefault(); input.focus(); input.select(); }
  else if (e.key === '1') setView('reservoir');
  else if (e.key === '2') setView('constellation');
  else if (e.key.toLowerCase() === 'q') toggleCurrents();
  else if (e.key.toLowerCase() === 'f') $('#btn-favs').click();
});

/* ---------------------------------------------------------- live wire ---- */
const onLibraryChange = debounce(() => {
  if (viewer.open) return;
  if (S.view === 'constellation') { renderSky(); return; }
  if (stage.scrollTop < 300 && S.sort === 'new') reload();
  else { S.pendingNew++; const p = $('#pill-new'); p.hidden = false; p.textContent = 'New arrivals ↑'; }
}, 700);
$('#pill-new').addEventListener('click', () => { stage.scrollTo({ top: 0 }); reload(); });

function connect() {
  const es = new EventSource('/api/events');
  es.addEventListener('job', (e) => upsertJob(JSON.parse(e.data)));
  es.addEventListener('job_removed', (e) => { const { id } = JSON.parse(e.data); S.jobs.delete(id); vials.querySelector(`[data-job="${id}"]`)?.remove(); updateCurrentsHead(); });
  es.addEventListener('jobs_cleared', () => { for (const [id, j] of S.jobs) if (!['running', 'queued'].includes(j.status)) { S.jobs.delete(id); vials.querySelector(`[data-job="${id}"]`)?.remove(); } updateCurrentsHead(); });
  es.addEventListener('clip', (e) => droplet(JSON.parse(e.data)));
  es.addEventListener('login', (e) => renderLogin(JSON.parse(e.data)));
  es.addEventListener('export', (e) => { if (S.items[S.vIdx]?.id === JSON.parse(e.data).item) refreshExports(); });
  es.addEventListener('library', onLibraryChange);
  es.addEventListener('settings', (e) => { S.settings = JSON.parse(e.data); renderClipStatus(); });
  es.addEventListener('thumb', (e) => {
    const t = JSON.parse(e.data);
    const it = S.items.find((i) => i.id === t.id);
    if (it) Object.assign(it, { w: t.w, h: t.h, color: t.color, thumb: t.ok });
    const tile = grid.querySelector(`.tile[data-id="${t.id}"]`);
    if (tile && t.ok === 1) {
      tile.style.setProperty('--c', t.color || '');
      if (t.w && t.h) tile.style.aspectRatio = Math.min(2.4, Math.max(.42, t.w / t.h));
      let img = tile.querySelector('img');
      if (!img) { img = el('img', { alt: '', decoding: 'async' }); img.onload = () => img.classList.add('ready'); tile.prepend(img); }
      img.src = `/thumb/${t.id}?v=${Date.now()}`;
    }
    $$(`.vial-thumbs img[src^="/thumb/${t.id}"]`).forEach((im) => { im.style.visibility = ''; im.src = `/thumb/${t.id}?v=${Date.now()}`; });
  });
  es.onerror = () => { /* EventSource reconnects by itself */ };
}

async function boot() {
  const st = await api.get('/api/state');
  S.settings = st.settings; S.base = st.base; S.versions = st.versions; S.gdlConfig = st.gdl_config;
  S.presets = st.presets || []; S.exportDir = st.export_dir || '';
  const opts = () => S.presets.map((p) => el('option', { value: p.id }, p.label));
  $('#v-preset').replaceChildren(...S.presets.map((p) => el('option', { value: p.id, title: p.label }, p.label.replace(/ \(.*\)$/, ''))));
  $('#v-preset').value = store.get('preset', S.settings.encode !== 'original' ? S.settings.encode : 'discord20');
  if (!$('#v-preset').value) $('#v-preset').value = 'discord20';
  $('#s-encode').replaceChildren(el('option', { value: 'original' }, 'No, keep originals only'), ...opts());
  st.jobs.slice().reverse().forEach(upsertJob);
  renderClipStatus();
  connect();
  await reload();
}
boot();
