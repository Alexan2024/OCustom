/* ÖMANKÖ custom station — редактор в реальном масштабе.
   Координаты хранятся в мм: x — смещение центра стикера от вертикальной оси зоны
   (+ вправо), y — от верхнего края зоны до центра стикера. */

const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand();

const $ = (id) => document.getElementById(id);
const state = {
  cfg: null, stickers: [],
  size: null, side: "front",
  placed: [],          // {uid, s, side, x_mm, y_mm, rotation}
  sel: null,
  ppm: 1,              // px per mm
  viewMode: false,
  uidSeq: 1,
};

const haptic = (t = "light") => tg?.HapticFeedback?.impactOccurred?.(t);

/* ---------- Загрузка ---------- */

async function boot() {
  const [cfg, stickers] = await Promise.all([
    fetch("/api/config").then(r => r.json()),
    fetch("/api/stickers").then(r => r.json()),
  ]);
  state.cfg = cfg; state.stickers = stickers;
  const params = new URLSearchParams(location.search);
  if (params.get("view")) return bootView(params.get("view"), params.get("key"));

  state.size = Object.keys(cfg.sizes).find(s => cfg.sizes[s].stock > 0) || "M";
  $("quota").textContent = cfg.quota_left > 0
    ? `сегодня осталось мест: ${cfg.quota_left}` : "на сегодня мест нет";
  renderSizes(); renderCatalog(); layout(); renderAll(); updateBar();
}

async function bootView(oid, key) {
  state.viewMode = true;
  const r = await fetch(`/api/orders/${oid}?key=${encodeURIComponent(key)}`);
  if (!r.ok) { document.body.innerHTML = "<p style='padding:40px'>Заказ не найден</p>"; return; }
  const o = await r.json();
  state.size = o.size;
  state.placed = o.items.map(i => ({
    uid: state.uidSeq++, side: i.side, x_mm: i.x_mm, y_mm: i.y_mm, rotation: i.rotation,
    s: { id: i.sticker_id, name: i.name, file: i.file, width_mm: i.width_mm, height_mm: i.height_mm },
  }));
  $("quota").textContent = `заказ №${o.id} · ${o.size} · ${o.price} ₽`;
  document.querySelector(".controls .sizes").remove();
  document.querySelector(".bottom").remove();
  $("hint").textContent = "Режим просмотра: раскладка как её собрал клиент.";
  layout(); renderAll();
}

/* ---------- Размеры ---------- */

function renderSizes() {
  const el = $("sizes"); el.innerHTML = "";
  for (const [s, v] of Object.entries(state.cfg.sizes)) {
    const b = document.createElement("button");
    b.className = "size-chip" + (s === state.size ? " active" : "");
    b.disabled = v.stock <= 0;
    b.innerHTML = `${s}<small>${v.stock > 0 ? "ост. " + v.stock : "нет"}</small>`;
    b.onclick = () => { state.size = s; renderSizes(); layout(); renderAll(); haptic(); };
    el.appendChild(b);
  }
}

/* ---------- Геометрия сцены ---------- */

function zoneMM() { return state.cfg.sizes[state.size]; }

function layout() {
  const stage = $("stage");
  const { print_w_mm: W, print_h_mm: H } = zoneMM();
  const availW = stage.clientWidth - 24, availH = stage.clientHeight - 16;
  // зона = 55% ширины и 72% высоты силуэта футболки
  let zoneH = availH * 0.72, zoneW = zoneH * (W / H);
  if (zoneW / 0.55 > availW) { zoneW = availW * 0.55; zoneH = zoneW * (H / W); }
  state.ppm = zoneW / W;

  const shirtW = zoneW / 0.55, shirtH = zoneH / 0.72;
  const shirt = $("shirt");
  shirt.style.width = shirtW + "px"; shirt.style.height = shirtH + "px";

  const svg = $("shirt-svg");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("width", shirtW); svg.setAttribute("height", shirtH);
  const collar = state.side === "front"
    ? "M38,6 Q50,16 62,6" : "M38,6 Q50,10 62,6";
  svg.innerHTML = `
    <path d="M38,6 L20,12 L2,24 L8,38 L20,32 L20,98 L80,98 L80,32 L92,38 L98,24 L80,12 L62,6 ${collar} Z"
      fill="#1d1f22" stroke="#34373c" stroke-width="0.7" vector-effect="non-scaling-stroke"/>`;

  const zone = $("zone");
  zone.style.width = zoneW + "px"; zone.style.height = zoneH + "px";
  zone.style.left = (shirtW - zoneW) / 2 + "px";
  zone.style.top = shirtH * 0.16 + "px";
  zone.style.setProperty("--tick", (50 * state.ppm) + "px"); // риска = 50 мм
}

