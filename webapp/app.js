/* ÖMANKÖ custom station — редактор в реальном масштабе.

   Координаты хранятся в мм: x — смещение центра стикера от вертикальной оси
   зоны (+ вправо), y — от верхнего края зоны до центра стикера.
   На рукаве та же пара означает другое: x идёт вокруг руки (+ к спине),
   y — вниз от проймы. Сторон четыре: front, back, sleeve_l, sleeve_r. */

const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand();
/* Свайп вниз внутри мини-аппа Telegram сворачивает окно. При перетаскивании
   принта вниз срабатывал именно он, а не перетаскивание. Метод есть
   с Bot API 7.7; на клиентах постарее его нет — там работает страховка
   с touchmove ниже. */
tg?.disableVerticalSwipes?.();

const $ = (id) => document.getElementById(id);
const SLEEVES = ["sleeve_l", "sleeve_r"];
const SIDE_NAMES = {
  front: "перед", back: "спина",
  sleeve_l: "левый рукав", sleeve_r: "правый рукав",
};
const SLEEVE_SPLIT_MM = 60;   // просвет между схемами рукавов на экране

const state = {
  cfg: null, stickers: [],
  size: null,
  view: "front",       // front | back | sleeves
  target: "front",     // куда кладём следующий принт
  placed: [],          // {uid, s, side, x_mm, y_mm, rotation}
  sel: null,
  ppm: 1,              // px per mm
  zoneEls: {},         // side -> DOM-узел зоны, живёт до следующего layout()
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
    b.onclick = () => {
      state.size = s;
      // зоны у размеров разные — то, что перестало влезать, надо убрать
      const dropped = state.placed.filter(p => !fitsZone(p.s, p.side, p.rotation));
      if (dropped.length) {
        state.placed = state.placed.filter(p => fitsZone(p.s, p.side, p.rotation));
        tg?.showAlert?.(`На размере ${s} не помещается: ` +
          dropped.map(p => p.s.name).join(", "));
      }
      state.placed.forEach(clamp);
      renderSizes(); layout(); renderAll(); renderCatalog(); haptic();
    };
    el.appendChild(b);
  }
}

/* ---------- Геометрия сцены ---------- */

function zoneOf(side) { return state.cfg.sizes[state.size].zones[side]; }
function visibleSides() { return state.view === "sleeves" ? SLEEVES : [state.view]; }

function makeZone(side) {
  const z = zoneOf(side);
  const el = document.createElement("div");
  el.className = "zone" + (state.target === side && !state.viewMode ? " target" : "");
  el.dataset.side = side;
  el.style.width = z.w_mm * state.ppm + "px";
  el.style.height = z.h_mm * state.ppm + "px";
  el.style.setProperty("--tick", (50 * state.ppm) + "px");   // риска = 50 мм
  el.innerHTML = '<div class="zone-center"></div>';
  el.addEventListener("pointerdown", (e) => {
    if (e.target !== el && !e.target.classList.contains("zone-center")) return;
    state.sel = null;
    if (!state.viewMode && state.target !== side) { state.target = side; layoutRefresh(); }
    else refreshFlags();
  });
  state.zoneEls[side] = el;
  return el;
}

function layout() {
  const stage = $("stage");
  stage.innerHTML = "";
  state.zoneEls = {};
  const availW = stage.clientWidth - 24, availH = stage.clientHeight - 16;
  if (availW <= 0 || availH <= 0) return;
  if (state.view === "sleeves") layoutSleeves(availW, availH);
  else layoutBody(availW, availH);
}

/* Пересобрать сцену и вернуть стикеры на место (после смены цели/поворота) */
function layoutRefresh() { layout(); renderAll(); }

function layoutBody(availW, availH) {
  const ph = state.cfg.photo;
  const aspect = ph.w_px / ph.h_px;
  // Снимок можно растянуть по вертикали (PHOTO_STRETCH_Y в config.py): фото —
  // сток-мокап, сжатый до реальных пропорций бланка, и без растяжки футболка
  // выглядит приплюснутой. Миллиметры от этого не едут: масштаб берётся
  // от ширины по груди, а верх зоны привязан к линии плеча, доля которой
  // в высоте снимка не меняется.
  const k = ph.stretch_y || 1;
  const shirtW = Math.min(availW, availH * aspect / k);
  const shirtH = shirtW / aspect * k;

  // Масштаб берём от ширины по груди: на фото это chest_x0..chest_x1,
  // в жизни — число B из размерной сетки.
  const chestPx = shirtW * (ph.chest_x1 - ph.chest_x0) / ph.w_px;
  state.ppm = chestPx / state.cfg.sizes[state.size].chest_mm;

  const shirt = document.createElement("div");
  shirt.className = "shirt";
  shirt.style.width = shirtW + "px";
  shirt.style.height = shirtH + "px";
  const img = document.createElement("img");
  img.className = "shirt-photo";
  img.src = `shirt_${state.view}.webp`;
  img.draggable = false;
  shirt.appendChild(img);

  const z = zoneOf(state.view);
  const el = makeZone(state.view);
  const centerX = shirtW * ((ph.chest_x0 + ph.chest_x1) / 2) / ph.w_px;
  el.style.left = (centerX - z.w_mm * state.ppm / 2) + "px";
  el.style.top = (shirtH * ph.shoulder_y / ph.h_px + z.top_mm * state.ppm) + "px";
  shirt.appendChild(el);
  $("stage").appendChild(shirt);
}

