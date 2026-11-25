'use strict';
/* wm-service frontend: vanilla JS, no build step, no external assets. */

const $ = (s, r = document) => r.querySelector(s);
const qs = (o) => new URLSearchParams(o).toString();

async function handle(r, kind) {
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try {
      const j = await r.json();
      msg = j.error || (j.detail ? JSON.stringify(j.detail) : msg);
    } catch (_) { /* not JSON */ }
    throw new Error(msg);
  }
  return kind === 'json' ? r.json() : r.blob();
}
const api = {
  json: (url, opts) => fetch(url, opts).then((r) => handle(r, 'json')),
  blob: (url, opts) => fetch(url, opts).then((r) => handle(r, 'blob')),
};
const fmtBytes = (n) => (n > 1048576 ? (n / 1048576).toFixed(2) + ' MB' : (n / 1024).toFixed(0) + ' KB');

// ---------------------------------------------------------------- global state
const S = {
  cfg: null,
  tab: 'remove',
  path: null, name: null,       // currently open photo
  origBlob: null, w: 0, h: 0,   // original (EXIF-transposed PNG) + natural size
  hist: [], idx: -1,            // states: {img: Blob, strokes: [...]}
  dirty: false,
  processed: new Set(),
  restorable: new Set(),
  selected: new Set(),
  folderImages: [],
  lastSelected: -1,
  spaceHeld: false,
};
const MAX_IMG_STATES = 10;

// Blob -> object URL cache (revoked when no longer referenced)
const urls = new Map();
function urlFor(blob) {
  if (!urls.has(blob)) urls.set(blob, URL.createObjectURL(blob));
  return urls.get(blob);
}
function revokeUnused() {
  const keep = new Set(S.hist.map((h) => h.img));
  [S.origBlob, wm.base, wm.preview].forEach((b) => b && keep.add(b));
  for (const [b, u] of urls) if (!keep.has(b)) { URL.revokeObjectURL(u); urls.delete(b); }
}

function setMsg(el, text, kind = '') {
  el.textContent = text || '';
  el.className = 'statusbar' + (kind ? ' ' + kind : '');
}

// ---------------------------------------------------------------- viewer (zoom/pan)
class Viewer {
  constructor(vp, stage, onChange) {
    Object.assign(this, { vp, stage, onChange, s: 1, tx: 0, ty: 0, w: 0, h: 0, pan: null, autoFit: true });
    vp.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });
    new ResizeObserver(() => { if (this.autoFit) this.fit(); }).observe(vp);
  }
  setSize(w, h) {
    this.w = w; this.h = h;
    this.stage.style.width = w + 'px';
    this.stage.style.height = h + 'px';
    this.fit();
  }
  fitScale() {
    const r = this.vp.getBoundingClientRect();
    return Math.min(r.width / this.w, r.height / this.h);
  }
  fit() {
    const r = this.vp.getBoundingClientRect();
    if (!this.w || !r.width) return;
    this.s = this.fitScale() * 0.98;
    this.tx = (r.width - this.w * this.s) / 2;
    this.ty = (r.height - this.h * this.s) / 2;
    this.autoFit = true;
    this.apply();
  }
  apply() {
    this.stage.style.transform = `translate(${this.tx}px, ${this.ty}px) scale(${this.s})`;
    if (this.onChange) this.onChange(this);
  }
  toNatural(cx, cy) {
    // pointer -> natural pixel coords (natural / displayed ratio = 1 / s)
    const r = this.vp.getBoundingClientRect();
    return [(cx - r.left - this.tx) / this.s, (cy - r.top - this.ty) / this.s];
  }
  zoomAt(cx, cy, f) {
    if (!this.w) return;
    const r = this.vp.getBoundingClientRect();
    const px = cx - r.left, py = cy - r.top;
    const ns = Math.min(Math.max(this.s * f, this.fitScale() * 0.25), 32);
    const k = ns / this.s;
    this.tx = px - (px - this.tx) * k;
    this.ty = py - (py - this.ty) * k;
    this.s = ns;
    this.autoFit = false;
    this.apply();
  }
  onWheel(e) {
    if (!this.w) return;
    if (this.wheelHook && this.wheelHook(e)) return;
    e.preventDefault();
    const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    this.zoomAt(e.clientX, e.clientY, Math.exp(-dy * 0.0015));
  }
  startPan(e) {
    this.pan = { x: e.clientX, y: e.clientY, tx: this.tx, ty: this.ty };
    this.vp.classList.add('panning');
  }
  movePan(e) {
    if (!this.pan) return false;
    this.tx = this.pan.tx + e.clientX - this.pan.x;
    this.ty = this.pan.ty + e.clientY - this.pan.y;
    this.autoFit = false;
    this.apply();
    return true;
  }
  endPan() {
    if (!this.pan) return false;
    this.pan = null;
    this.vp.classList.remove('panning');
    return true;
  }
}

// ================================================================ TAB 1: REMOVE
const rm = {
  vp: $('#rm-viewport'), stage: $('#rm-stage'), cur: $('#rm-cur'), orig: $('#rm-orig'),
  mask: $('#rm-mask'), cursor: $('#rm-cursor'), divider: $('#rm-divider'), badge: $('#rm-badge'),
  split: $('#rm-split'), busyEl: $('#rm-busy'), msg: $('#rm-msg'),
  brushEl: $('#brush'), brushValEl: $('#brush-val'), dilateEl: $('#dilate'), dilateValEl: $('#dilate-val'),
  brush: 40, eraser: false, altHeld: false, vHeld: false, showOrig: false, splitMode: false,
  drawing: null, busy: false, shownImg: null, lastPointer: null,
};
rm.ctx = rm.mask.getContext('2d');
rm.viewer = new Viewer(rm.vp, rm.stage, () => { updateCursor(); updateCompare(); });
rm.viewer.wheelHook = (e) => {
  if (!(e.ctrlKey || e.metaKey)) return false;
  e.preventDefault();
  setBrush(rm.brush * (e.deltaY < 0 ? 1.12 : 1 / 1.12));
  return true;
};