/* эффективные (с учётом поворота) полуразмеры, мм */
function halfDims(p) {
  let w = p.s.width_mm, h = p.s.height_mm;
  if (p.rotation % 180 !== 0) [w, h] = [h, w];
  return [w / 2, h / 2];
}

function clamp(p) {
  const { print_w_mm: W, print_h_mm: H } = zoneMM();
  const [hw, hh] = halfDims(p);
  p.x_mm = Math.min(W / 2 - hw, Math.max(-(W / 2 - hw), p.x_mm));
  p.y_mm = Math.min(H - hh, Math.max(hh, p.y_mm));
}

function overlaps(a, b) {
  if (a.side !== b.side) return false;
  const [aw, ah] = halfDims(a), [bw, bh] = halfDims(b);
  return Math.abs(a.x_mm - b.x_mm) < aw + bw && Math.abs(a.y_mm - b.y_mm) < ah + bh;
}

function anyOverlap() {
  const ps = state.placed;
  for (let i = 0; i < ps.length; i++)
    for (let j = i + 1; j < ps.length; j++)
      if (overlaps(ps[i], ps[j])) return true;
  return false;
}

/* ---------- Рендер стикеров ---------- */

function renderAll() {
  document.querySelectorAll(".sticker").forEach(e => e.remove());
  const zone = $("zone"), { print_w_mm: W } = zoneMM();
  for (const p of state.placed.filter(p => p.side === state.side)) {
    const d = document.createElement("div");
    d.className = "sticker";
    d.dataset.uid = p.uid;
    const w = p.s.width_mm * state.ppm, h = p.s.height_mm * state.ppm;
    d.style.width = w + "px"; d.style.height = h + "px";
    d.style.left = (W / 2 + p.x_mm) * state.ppm + "px";
    d.style.top = p.y_mm * state.ppm + "px";
    d.style.transform = `translate(-50%,-50%) rotate(${p.rotation}deg)`;
    d.innerHTML = `<img src="/stickers/${p.s.file}" draggable="false">
      <div class="dim">${p.s.width_mm}×${p.s.height_mm} мм</div>`;
    if (!state.viewMode) attachDrag(d, p);
    zone.appendChild(d);
  }
  refreshFlags();
}

function refreshFlags() {
  const bad = new Set();
  const ps = state.placed;
  for (let i = 0; i < ps.length; i++)
    for (let j = i + 1; j < ps.length; j++)
      if (overlaps(ps[i], ps[j])) { bad.add(ps[i].uid); bad.add(ps[j].uid); }
  document.querySelectorAll(".sticker").forEach(el => {
    const uid = +el.dataset.uid;
    el.classList.toggle("overlap", bad.has(uid));
    el.classList.toggle("selected", state.sel === uid && !bad.has(uid));
  });
  $("selTools").classList.toggle("hidden", state.sel == null || state.viewMode);
  const hint = $("hint");
  if (bad.size) { hint.textContent = "Принты пересекаются — так запечатать нельзя, раздвинь их."; hint.className = "hint warn"; }
  else if (!state.viewMode) { hint.textContent = "Тапни принт, чтобы повернуть или убрать. Пунктир — центр."; hint.className = "hint"; }
  if (!state.viewMode) updateBar();
}

/* ---------- Drag ---------- */

function attachDrag(el, p) {
  let startX, startY, sx, sy, snapped = false;
  el.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    state.sel = p.uid; refreshFlags();
    startX = e.clientX; startY = e.clientY; sx = p.x_mm; sy = p.y_mm;
  });
  el.addEventListener("pointermove", (e) => {
    if (startX == null) return;
    p.x_mm = sx + (e.clientX - startX) / state.ppm;
    p.y_mm = sy + (e.clientY - startY) / state.ppm;
    if (Math.abs(p.x_mm) < 4) {                 // снап к центральной оси
      p.x_mm = 0;
      if (!snapped) { haptic("medium"); snapped = true; }
    } else snapped = false;
    clamp(p);
    const { print_w_mm: W } = zoneMM();
    el.style.left = (W / 2 + p.x_mm) * state.ppm + "px";
    el.style.top = p.y_mm * state.ppm + "px";
    refreshFlags();
  });
  const end = () => { startX = null; };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}

/* ---------- Каталог ---------- */

function usedCount(sid) { return state.placed.filter(p => p.s.id === sid).length; }