function layoutSleeves(availW, availH) {
  // Рукав показываем схемой, а не фотографией: на фронтальном кадре он уходит
  // под углом и укорочен, и миллиметры по нему врали бы.
  const z = zoneOf("sleeve_l");
  state.ppm = Math.min(
    availW / (z.w_mm * 2 + SLEEVE_SPLIT_MM),
    (availH - 46) / z.h_mm,
  );
  const wrap = document.createElement("div");
  wrap.className = "sleeve-wrap";
  wrap.style.gap = SLEEVE_SPLIT_MM * state.ppm + "px";
  for (const side of SLEEVES) {
    const block = document.createElement("div");
    block.className = "sleeve-block";
    const lab = document.createElement("div");
    lab.className = "sleeve-label";
    lab.textContent = side === "sleeve_l" ? "Левый" : "Правый";
    const el = makeZone(side);
    el.classList.add("static");
    block.appendChild(lab);
    block.appendChild(el);
    wrap.appendChild(block);
  }
  $("stage").appendChild(wrap);
}

/* эффективные (с учётом поворота) полуразмеры, мм */
function halfDims(p) {
  let w = p.s.width_mm, h = p.s.height_mm;
  if (p.rotation % 180 !== 0) [w, h] = [h, w];
  return [w / 2, h / 2];
}

function fitsZone(s, side, rotation = 0) {
  const z = zoneOf(side);
  let [w, h] = [s.width_mm, s.height_mm];
  if (rotation % 180 !== 0) [w, h] = [h, w];
  return w <= z.w_mm && h <= z.h_mm;
}

function clamp(p) {
  const z = zoneOf(p.side);
  const [hw, hh] = halfDims(p);
  p.x_mm = Math.min(z.w_mm / 2 - hw, Math.max(-(z.w_mm / 2 - hw), p.x_mm));
  p.y_mm = Math.min(z.h_mm - hh, Math.max(hh, p.y_mm));
}

/* Между принтами нужен просвет: пресс жмёт наклейки по одной, и край
   платена не должен лечь на уже запечатанного соседа. */
function tooClose(a, b) {
  if (a.side !== b.side) return false;
  const g = state.cfg.min_gap_mm;
  const [aw, ah] = halfDims(a), [bw, bh] = halfDims(b);
  return Math.abs(a.x_mm - b.x_mm) < aw + bw + g
      && Math.abs(a.y_mm - b.y_mm) < ah + bh + g;
}

function anyTooClose() {
  const ps = state.placed;
  for (let i = 0; i < ps.length; i++)
    for (let j = i + 1; j < ps.length; j++)
      if (tooClose(ps[i], ps[j])) return true;
  return false;
}

/* ---------- Рендер стикеров ---------- */

function renderAll() {
  for (const side of visibleSides()) {
    const zEl = state.zoneEls[side];
    if (!zEl) continue;
    zEl.querySelectorAll(".sticker").forEach(e => e.remove());
    const z = zoneOf(side);
    for (const p of state.placed.filter(p => p.side === side)) {
      const d = document.createElement("div");
      d.className = "sticker";
      d.dataset.uid = p.uid;
      d.style.width = p.s.width_mm * state.ppm + "px";
      d.style.height = p.s.height_mm * state.ppm + "px";
      d.style.left = (z.w_mm / 2 + p.x_mm) * state.ppm + "px";
      d.style.top = p.y_mm * state.ppm + "px";
      d.style.transform = `translate(-50%,-50%) rotate(${p.rotation}deg)`;
      d.innerHTML = `<img src="/stickers/${p.s.file}" draggable="false">
        <div class="dim">${p.s.width_mm}×${p.s.height_mm} мм</div>`;
      if (!state.viewMode) attachDrag(d, p);
      zEl.appendChild(d);
    }
  }
  refreshFlags();
}