const curState = () => S.hist[S.idx];
const isErasing = () => rm.eraser || rm.altHeld;

function clampControlValue(el, value) {
  const min = Number(el.min);
  const max = Number(el.max);
  return Math.min(max, Math.max(min, Math.round(value)));
}

function syncNumberInput(input, value) {
  input.value = String(value);
}

function setBrush(v) {
  const value = clampControlValue(rm.brushEl, Number(v));
  rm.brush = value;
  rm.brushEl.value = String(value);
  syncNumberInput(rm.brushValEl, value);
  updateCursor();
}

function setDilate(v) {
  const value = clampControlValue(rm.dilateEl, Number(v));
  rm.dilateEl.value = String(value);
  syncNumberInput(rm.dilateValEl, value);
}

function commitNumberInput(input, slider, setter) {
  const value = Number(input.value);
  if (!Number.isFinite(value)) {
    syncNumberInput(input, slider.value);
    return;
  }
  setter(value);
}

function drawStroke(ctx, st) {
  ctx.save();
  ctx.globalCompositeOperation = st.erase ? 'destination-out' : 'source-over';
  ctx.strokeStyle = ctx.fillStyle = '#ff0000';
  ctx.lineWidth = st.size;
  ctx.lineCap = ctx.lineJoin = 'round';
  const p = st.pts;
  ctx.beginPath();
  ctx.arc(p[0][0], p[0][1], st.size / 2, 0, Math.PI * 2);
  ctx.fill();
  if (p.length > 1) {
    ctx.beginPath();
    ctx.moveTo(p[0][0], p[0][1]);
    for (let i = 1; i < p.length; i++) ctx.lineTo(p[i][0], p[i][1]);
    ctx.stroke();
  }
  ctx.restore();
}
function drawSegment(ctx, st, a, b) {
  drawStroke(ctx, { erase: st.erase, size: st.size, pts: [a, b] });
}
function redrawMask() {
  rm.ctx.clearRect(0, 0, rm.mask.width, rm.mask.height);
  const st = curState();
  if (st) st.strokes.forEach((s) => drawStroke(rm.ctx, s));
}

function pushState(st, { redraw = true } = {}) {
  S.hist = S.hist.slice(0, S.idx + 1);
  S.hist.push(st);
  S.idx = S.hist.length - 1;
  // keep at most MAX_IMG_STATES distinct images in history
  const distinct = () => new Set(S.hist.map((h) => h.img)).size;
  while (distinct() > MAX_IMG_STATES) {
    const first = S.hist[0].img;
    let n = 0;
    while (n < S.hist.length && S.hist[n].img === first) n++;
    S.hist.splice(0, n);
    S.idx -= n;
  }
  revokeUnused();
  renderState(redraw);
}
function renderState(redraw = true) {
  const st = curState();
  if (!st) return;
  if (rm.shownImg !== st.img) {
    rm.cur.src = urlFor(st.img);
    rm.shownImg = st.img;
  }
  if (redraw) redrawMask();
  updateRmButtons();
}
function undo() { if (rm.busy || S.idx <= 0) return; S.idx--; renderState(); }
function redo() { if (rm.busy || S.idx >= S.hist.length - 1) return; S.idx++; renderState(); }

function updateRmButtons() {
  const has = !!S.origBlob;
  $('#undo').disabled = rm.busy || S.idx <= 0;
  $('#redo').disabled = rm.busy || S.idx >= S.hist.length - 1;
  $('#inpaint').disabled = rm.busy || !has;
  $('#send').disabled = rm.busy || !has;
  $('#skip-remove').disabled = rm.busy || !has;
  $('#restore').disabled = !has || !S.restorable.has(S.path);
  $('#clear-mask').disabled = rm.busy || !has || !curState() || !curState().strokes.length;
  const passes = S.hist.slice(0, S.idx + 1).filter((h, i, a) => i === 0 || h.img !== a[i - 1].img).length - 1;
  if (has) {
    $('#file-info').textContent = `${S.path}  —  ${S.w}×${S.h}` + (passes > 0 ? `  —  ${passes} inpaint pass(es)` : '');
  }
}

function updateCursor() {
  const c = rm.cursor;
  const p = rm.lastPointer;
  if (!p || !S.origBlob || rm.viewer.pan || S.spaceHeld || rm.busy) { c.hidden = true; return; }
  const r = rm.vp.getBoundingClientRect();
  const d = Math.max(2, rm.brush * rm.viewer.s);
  c.hidden = false;
  c.style.width = c.style.height = d + 'px';
  c.style.left = (p.x - r.left) + 'px';
  c.style.top = (p.y - r.top) + 'px';
  c.classList.toggle('erase', isErasing());
}