function renderCatalog() {
  const el = $("catalog"); el.innerHTML = "";
  for (const s of state.stickers) {
    const left = s.stock - usedCount(s.id);
    const d = document.createElement("div");
    d.className = "cat-item" + (left <= 0 ? " out" : "");
    d.innerHTML = `<img src="/stickers/${s.file}"><div class="nm">${s.name}</div>
      <div class="sz">${s.width_mm}×${s.height_mm} мм</div>
      <div class="st">${left > 0 ? "осталось " + left : "разобрали"}</div>`;
    d.onclick = () => addSticker(s);
    el.appendChild(d);
  }
}

function addSticker(s) {
  if (state.placed.length >= state.cfg.max_prints) {
    tg?.showAlert?.(`Максимум ${state.cfg.max_prints} принтов на футболку — иначе запечатка займёт вечность.`);
    return;
  }
  const p = { uid: state.uidSeq++, s, side: state.side, x_mm: 0, y_mm: 0, rotation: 0 };
  const { print_h_mm: H } = zoneMM();
  p.y_mm = H * 0.3; clamp(p);
  // не роняем новый принт на существующий
  let guard = 0;
  while (state.placed.some(q => overlaps(p, q)) && guard++ < 20) {
    p.y_mm += 20; clamp(p);
  }
  state.placed.push(p);
  state.sel = p.uid;
  closeSheet(); renderAll(); renderCatalog(); haptic();
}

/* ---------- Панель, цена, заказ ---------- */

function price() {
  const n = state.placed.length;
  const extra = Math.max(0, n - state.cfg.included_prints);
  return state.cfg.base_price + extra * state.cfg.extra_print_price;
}

function updateBar() {
  const n = state.placed.length, c = state.cfg;
  $("price").innerHTML = n
    ? `${price()} ₽<small>${n} принт(а) · ${c.included_prints} включено</small>`
    : `${c.base_price} ₽<small>${c.included_prints} принта включено</small>`;
  $("btnOrder").disabled = n === 0 || anyOverlap() || c.quota_left <= 0;
}

async function submitOrder() {
  const btn = $("btnOrder"); btn.disabled = true; btn.textContent = "…";
  const body = {
    size: state.size,
    items: state.placed.map(p => ({
      side: p.side, sticker_id: p.s.id,
      x_mm: +p.x_mm.toFixed(1), y_mm: +p.y_mm.toFixed(1), rotation: p.rotation,
    })),
  };
  const r = await fetch("/api/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json",
               "X-Telegram-Init-Data": tg?.initData || "" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    tg?.showAlert?.(data.detail || "Не получилось создать заказ");
    btn.disabled = false; btn.textContent = "Заказать";
    return;
  }
  haptic("heavy");
  const hold = data.hold_minutes
    ? `<br><br>Ссылка ждёт ${data.hold_minutes} минут — потом принты вернутся в каталог.`
    : "";
  const ov = $("overlay");
  ov.classList.remove("hidden");
  ov.innerHTML = `
    <h2>Заказ принят</h2>
    <div class="num">№${data.order_id}</div>
    <p>Сумма ${data.price} ₽. ${data.pay_url
      ? "Ссылка на оплату — в чате с ботом."
      : "Реквизиты для оплаты придут в чат с ботом."}${hold}<br><br>
    После оплаты возьмём в работу и напишем, когда будет готово.</p>`;
  setTimeout(() => tg?.close?.(), data.pay_url ? 1800 : 2600);
}

/* ---------- События ---------- */

document.querySelectorAll(".side-tab").forEach(b => b.onclick = () => {
  document.querySelectorAll(".side-tab").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  state.side = b.dataset.side; state.sel = null;
  layout(); renderAll();
});
$("btnCatalog").onclick = () => { renderCatalog(); $("sheet").classList.remove("hidden"); $("scrim").classList.remove("hidden"); };
const closeSheet = () => { $("sheet").classList.add("hidden"); $("scrim").classList.add("hidden"); };
$("btnCloseSheet").onclick = closeSheet;
$("scrim").onclick = closeSheet;
$("btnRotate").onclick = () => {
  const p = state.placed.find(p => p.uid === state.sel); if (!p) return;
  p.rotation = (p.rotation + 90) % 360; clamp(p); renderAll(); haptic();
};
$("btnDelete").onclick = () => {
  state.placed = state.placed.filter(p => p.uid !== state.sel);
  state.sel = null; renderAll(); renderCatalog(); haptic();
};
$("btnOrder").onclick = submitOrder;
$("zone").addEventListener("pointerdown", (e) => {
  if (e.target.id === "zone") { state.sel = null; refreshFlags(); }
});
window.addEventListener("resize", () => { layout(); renderAll(); });

boot();