function refreshFlags() {
  const bad = new Set();
  const ps = state.placed;
  for (let i = 0; i < ps.length; i++)
    for (let j = i + 1; j < ps.length; j++)
      if (tooClose(ps[i], ps[j])) { bad.add(ps[i].uid); bad.add(ps[j].uid); }
  document.querySelectorAll(".sticker").forEach(el => {
    const uid = +el.dataset.uid;
    el.classList.toggle("overlap", bad.has(uid));
    el.classList.toggle("selected", state.sel === uid && !bad.has(uid));
  });
  $("selTools").classList.toggle("hidden", state.sel == null || state.viewMode);

  const hint = $("hint");
  if (bad.size) {
    hint.textContent = `Между принтами нужно ${state.cfg.min_gap_mm} мм — ` +
      "иначе пресс заденет соседний. Раздвинь их.";
    hint.className = "hint warn";
  } else if (!state.viewMode) {
    hint.textContent = state.view === "sleeves"
      ? `Кладём на ${SIDE_NAMES[state.target]} — тапни другой, чтобы переключить. ` +
        "Левый и правый — как на человеке."
      : "Тапни принт, чтобы повернуть или убрать. Пунктир — центр.";
    hint.className = "hint";
  }
  if (!state.viewMode) updateBar();
}

/* ---------- Drag ---------- */

function attachDrag(el, p) {
  let startX, startY, sx, sy, snapped = false;
  el.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    state.sel = p.uid;
    if (state.target !== p.side) { state.target = p.side; layoutRefresh(); return; }
    refreshFlags();
    startX = e.clientX; startY = e.clientY; sx = p.x_mm; sy = p.y_mm;
    document.body.classList.add("dragging");
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
    const z = zoneOf(p.side);
    el.style.left = (z.w_mm / 2 + p.x_mm) * state.ppm + "px";
    el.style.top = p.y_mm * state.ppm + "px";
    refreshFlags();
  });
  const end = () => { startX = null; document.body.classList.remove("dragging"); };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}

/* ---------- Каталог ---------- */

function usedCount(sid) { return state.placed.filter(p => p.s.id === sid).length; }

function renderCatalog() {
  const el = $("catalog"); el.innerHTML = "";
  const side = state.target;
  $("sheetSide").textContent = `на ${SIDE_NAMES[side]}, размеры реальные`;
  const list = state.stickers.filter(s => fitsZone(s, side));
  if (!list.length) {
    el.innerHTML = `<div class="empty">В зону ${SIDE_NAMES[side]} ` +
      `(${zoneOf(side).w_mm}×${zoneOf(side).h_mm} мм) ни один принт не помещается.</div>`;
    return;
  }
  for (const s of list) {
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
  const side = state.target;
  const z = zoneOf(side);
  const p = { uid: state.uidSeq++, s, side, x_mm: 0, y_mm: z.h_mm * 0.3, rotation: 0 };
  clamp(p);
  // не роняем новый принт на существующий
  let guard = 0;
  while (state.placed.some(q => tooClose(p, q)) && guard++ < 30) {
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
  $("btnOrder").disabled = n === 0 || anyTooClose() || c.quota_left <= 0;
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
  state.view = b.dataset.view;
  state.target = state.view === "sleeves" ? "sleeve_l" : state.view;
  state.sel = null;
  layout(); renderAll(); renderCatalog();
});
$("btnCatalog").onclick = () => {
  renderCatalog();
  $("sheet").classList.remove("hidden"); $("scrim").classList.remove("hidden");
};
const closeSheet = () => { $("sheet").classList.add("hidden"); $("scrim").classList.add("hidden"); };
$("btnCloseSheet").onclick = closeSheet;
$("scrim").onclick = closeSheet;
$("btnRotate").onclick = () => {
  const p = state.placed.find(p => p.uid === state.sel); if (!p) return;
  const next = (p.rotation + 90) % 360;
  if (!fitsZone(p.s, p.side, next)) {
    tg?.showAlert?.("Повёрнутым он в эту зону не влезет.");
    return;
  }
  p.rotation = next; clamp(p); renderAll(); haptic();
};
$("btnDelete").onclick = () => {
  state.placed = state.placed.filter(p => p.uid !== state.sel);
  state.sel = null; renderAll(); renderCatalog(); haptic();
};
$("btnOrder").onclick = submitOrder;
window.addEventListener("resize", () => { layout(); renderAll(); });

/* Пока тащим принт, страница не должна прокручиваться: именно эта прокрутка
   на телефоне и превращается в жест «свернуть мини-апп». Каталога не касается —
   класс висит только во время перетаскивания. */
document.addEventListener("touchmove", (e) => {
  if (document.body.classList.contains("dragging")) e.preventDefault();
}, { passive: false });

boot();