function updateCompare() {
  const full = rm.vHeld || rm.showOrig;
  const has = !!S.origBlob;
  rm.orig.hidden = !has || !(full || rm.splitMode);
  rm.mask.hidden = full;
  rm.badge.hidden = !has || !full;
  rm.split.hidden = !has || !rm.splitMode || full;
  rm.divider.hidden = !has || !rm.splitMode || full;
  if (full) {
    rm.orig.style.clipPath = 'none';
  } else if (rm.splitMode) {
    const pos = +rm.split.value;
    rm.orig.style.clipPath = `inset(0 ${100 - pos}% 0 0)`;
    rm.divider.style.left = pos + '%';
    rm.divider.style.borderLeftWidth = (2 / rm.viewer.s) + 'px';
  }
  $('#show-orig').classList.toggle('on', rm.showOrig);
  $('#split-mode').classList.toggle('on', rm.splitMode);
}

function setRmBusy(b, text) {
  rm.busy = b;
  rm.busyEl.hidden = !b;
  if (text) rm.busyEl.querySelector('span').textContent = text;
  updateRmButtons();
  updateCursor();
}

// --- pointer handling (drawing + panning)
rm.vp.addEventListener('pointerdown', (e) => {
  if (!S.origBlob || rm.busy) return;
  if (e.button === 1 || (e.button === 0 && S.spaceHeld)) {
    e.preventDefault();
    rm.vp.setPointerCapture(e.pointerId);
    rm.viewer.startPan(e);
    updateCursor();
    return;
  }
  if (e.button !== 0 || rm.vHeld || rm.showOrig) return;
  rm.vp.setPointerCapture(e.pointerId);
  const pt = rm.viewer.toNatural(e.clientX, e.clientY);
  rm.drawing = { erase: isErasing(), size: rm.brush, pts: [pt] };
  drawStroke(rm.ctx, rm.drawing);
});
rm.vp.addEventListener('pointermove', (e) => {
  rm.lastPointer = { x: e.clientX, y: e.clientY };
  if (rm.viewer.movePan(e)) return;
  updateCursor();
  if (!rm.drawing) return;
  const pts = rm.drawing.pts;
  const last = pts[pts.length - 1];
  const pt = rm.viewer.toNatural(e.clientX, e.clientY);
  if (Math.hypot(pt[0] - last[0], pt[1] - last[1]) < 0.75) return;
  pts.push(pt);
  drawSegment(rm.ctx, rm.drawing, last, pt);
});
function endPointer() {
  if (rm.viewer.endPan()) { updateCursor(); return; }
  if (!rm.drawing) return;
  const st = curState();
  const stroke = rm.drawing;
  rm.drawing = null;
  pushState({ img: st.img, strokes: [...st.strokes, stroke] }, { redraw: false });
}
rm.vp.addEventListener('pointerup', endPointer);
rm.vp.addEventListener('pointercancel', endPointer);
rm.vp.addEventListener('pointerleave', () => { rm.lastPointer = null; updateCursor(); });
rm.vp.addEventListener('mousedown', (e) => { if (e.button === 1) e.preventDefault(); }); // no autoscroll
rm.vp.addEventListener('contextmenu', (e) => e.preventDefault());

// --- mask export: white = remove, black = keep, natural resolution
function exportMask() {
  const c = document.createElement('canvas');
  c.width = S.w; c.height = S.h;
  const x = c.getContext('2d');
  x.drawImage(rm.mask, 0, 0);
  x.globalCompositeOperation = 'source-in';
  x.fillStyle = '#fff';
  x.fillRect(0, 0, c.width, c.height);
  x.globalCompositeOperation = 'destination-over';
  x.fillStyle = '#000';
  x.fillRect(0, 0, c.width, c.height);
  return new Promise((res, rej) => c.toBlob((b) => (b ? res(b) : rej(new Error('Mask encoding failed'))), 'image/png'));
}

async function inpaint() {
  if (rm.busy || !S.origBlob) return;
  const st = curState();
  if (!st.strokes.some((s) => !s.erase)) {
    setMsg(rm.msg, 'Paint a mask over the old watermark first.', 'error');
    return;
  }
  setRmBusy(true, 'Inpainting…');
  setMsg(rm.msg, 'Inpainting…');
  const t0 = performance.now();
  try {
    const mask = await exportMask();
    const fd = new FormData();
    fd.append('image', st.img, 'image.png');
    fd.append('mask', mask, 'mask.png');
    fd.append('dilate_px', rm.dilateEl.value);
    const out = await api.blob('/api/remove', { method: 'POST', body: fd });
    pushState({ img: out, strokes: [] });
    S.dirty = true;
    setMsg(rm.msg, `Inpainted in ${((performance.now() - t0) / 1000).toFixed(1)} s. ` +
      'Hold V or use Before/After to verify. Paint again for another pass, or Send.', 'ok');
  } catch (e) {
    setMsg(rm.msg, 'Inpaint failed: ' + e.message, 'error');
    checkHealth();
  } finally {
    setRmBusy(false);
  }
}

// --- open a photo
async function openImage(path, name) {
  if (rm.busy) return;
  if (S.dirty && S.path !== path && !confirm('Discard the unsaved edits of the current photo?')) return;
  setTab('remove');
  setRmBusy(true, 'Loading…');
  setMsg(rm.msg, 'Loading ' + name + '…');
  try {
    const blob = await api.blob('/api/image?' + qs({ path }));
    const url = urlFor(blob);
    rm.cur.src = url;
    await rm.cur.decode();
    S.path = path; S.name = name; S.dirty = false;
    S.w = rm.cur.naturalWidth; S.h = rm.cur.naturalHeight;
    S.origBlob = blob;
    S.hist = [{ img: blob, strokes: [] }];
    S.idx = 0;
    rm.shownImg = blob;
    rm.orig.src = url;
    rm.mask.width = S.w; rm.mask.height = S.h;
    rm.viewer.setSize(S.w, S.h);
    wm.base = null; wm.preview = null; wm.img.removeAttribute('src');
    revokeUnused();
    $('#rm-empty').hidden = true;
    renderState();
    updateCompare();
    updateWmUi();
    document.querySelectorAll('.tile').forEach((t) => t.classList.toggle('sel', t.dataset.path === path));
    setMsg(rm.msg, `Opened ${name} (${S.w}×${S.h}). Paint over the old watermark, then Inpaint (Enter).` +
      ' If there is no old watermark, just press Send.');
  } catch (e) {
    setMsg(rm.msg, 'Could not open image: ' + e.message, 'error');
  } finally {
    setRmBusy(false);
  }
}

function sendToWatermark() {
  if (!S.origBlob || rm.busy) return;
  wm.base = curState().img; // in-memory PNG blob, no re-encoding
  wm.preview = null;
  wm.viewer.setSize(S.w, S.h);
  resetWmArm();
  setTab('wm');
  wmRender();
}

function skipRemovalToWatermark() {
  if (!S.origBlob || rm.busy) return;
  wm.base = S.origBlob;
  wm.preview = null;
  wm.viewer.setSize(S.w, S.h);
  resetWmArm();
  setTab('wm');
  wmRender();
}

// --- toolbar wiring
rm.brushEl.addEventListener('input', () => setBrush(rm.brushEl.value));
rm.dilateEl.addEventListener('input', () => setDilate(rm.dilateEl.value));
rm.brushValEl.addEventListener('change', () => commitNumberInput(rm.brushValEl, rm.brushEl, setBrush));
rm.dilateValEl.addEventListener('change', () => commitNumberInput(rm.dilateValEl, rm.dilateEl, setDilate));
for (const [input, slider, setter] of [[rm.brushValEl, rm.brushEl, setBrush], [rm.dilateValEl, rm.dilateEl, setDilate]]) {
  input.addEventListener('blur', () => commitNumberInput(input, slider, setter));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      commitNumberInput(input, slider, setter);
      input.blur();
    }
  });
}
$('#eraser').addEventListener('click', () => { rm.eraser = !rm.eraser; $('#eraser').classList.toggle('on', rm.eraser); updateCursor(); });
$('#clear-mask').addEventListener('click', () => {
  const st = curState();
  if (st && st.strokes.length) pushState({ img: st.img, strokes: [] });
});
$('#inpaint').addEventListener('click', inpaint);
$('#undo').addEventListener('click', undo);
$('#redo').addEventListener('click', redo);
$('#show-orig').addEventListener('click', () => { rm.showOrig = !rm.showOrig; updateCompare(); });
$('#split-mode').addEventListener('click', () => { rm.splitMode = !rm.splitMode; updateCompare(); });
rm.split.addEventListener('input', updateCompare);
$('#rm-fit').addEventListener('click', () => rm.viewer.fit());
$('#send').addEventListener('click', sendToWatermark);
$('#skip-remove').addEventListener('click', skipRemovalToWatermark);
$('#restore').addEventListener('click', async () => {
  if (!S.path || !S.processed.has(S.path)) return;
  if (!confirm(`Restore the original for ${S.name}?`)) return;
  try {
    await api.json('/api/restore', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: S.path})});
    S.processed.delete(S.path);
    S.restorable.delete(S.path);
    await openImage(S.path, S.name);
    setMsg(rm.msg, `Restored ${S.name}.`, 'ok');
  } catch (e) { setMsg(rm.msg, 'Restore failed: ' + e.message, 'error'); }
});

// ================================================================ TAB 2: WATERMARK
const wm = {
  vp: $('#wm-viewport'), stage: $('#wm-stage'), img: $('#wm-img'), msg: $('#wm-msg'), busyEl: $('#wm-busy'),
  logo: $('#wm-logo'), mx: $('#wm-mx'), my: $('#wm-my'), op: $('#wm-op'), ang: $('#wm-ang'),
  logoOn: $('#wm-logo-on'), textOn: $('#wm-text-on'),
  base: null, preview: null, seq: 0, timer: null, busy: false, show: true, armed: false, armTimer: null,
  anchorX: 1, anchorY: 1,
};
wm.viewer = new Viewer(wm.vp, wm.stage);

function wmParams() {
  return {
    logo_width: +wm.logo.value, logo_margin_x: +wm.mx.value, logo_margin_y: +wm.my.value,
    text_opacity: +wm.op.value, text_angle: +wm.ang.value,
    logo_anchor_x: wm.anchorX ?? S.cfg.watermark.logo_anchor_x,
    logo_anchor_y: wm.anchorY ?? S.cfg.watermark.logo_anchor_y,
    logo_enabled: wm.logoOn.checked, text_enabled: wm.textOn.checked,
  };
}
function updateWmLabels() {
  $('#wm-logo-val').textContent = (wm.logo.value * 100).toFixed(1) + '%';
  $('#wm-mx-val').textContent = (wm.mx.value * 100).toFixed(1) + '%';
  $('#wm-my-val').textContent = (wm.my.value * 100).toFixed(1) + '%';
  $('#wm-op-val').textContent = (+wm.op.value).toFixed(2);
  $('#wm-ang-val').textContent = wm.ang.value + '°';
}
function resetWmSliders() {
  const d = S.cfg.watermark;
  wm.logo.value = d.logo_width; wm.mx.value = d.logo_margin_x; wm.my.value = d.logo_margin_y;
  wm.op.value = d.text_opacity; wm.ang.value = d.text_angle;
  wm.anchorX = d.logo_anchor_x; wm.anchorY = d.logo_anchor_y;
  wm.logoOn.checked = d.logo_enabled; wm.textOn.checked = d.text_enabled;
  updateWmLabels();
}
function updateWmUi() {
  const has = !!wm.base;
  $('#wm-empty').hidden = has;
  const ready = has && !!wm.preview && !wm.busy;
  $('#wm-download').disabled = !ready;
  $('#wm-save').disabled = !ready;
  wm.busyEl.hidden = !wm.busy;
  $('#wm-target').textContent = S.path ? `Target: ${S.path}` : '';
  $('#wm-toggle').classList.toggle('on', wm.show);
}
function showWm() {
  const b = wm.show ? wm.preview : wm.base;
  if (b) wm.img.src = urlFor(b); else wm.img.removeAttribute('src');
}

async function wmRender() {
  if (!wm.base) { updateWmUi(); return; }
  const my = ++wm.seq;
  wm.busy = true; updateWmUi();
  const t0 = performance.now();
  try {
    const fd = new FormData();
    fd.append('image', wm.base, 'image.png');
    fd.append('fmt', 'png');
    for (const [k, v] of Object.entries(wmParams())) fd.append(k, v);
    const b = await api.blob('/api/watermark', { method: 'POST', body: fd });
    if (my !== wm.seq) return;
    wm.preview = b;
    revokeUnused();
    showWm();
    setMsg(wm.msg, `Preview rendered at full resolution (${S.w}×${S.h}) in ${((performance.now() - t0) / 1000).toFixed(1)} s. ` +
      'This exact image is what Download / Save encode.');
  } catch (e) {
    if (my === wm.seq) setMsg(wm.msg, 'Watermark failed: ' + e.message, 'error');
  } finally {
    if (my === wm.seq) { wm.busy = false; updateWmUi(); }
  }
}
function wmScheduleRender() {
  updateWmLabels();
  resetWmArm();
  wm.preview = null; updateWmUi();
  clearTimeout(wm.timer);
  wm.timer = setTimeout(wmRender, 250);
}
[wm.logo, wm.mx, wm.my, wm.op, wm.ang, wm.logoOn, wm.textOn].forEach((el) => el.addEventListener('input', wmScheduleRender));
[wm.logoOn, wm.textOn].forEach((el) => el.addEventListener('change', wmScheduleRender));
$('#wm-anchors').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  wm.anchorX = +b.dataset.x; wm.anchorY = +b.dataset.y; wmScheduleRender();
});
$('#wm-reset').addEventListener('click', () => { resetWmSliders(); wmScheduleRender(); });

function yamlValue(v) {
  if (typeof v === 'string') return JSON.stringify(v);
  if (typeof v === 'number' && !Number.isInteger(v)) return String(Math.round(v * 10000) / 10000);
  return String(v);
}
$('#wm-yaml').addEventListener('click', async () => {
  const w = { ...S.cfg.watermark, ...wmParams() };
  const text = 'watermark:\n' + Object.entries(w).map(([k, v]) => `  ${k}: ${yamlValue(v)}`).join('\n') + '\n';
  const ta = $('#yaml-out');
  try {
    await navigator.clipboard.writeText(text);
    setMsg(wm.msg, 'YAML copied to clipboard. Paste it into config.yaml to make it the template.', 'ok');
    ta.hidden = true;
  } catch (_) {
    ta.value = text; ta.hidden = false; ta.select();
    setMsg(wm.msg, 'Clipboard unavailable: copy the YAML from the box (click it again to hide).');
    ta.onclick = () => { ta.hidden = true; };
  }
});

$('#wm-default').addEventListener('click', async () => {
  try {
    const r = await api.json('/api/config/default', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({watermark: wmParams()}) });
    S.cfg.watermark = r.watermark;
    setMsg(wm.msg, 'Default template saved to config.local.yaml.', 'ok');
  } catch (e) { setMsg(wm.msg, 'Could not save default: ' + e.message, 'error'); }
});

$('#wm-fit').addEventListener('click', () => wm.viewer.fit());

wm.vp.addEventListener('pointerdown', (e) => {
  if (!wm.base || e.button !== 0 || S.spaceHeld) return;
  const r = wm.vp.getBoundingClientRect();
  const [x, y] = wm.viewer.toNatural(e.clientX, e.clientY);
  const box = logoBoxPreview();
  if (!box || x < box[0] || y < box[1] || x > box[0] + box[2] || y > box[1] + box[3]) return;
  e.preventDefault(); wm.vp.setPointerCapture(e.pointerId);
  wm.drag = {w: box[2], h: box[3], dx: x - box[0], dy: y - box[1]};
  wm.vp.classList.add('dragging-logo');
});
wm.vp.addEventListener('pointermove', (e) => {
  if (!wm.drag) return;
  const [x, y] = wm.viewer.toNatural(e.clientX, e.clientY);
  const mx = +wm.mx.value * S.w, my = +wm.my.value * S.w;
  const nx = Math.min(S.w - wm.drag.w - mx, Math.max(mx, x - wm.drag.dx));
  const ny = Math.min(S.h - wm.drag.h - my, Math.max(my, y - wm.drag.dy));
  const sx = S.w - wm.drag.w - 2 * mx, sy = S.h - wm.drag.h - 2 * my;
  let ax = sx ? (nx - mx) / sx : 0, ay = sy ? (ny - my) / sy : 0;
  if (e.shiftKey) { ax = Math.round(ax * 2) / 2; ay = Math.round(ay * 2) / 2; }
  wm.anchorX = Math.max(0, Math.min(1, ax)); wm.anchorY = Math.max(0, Math.min(1, ay));
  updateWmLabels();
});
function finishLogoDrag() {
  if (!wm.drag) return;
  wm.drag = null; wm.vp.classList.remove('dragging-logo'); wmScheduleRender();
}
wm.vp.addEventListener('pointerup', finishLogoDrag);
wm.vp.addEventListener('pointercancel', finishLogoDrag);

function finalForm() {
  const fd = new FormData();
  fd.append('image', wm.preview, 'final.png');
  fd.append('path', S.path);
  return fd;
}
$('#wm-download').addEventListener('click', async () => {
  if (!wm.preview) return;
  try {
    setMsg(wm.msg, 'Encoding…');
    const b = await api.blob('/api/export', { method: 'POST', body: finalForm() });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(b);
    a.download = S.name;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 30000);
    setMsg(wm.msg, `Downloaded ${S.name} (${fmtBytes(b.size)}).`, 'ok');
  } catch (e) {
    setMsg(wm.msg, 'Download failed: ' + e.message, 'error');
  }
});

function resetWmArm() {
  wm.armed = false;
  clearTimeout(wm.armTimer);
  const b = $('#wm-save');
  b.classList.remove('armed');
  b.textContent = 'Save to folder (replace original)';
  $('#wm-save-cancel').hidden = true;
}
$('#wm-save-cancel').addEventListener('click', resetWmArm);
$('#wm-save').addEventListener('click', async () => {
  if (!wm.preview || !S.path) return;
  const b = $('#wm-save');
  if (!wm.armed) {
    wm.armed = true;
    b.classList.add('armed');
    b.textContent = `Confirm: replace ${S.name}`;
    $('#wm-save-cancel').hidden = false;
    setMsg(wm.msg, `Click again to overwrite ${S.path}` + (S.cfg.backup ? ' (original is backed up first).' : ' (backup is DISABLED).'));
    wm.armTimer = setTimeout(resetWmArm, 8000);
    return;
  }
  resetWmArm();
  b.disabled = true;
  try {
    setMsg(wm.msg, 'Saving…');
    const res = await api.json('/api/save', { method: 'POST', body: finalForm() });
    S.dirty = false;
    S.processed.add(S.path);
    if (res.backup) S.restorable.add(S.path);
    markTile(S.path);
    const bk = res.backup ? (res.backup_created ? `Backup: ${res.backup}` : `Backup already existed (kept first one): ${res.backup}`)
      : 'No backup (disabled in config).';
    setMsg(wm.msg, `Saved ${res.path} (${res.format}, ${fmtBytes(res.size)}). ${bk}`, 'ok');
  } catch (e) {
    setMsg(wm.msg, 'Save failed: ' + e.message, 'error');
  } finally {
    updateWmUi();
  }
});

// ================================================================ sidebar
const tree = $('#tree');
const grid = $('#grid');

function makeNode(name, path, expand, photoCount = null) {
  const el = document.createElement('div'); el.className = 'node';
  const row = document.createElement('div'); row.className = 'row'; row.title = path;
  const tw = document.createElement('span'); tw.className = 'twisty'; tw.textContent = '▸';
  const lb = document.createElement('span'); lb.className = 'label'; lb.textContent = name;
  const count = document.createElement('span'); count.className = 'folder-count';
  count.textContent = photoCount == null ? '' : `(${photoCount})`;
  const kids = document.createElement('div'); kids.className = 'kids'; kids.hidden = true;
  row.append(tw, lb, count); el.append(row, kids);
  let loaded = false;
  async function toggle(open) {
    if (open === undefined) open = kids.hidden;
    if (open && !loaded) {
      loaded = true;
      try {
        const d = await api.json('/api/folders?' + qs({ path }));
        d.folders.forEach((f) => kids.append(makeNode(f.name, f.path, false, f.photo_count)));
        if (!d.folders.length) tw.classList.add('empty');
      } catch (e) { kids.textContent = e.message; }
    }
    kids.hidden = !open;
    tw.textContent = open ? '▾' : '▸';
  }
  tw.addEventListener('click', (e) => { e.stopPropagation(); toggle(); });
  row.addEventListener('click', () => { selectFolder(path, row); toggle(true); });
  if (expand) toggle(true);
  return el;
}

async function selectFolder(path, row) {
  tree.querySelectorAll('.row.sel').forEach((r) => r.classList.remove('sel'));
  row.classList.add('sel');
  grid.innerHTML = '<div class="hint">Loading…</div>';
  try {
    const d = await api.json('/api/images?' + qs({ path }));
    const state = await api.json('/api/state?' + qs({ path }));
    S.folderImages = d.images;
    S.selected.clear(); S.lastSelected = -1;
    S.processed = new Set(Object.keys(state.statuses || {}));
    S.restorable = new Set(Object.entries(state.statuses || {}).filter(([, entry]) => entry.backup_path).map(([path]) => path));
    const folderNode = row.closest('.node');
    const folderCount = folderNode?.querySelector(':scope > .row > .folder-count');
    if (folderCount) folderCount.textContent = `(${d.images.length})`;
    $('#selection-toolbar').hidden = false;
    updateSelectionUi();
    $('#grid-title').textContent = `Photos (${d.images.length})`;
    grid.innerHTML = '';
    if (!d.images.length) grid.innerHTML = '<div class="hint">No jpg/png/webp images here</div>';
    for (const [index, im] of d.images.entries()) {
      const t = document.createElement('div');
      t.className = 'tile' + (im.path === S.path ? ' sel' : '');
      t.dataset.path = im.path; t.dataset.index = index;
      t.title = `${im.name}\n${fmtBytes(im.size)}  ${new Date(im.mtime * 1000).toLocaleString()}`;
      const check = document.createElement('input'); check.type = 'checkbox'; check.className = 'tile-check';
      check.checked = S.selected.has(im.path);
      check.addEventListener('click', (e) => { e.stopPropagation(); selectTile(index, e); });
      const img = document.createElement('img'); img.loading = 'lazy'; img.decoding = 'async';
      img.src = '/api/thumb?' + qs({ path: im.path, w: 256, v: im.mtime });
      const cap = document.createElement('div'); cap.className = 'cap'; cap.textContent = im.name;
      t.append(check, img, cap); if (S.processed.has(im.path)) addBadge(t);
      t.addEventListener('click', (e) => { if (e.target !== check) openImage(im.path, im.name); });
      grid.append(t);
    }
  } catch (e) {
    grid.innerHTML = ''; const h = document.createElement('div'); h.className = 'hint'; h.textContent = e.message; grid.append(h);
  }
}
function updateSelectionUi() {
  $('#selection-count').textContent = `Selected ${S.selected.size} of ${S.folderImages.length}`;
  $('#batch-open').textContent = `Watermark selected (${S.selected.size})`;
  $('#batch-open').disabled = !S.selected.size;
  grid.querySelectorAll('.tile').forEach((t) => { t.classList.toggle('batch-selected', S.selected.has(t.dataset.path)); const c = t.querySelector('.tile-check'); if (c) c.checked = S.selected.has(t.dataset.path); });
}
function selectTile(index, e = {}) {
  const paths = S.folderImages.map((x) => x.path);
  if (e.shiftKey && S.lastSelected >= 0) {
    const a = Math.min(index, S.lastSelected), b = Math.max(index, S.lastSelected);
    for (let i = a; i <= b; i++) S.selected.add(paths[i]);
  } else if (S.selected.has(paths[index])) S.selected.delete(paths[index]); else S.selected.add(paths[index]);
  S.lastSelected = index; updateSelectionUi();
}
function addBadge(t) { if (t.querySelector('.done')) return; const b = document.createElement('span'); b.className = 'done'; b.textContent = '✓ processed'; t.append(b); }
function markTile(path) {
  document.querySelectorAll('.tile').forEach((t) => {
    if (t.dataset.path !== path) return;
    addBadge(t);
    const img = t.querySelector('img');
    img.src = '/api/thumb?' + qs({ path, w: 256, v: Date.now() });
  });
}

$('#select-all').addEventListener('click', () => { S.folderImages.forEach((x) => S.selected.add(x.path)); updateSelectionUi(); });
$('#select-none').addEventListener('click', () => { S.selected.clear(); updateSelectionUi(); });
$('#select-invert').addEventListener('click', () => { S.folderImages.forEach((x) => S.selected.has(x.path) ? S.selected.delete(x.path) : S.selected.add(x.path)); updateSelectionUi(); });
$('#select-unprocessed').addEventListener('click', () => { S.selected = new Set(S.folderImages.filter((x) => !S.processed.has(x.path)).map((x) => x.path)); updateSelectionUi(); });
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a' && document.activeElement?.closest('#grid')) {
    e.preventDefault(); S.folderImages.forEach((x) => S.selected.add(x.path)); updateSelectionUi();
  }
});

let batchTimer = null, batchJobId = null, batchPaths = [];
$('#batch-open').addEventListener('click', () => {
  if (!S.selected.size) return;
  $('#batch-panel').hidden = false;
  $('#batch-summary').textContent = `${S.selected.size} file(s) selected. Mode: ${$('#batch-mode').value}. Replace modifies originals after making backups.`;
  $('#batch-results').textContent = ''; $('#batch-progress').value = 0; $('#batch-progress').max = S.selected.size;
  $('#batch-retry').hidden = true;
  renderBatchPreview();
});
async function renderBatchPreview() {
  const preview = $('#batch-preview');
  preview.hidden = true;
  const path = [...S.selected][0];
  if (!path) return;
  try {
    const source = await api.blob('/api/image?' + qs({path}));
    const fd = new FormData();
    fd.append('image', source, 'sample.png');
    fd.append('fmt', 'png');
    for (const [k, v] of Object.entries(wmParams())) fd.append(k, v);
    const rendered = await api.blob('/api/watermark', {method: 'POST', body: fd});
    preview.src = urlFor(rendered);
    preview.hidden = false;
  } catch (e) {
    $('#batch-summary').textContent += ` Preview unavailable: ${e.message}`;
  }
}
$('#batch-close').addEventListener('click', () => { if (!batchJobId) $('#batch-panel').hidden = true; });
$('#batch-confirm').addEventListener('click', async () => {
  try {
    const r = await api.json('/api/batch/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({paths: [...S.selected], mode: $('#batch-mode').value, skip_processed: $('#batch-skip').checked, overrides: wmParams()})});
    batchPaths = [...S.selected];
    batchJobId = r.job_id; $('#batch-confirm').hidden = true; $('#batch-cancel').hidden = false; pollBatch();
  } catch (e) { $('#batch-summary').textContent = 'Could not start batch: ' + e.message; }
});
$('#batch-cancel').addEventListener('click', async () => { if (batchJobId) await api.json('/api/batch/' + batchJobId + '/cancel', {method: 'POST'}); });
$('#batch-retry').addEventListener('click', async () => {
  const failed = $('#batch-results').textContent.split('\n').filter((line) => line.startsWith('failed: ')).map((line) => line.slice(8).split(' — ')[0]).filter(Boolean);
  if (!failed.length) return;
  S.selected = new Set(failed); updateSelectionUi();
  $('#batch-retry').hidden = true;
  $('#batch-confirm').click();
});
async function pollBatch() {
  if (!batchJobId) return;
  try {
    const j = await api.json('/api/batch/' + batchJobId);
    $('#batch-progress').value = j.completed; $('#batch-current').textContent = j.items.find((x) => x.status === 'running')?.path || j.status;
    $('#batch-results').textContent = j.items.filter((x) => ['ok','skipped','failed'].includes(x.status)).map((x) => `${x.status}: ${x.path}${x.error ? ' — ' + x.error : ''}`).join('\n');
    if (j.status === 'running') batchTimer = setTimeout(pollBatch, 500); else {
      batchJobId = null; $('#batch-cancel').hidden = true; $('#batch-confirm').hidden = false;
      $('#batch-retry').hidden = !j.items.some((x) => x.status === 'failed');
      selectFolder($('#tree .row.sel')?.title, $('#tree .row.sel'));
    }
  } catch (e) { $('#batch-results').textContent = e.message; }
}

function setTab(name) {
  S.tab = name;
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $('#tab-remove').classList.toggle('active', name === 'remove');
  $('#tab-wm').classList.toggle('active', name === 'wm');
  if (name === 'wm') {
    updateWmUi();
    requestAnimationFrame(() => wm.viewer.autoFit && wm.viewer.fit());
  } else {
    requestAnimationFrame(() => rm.viewer.autoFit && rm.viewer.fit());
  }
}
document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));

async function checkHealth() {
  const el = $('#iop-status');
  try {
    const h = await api.json('/api/health');
    el.className = 'pill ' + (h.mock ? 'mock' : h.ok ? 'ok' : 'bad');
    el.textContent = h.mock ? 'IOPaint: MOCK' : h.ok ? 'IOPaint: connected' : 'IOPaint: offline';
    el.title = h.ok ? h.url : (h.error || 'unreachable') + '\n(click to re-check)';
    if (!h.ok && !rm.busy) setMsg(rm.msg, h.error, 'error');
  } catch (e) {
    el.className = 'pill bad'; el.textContent = 'Server error'; el.title = e.message;
  }
}
$('#iop-status').addEventListener('click', checkHealth);

const isTyping = (t) => (t.tagName === 'INPUT' && !['range', 'checkbox', 'button'].includes(t.type)) || t.tagName === 'TEXTAREA';

document.addEventListener('keydown', (e) => {
  if (isTyping(e.target)) return;
  const k = e.key;
  if (k === 'Alt') { e.preventDefault(); rm.altHeld = true; updateCursor(); return; }
  if (e.code === 'Space') {
    e.preventDefault();
    if (!S.spaceHeld) { S.spaceHeld = true; rm.vp.classList.add('space'); wm.vp.classList.add('space'); updateCursor(); }
    return;
  }
  const mod = e.ctrlKey || e.metaKey;
  if (S.tab === 'remove') {
    if (mod && k.toLowerCase() === 'z') { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
    if (mod && k.toLowerCase() === 'y') { e.preventDefault(); redo(); return; }
    if (mod) return;
    if (k === '[') { setBrush(rm.brush / 1.15 - 1); }
    else if (k === ']') { setBrush(rm.brush * 1.15 + 1); }
    else if (k === 'Enter') { e.preventDefault(); inpaint(); }
    else if (k === 'v' || k === 'V') { if (!rm.vHeld) { rm.vHeld = true; updateCompare(); } }
    else if (k === 'e' || k === 'E') { $('#eraser').click(); }
    else if (k === 'f' || k === 'F' || k === '0') { rm.viewer.fit(); }
  } else if (!mod) {
    if (k === 'w' || k === 'W') $('#wm-toggle').click();
    else if (k === 'f' || k === 'F' || k === '0') wm.viewer.fit();
  }
});
document.addEventListener('keyup', (e) => {
  if (e.key === 'Alt') { e.preventDefault(); rm.altHeld = false; updateCursor(); }
  if (e.code === 'Space') { S.spaceHeld = false; rm.vp.classList.remove('space'); wm.vp.classList.remove('space'); updateCursor(); }
  if (e.key === 'v' || e.key === 'V') { rm.vHeld = false; updateCompare(); }
});
window.addEventListener('blur', () => {
  rm.altHeld = false; rm.vHeld = false; S.spaceHeld = false;
  rm.vp.classList.remove('space'); wm.vp.classList.remove('space');
  updateCompare(); updateCursor();
});
window.addEventListener('beforeunload', (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ''; } });

// ================================================================ init
(async function init() {
  try {
    S.cfg = await api.json('/api/config');
  } catch (e) {
    setMsg(rm.msg, 'Cannot load config: ' + e.message, 'error');
    return;
  }
  rm.dilateEl.value = S.cfg.default_dilate_px;
  setDilate(S.cfg.default_dilate_px);
  setBrush(40);
  resetWmSliders();
  updateRmButtons();
  updateWmUi();
  if (!S.cfg.roots.length) {
    tree.innerHTML = '<div class="hint">No allowed_roots configured.<br>Set allowed_roots in config.yaml or the ALLOWED_ROOTS env var.</div>';
  } else {
    try {
      const roots = await api.json('/api/folders');
      roots.folders.forEach((r, i) => tree.append(makeNode(r.name, r.path, i === 0, r.photo_count)));
    } catch (e) {
      tree.innerHTML = `<div class="hint">Could not load folders: ${e.message}</div>`;
    }
  }
  if (!S.cfg.has_logo) setMsg(wm.msg, 'assets/logo.png not found: the watermark will have no logo.', 'error');
  checkHealth();
})();
