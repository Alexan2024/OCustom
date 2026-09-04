/* ÖMANKÖ custom station — редактор в реальном масштабе.

   Координаты хранятся в мм: x — смещение центра стикера от вертикальной оси
   зоны (+ вправо), y — от верхнего края зоны до центра стикера.
   На рукаве та же пара означает другое: x идёт вокруг руки (+ к спине),
   y — вниз от проймы. Сторон четыре: front, back, sleeve_l, sleeve_r.

   Поворот свободный, 0..359°. Габарит, по которому принт удерживается
   в зоне, всегда неповёрнутый: на промежуточных углах углы принта могут
   выйти за пунктир. Так же считает и сервер — правило одно на обе стороны.

   Второй экран — оформление: способ получения, получатель, доставка.
   Цену доставки здесь только показываем; при создании заказа сервер
   пересчитывает её сам и в спорной ситуации побеждает он. */

const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand();
/* Свайп вниз внутри мини-аппа Telegram сворачивает окно. При перетаскивании
   принта вниз срабатывал именно он, а не перетаскивание. Метод есть
   с Bot API 7.7; на клиентах постарее его нет — там работает страховка
   с touchmove ниже. */
tg?.disableVerticalSwipes?.();
/* Шапка Telegram своего цвета оставляла шов над мини-аппом. */
try { tg?.setHeaderColor?.("#efefef"); } catch (e) { /* старый клиент */ }
try { tg?.setBackgroundColor?.("#efefef"); } catch (e) { /* старый клиент */ }

const $ = (id) => document.getElementById(id);
const SLEEVES = ["sleeve_l", "sleeve_r"];
const SIDE_NAMES = {
  front: "перед", back: "спина",
  sleeve_l: "левый рукав", sleeve_r: "правый рукав",
};
const SLEEVE_SPLIT_MM = 60;   // просвет между схемами рукавов на экране
const SNAP_DEG = 5;           // магнит поворота к 0/90/180/270
const STOCK_POLL_MS = 45000;  // как часто переспрашиваем остатки

const METHOD_NAMES = {
  pickup: "Самовывоз",
  cdek_pvz: "СДЭК, пункт выдачи",
  cdek_door: "СДЭК, курьер до двери",
};

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
  preview: false,      // «как выглядит»: без сетки и подписей
  uidSeq: 1,
  gone: new Set(),     // id принтов, которые разобрали, пока человек собирал
  catQuery: "",
  catBySize: false,
  delivery: {
    method: "pickup",
    name: "", phone: "",
    city: null,        // {code, city, region}
    point: null,       // {code, address, work_time}
    address: "",
    price: 0, period: "", calcing: false, error: "",
  },
};

const haptic = (t = "light") => tg?.HapticFeedback?.impactOccurred?.(t);
const initHeaders = () => ({ "X-Telegram-Init-Data": tg?.initData || "" });

/* ---------- Показ и скрытие с переходом ---------- */

function show(el) {
  el.classList.remove("hidden");
  requestAnimationFrame(() => el.classList.add("open"));
}

function hide(el) {
  el.classList.remove("open");
  setTimeout(() => el.classList.add("hidden"), 220);
}

/* ---------- Кнопка «Назад» Telegram ---------- */

/* Стопка открытых слоёв: аппаратная кнопка на Android иначе закрывала
   весь мини-апп, а не панель поверх него. */
const navStack = [];

function pushNav(close) {
  navStack.push(close);
  syncBack();
}

function dropNav(close) {
  const i = navStack.lastIndexOf(close);
  if (i >= 0) navStack.splice(i, 1);
  syncBack();
}

function syncBack() {
  const bb = tg?.BackButton;
  if (!bb) return;
  navStack.length ? bb.show?.() : bb.hide?.();
}

tg?.BackButton?.onClick?.(() => {
  const close = navStack[navStack.length - 1];
  if (close) close();
});

/* ---------- Тосты ---------- */

function toast(text, opts = {}) {
  const box = $("toasts");
  const el = document.createElement("div");
  el.className = "toast" + (opts.warn ? " warn" : "");
  const span = document.createElement("span");
  span.textContent = text;
  el.appendChild(span);
  let timer;
  const close = () => {
    clearTimeout(timer);
    el.classList.remove("on");
    setTimeout(() => el.remove(), 220);
  };
  if (opts.action) {
    const b = document.createElement("button");
    b.textContent = opts.action.label;
    b.onclick = () => { opts.action.fn(); close(); };
    el.appendChild(b);
  }
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add("on"));
  timer = setTimeout(close, opts.ms || (opts.action ? 5000 : 2600));
  return close;
}

/* ---------- Загрузка ---------- */

async function boot() {
  let cfg, stickers;
  try {
    [cfg, stickers] = await Promise.all([
      fetch("/api/config").then(r => r.json()),
      fetch("/api/stickers").then(r => r.json()),
    ]);
  } catch (e) {
    $("boot").innerHTML = "<p style='padding:40px;text-align:center'>" +
      "Не получилось загрузить каталог. Открой мини-апп ещё раз.</p>";
    return;
  }
  state.cfg = cfg; state.stickers = stickers;
  const params = new URLSearchParams(location.search);
  if (params.get("view")) return bootView(params.get("view"), params.get("key"));

  state.size = Object.keys(cfg.sizes).find(s => cfg.sizes[s].stock > 0)
    || Object.keys(cfg.sizes)[0];
  renderSizes(); renderCatalog(); layout(); renderAll(); updateBar();
  hideBoot();

  // Имя и телефон, если человек их уже давал — чтобы не вводил заново
  if (cfg.delivery?.cdek) {
    fetch("/api/me", { headers: initHeaders() })
      .then(r => r.ok ? r.json() : null)
      .then(me => {
        if (!me) return;
        state.delivery.name = state.delivery.name || me.name || "";
        state.delivery.phone = state.delivery.phone || me.phone || "";
      })
      .catch(() => {});
  }

  setInterval(refreshStock, STOCK_POLL_MS);
}

function hideBoot() {
  const b = $("boot");
  if (!b) return;
  b.classList.add("fade");
  setTimeout(() => b.remove(), 300);
}

async function bootView(oid, key) {
  state.viewMode = true;
  const r = await fetch(`/api/orders/${oid}?key=${encodeURIComponent(key)}`);
  if (!r.ok) {
    document.body.innerHTML = "<p style='padding:40px'>Заказ не найден</p>";
    return;
  }
  const o = await r.json();
  state.size = o.size;
  state.placed = o.items.map(i => ({
    uid: state.uidSeq++, side: i.side, x_mm: i.x_mm, y_mm: i.y_mm, rotation: i.rotation,
    s: { id: i.sticker_id, name: i.name, file: i.file, width_mm: i.width_mm, height_mm: i.height_mm },
  }));
  document.querySelector(".controls .sizes").remove();
  document.querySelector(".bottom").remove();
  $("hint").textContent = `Заказ №${o.id} · размер ${o.size} · ${o.price} ₽ — раскладка как её собрал клиент.`;
  layout(); renderAll(); hideBoot();
}

/* Остатки живые: пока человек собирает футболку, принт мог разобрать
   кто-то другой. Переспрашиваем каталог и помечаем то, чего уже нет. */
async function refreshStock() {
  if (state.viewMode) return;
  let list;
  try {
    list = await fetch("/api/stickers").then(r => r.json());
  } catch (e) { return; }
  if (!Array.isArray(list)) return;
  state.stickers = list;
  const byId = new Map(list.map(s => [s.id, s]));
  for (const p of state.placed) {
    const fresh = byId.get(p.s.id);
    if (fresh) p.s = { ...p.s, stock: fresh.stock, active: fresh.active };
  }
  const wasGone = new Set(state.gone);
  markGone();
  for (const id of state.gone) {
    if (!wasGone.has(id)) {
      const p = state.placed.find(x => x.s.id === id);
      toast(`«${p ? p.s.name : id}» разобрали — замени принт`, { warn: true });
      haptic("heavy");
      break;
    }
  }
  if (!$("sheet").classList.contains("hidden")) renderCatalog();
  renderAll();
}

function markGone() {
  const need = {};
  for (const p of state.placed) need[p.s.id] = (need[p.s.id] || 0) + 1;
  state.gone = new Set();
  for (const p of state.placed) {
    const stock = p.s.stock == null ? Infinity : p.s.stock;
    if (need[p.s.id] > stock || p.s.active === 0) state.gone.add(p.s.id);
  }
}

/* ---------- Размеры ---------- */

function renderSizes() {
  const el = $("sizes"); el.innerHTML = "";
  for (const [s, v] of Object.entries(state.cfg.sizes)) {
    const b = document.createElement("button");
    // Количества покупателю не показываем: нет в наличии — просто серый чип.
    b.className = "size-chip" + (s === state.size ? " active" : "")
      + (wontFit(s).length ? " tight" : "");
    b.disabled = v.stock <= 0;
    b.textContent = s;
    b.onclick = () => setSize(s);
    el.appendChild(b);
  }
}

/* Что из уже разложенного не влезет на другой размер */
function wontFit(size) {
  if (size === state.size) return [];
  const zs = state.cfg.sizes[size]?.zones;
  if (!zs) return [];
  return state.placed.filter(p => {
    const z = zs[p.side];
    return !z || p.s.width_mm > z.w_mm || p.s.height_mm > z.h_mm;
  });
}

function setSize(s) {
  if (s === state.size) return;
  const dropped = wontFit(s);
  state.size = s;
  if (dropped.length) {
    const uids = new Set(dropped.map(p => p.uid));
    state.placed = state.placed.filter(p => !uids.has(p.uid));
    toast(`На размере ${s} не помещается: ` +
      dropped.map(p => p.s.name).join(", "), { warn: true });
  }
  state.placed.forEach(clamp);
  renderSizes(); layout(); renderAll(); renderCatalog(); haptic();
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
  el.innerHTML = `<div class="zone-center"></div>
    <div class="zone-tag">зона ${Math.round(z.w_mm)}×${Math.round(z.h_mm)} мм</div>`;
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

/* Габарит, которым принт держится в зоне. Поворот его не меняет: на углах,
   кратных 90°, картинка честно совпадает с прямоугольником, на остальных
   углы принта выходят за пунктир — это разрешено сознательно, иначе
   свободный поворот упирался бы в рамку на каждом шаге. */
function halfDims(p) {
  return [p.s.width_mm / 2, p.s.height_mm / 2];
}

function fitsZone(s, side) {
  const z = zoneOf(side);
  return s.width_mm <= z.w_mm && s.height_mm <= z.h_mm;
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

/* ---------- Рендер принтов ---------- */

function renderAll() {
  for (const side of visibleSides()) {
    const zEl = state.zoneEls[side];
    if (!zEl) continue;
    zEl.querySelectorAll(".sticker, .zone-ghost").forEach(e => e.remove());
    const z = zoneOf(side);
    const mine = state.placed.filter(p => p.side === side);

    if (!mine.length && !state.viewMode && side === state.target) {
      const ghost = document.createElement("div");
      ghost.className = "zone-ghost";
      ghost.innerHTML = "<b>+</b><span>добавить принт</span>";
      ghost.onclick = openSheet;
      zEl.appendChild(ghost);
    }

    for (const p of mine) {
      const d = document.createElement("div");
      d.className = "sticker";
      d.dataset.uid = p.uid;
      d.style.width = p.s.width_mm * state.ppm + "px";
      d.style.height = p.s.height_mm * state.ppm + "px";
      place(d, p);
      d.innerHTML = `<img src="/stickers/${p.s.file}" draggable="false">
        <div class="dim">${p.s.width_mm}×${p.s.height_mm} мм</div>
        <div class="rot-handle">⟲</div>`;
      if (!state.viewMode) attachDrag(d, p);
      zEl.appendChild(d);
    }
  }
  refreshFlags();
}

function place(el, p) {
  const z = zoneOf(p.side);
  el.style.left = (z.w_mm / 2 + p.x_mm) * state.ppm + "px";
  el.style.top = p.y_mm * state.ppm + "px";
  el.style.transform = `translate(-50%,-50%) rotate(${p.rotation}deg)`;
}

function refreshFlags() {
  const bad = new Set();
  const ps = state.placed;
  for (let i = 0; i < ps.length; i++)
    for (let j = i + 1; j < ps.length; j++)
      if (tooClose(ps[i], ps[j])) { bad.add(ps[i].uid); bad.add(ps[j].uid); }
  document.querySelectorAll(".sticker").forEach(el => {
    const uid = +el.dataset.uid;
    const p = state.placed.find(x => x.uid === uid);
    el.classList.toggle("overlap", bad.has(uid));
    el.classList.toggle("gone", !!p && state.gone.has(p.s.id));
    el.classList.toggle("selected", state.sel === uid && !bad.has(uid));
  });
  $("selTools").classList.toggle("on", state.sel != null && !state.viewMode && !state.preview);
  updateTele();

  const hint = $("hint");
  if (state.viewMode) { /* подпись в режиме просмотра ставится один раз */ }
  else if (state.gone.size) {
    hint.textContent = "Перечёркнутый принт разобрали, пока ты собирал. Убери его или выбери другой.";
    hint.className = "hint warn";
  } else if (bad.size) {
    hint.textContent = `Между принтами нужно ${state.cfg.min_gap_mm} мм — ` +
      "иначе пресс заденет соседний. Раздвинь их.";
    hint.className = "hint warn";
  } else {
    hint.textContent = state.view === "sleeves"
      ? `Кладём на ${SIDE_NAMES[state.target]} — тапни другой, чтобы переключить. ` +
        "Левый и правый — как на человеке."
      : "Тапни принт, чтобы повернуть или убрать. Уголок ⟲ — свободный поворот.";
    hint.className = "hint";
  }
  if (!state.viewMode) updateBar();
}

/* Телеметрия выбранного принта: где именно он лежит, в миллиметрах */
function updateTele() {
  const el = $("tele");
  const p = state.placed.find(x => x.uid === state.sel);
  if (!p || state.viewMode || state.preview) { el.classList.remove("on"); return; }
  const z = zoneOf(p.side);
  const dx = Math.round(p.x_mm), dy = Math.round(p.y_mm);
  const axis = p.side.startsWith("sleeve")
    ? (dx === 0 ? "по центру" : `${Math.abs(dx)} мм ${dx > 0 ? "к спине" : "к переду"}`)
    : (dx === 0 ? "по центру" : `${Math.abs(dx)} мм ${dx > 0 ? "вправо" : "влево"}`);
  const from = p.side.startsWith("sleeve") ? "от проймы" : "от верха зоны";
  const rot = p.rotation ? ` · ${Math.round(p.rotation)}°` : "";
  el.textContent = `${p.s.width_mm}×${p.s.height_mm} · ${axis} · ${dy} ${from}${rot}` +
    ` · зона ${Math.round(z.w_mm)}×${Math.round(z.h_mm)}`;
  el.classList.add("on");
}

/* ---------- Перетаскивание и поворот ---------- */

function normAngle(a) { return ((a % 360) + 360) % 360; }

/* Магнит к 0/90/180/270 — чтобы «ровно» получалось само */
function snapAngle(a) {
  a = normAngle(a);
  for (const t of [0, 90, 180, 270, 360]) {
    if (Math.abs(a - t) <= SNAP_DEG) return normAngle(t);
  }
  return Math.round(a);
}

function centerOf(el) {
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

function attachDrag(el, p) {
  const pts = new Map();
  let mode = null;               // move | rotate2
  let startX, startY, sx, sy, snappedX = false;
  let baseRot = 0, baseAng = 0, snappedRot = null;

  const pairAngle = () => {
    const [a, b] = [...pts.values()];
    return Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI;
  };

  el.addEventListener("pointerdown", (e) => {
    if (e.target.classList.contains("rot-handle")) return;   // у ручки свой обработчик
    e.preventDefault();
    el.setPointerCapture(e.pointerId);
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    state.sel = p.uid;
    if (state.target !== p.side) { state.target = p.side; layoutRefresh(); return; }

    if (pts.size >= 2) {
      mode = "rotate2";
      baseRot = p.rotation; baseAng = pairAngle(); snappedRot = null;
    } else {
      mode = "move";
      startX = e.clientX; startY = e.clientY; sx = p.x_mm; sy = p.y_mm;
      document.body.classList.add("dragging");
    }
    refreshFlags();
  });

  el.addEventListener("pointermove", (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (mode === "rotate2" && pts.size >= 2) {
      const next = snapAngle(baseRot + (pairAngle() - baseAng));
      if (next % 90 === 0 && snappedRot !== next) { haptic("medium"); snappedRot = next; }
      if (next % 90 !== 0) snappedRot = null;
      p.rotation = next;
      place(el, p); clamp(p); place(el, p); updateTele();
      return;
    }
    if (mode !== "move" || startX == null) return;

    p.x_mm = sx + (e.clientX - startX) / state.ppm;
    p.y_mm = sy + (e.clientY - startY) / state.ppm;
    if (Math.abs(p.x_mm) < 4) {                 // снап к центральной оси
      p.x_mm = 0;
      if (!snappedX) { haptic("medium"); snappedX = true; }
    } else snappedX = false;
    litCenter(p.side, p.x_mm === 0);
    clamp(p);
    place(el, p);
    refreshFlags();
  });

  const end = (e) => {
    pts.delete(e.pointerId);
    if (pts.size === 0) {
      mode = null; startX = null;
      document.body.classList.remove("dragging");
      litCenter(p.side, false);
    } else if (mode === "rotate2") {
      mode = null;
    }
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);

  // Ручка в углу: свободный поворот одним пальцем
  const handle = el.querySelector(".rot-handle");
  if (handle) attachRotate(handle, el, p);
}

function attachRotate(handle, el, p) {
  let active = false, baseRot = 0, baseAng = 0, snapped = null;
  const angTo = (e) => {
    const c = centerOf(el);
    return Math.atan2(e.clientY - c.y, e.clientX - c.x) * 180 / Math.PI;
  };
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault(); e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
    active = true; baseRot = p.rotation; baseAng = angTo(e); snapped = null;
    state.sel = p.uid;
    document.body.classList.add("dragging");
    refreshFlags();
  });
  handle.addEventListener("pointermove", (e) => {
    if (!active) return;
    e.stopPropagation();
    const next = snapAngle(baseRot + (angTo(e) - baseAng));
    if (next % 90 === 0 && snapped !== next) { haptic("medium"); snapped = next; }
    if (next % 90 !== 0) snapped = null;
    p.rotation = next;
    clamp(p); place(el, p); updateTele();
  });
  const stop = (e) => {
    if (!active) return;
    e.stopPropagation();
    active = false;
    document.body.classList.remove("dragging");
    refreshFlags();
  };
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
}

function litCenter(side, on) {
  const z = state.zoneEls[side];
  z?.querySelector(".zone-center")?.classList.toggle("lit", !!on);
}

/* ---------- Каталог ---------- */

function usedCount(sid) { return state.placed.filter(p => p.s.id === sid).length; }

function openSheet() {
  renderCatalog();
  show($("sheet")); show($("scrim"));
  pushNav(closeSheet);
}

function closeSheet() {
  dropNav(closeSheet);
  hide($("sheet")); hide($("scrim"));
}

function renderCatalog() {
  const el = $("catalog"); el.innerHTML = "";
  const side = state.target;
  $("sheetSide").textContent = `на ${SIDE_NAMES[side]}, размеры реальные`;

  let list = state.stickers.filter(s => fitsZone(s, side));
  const q = state.catQuery.trim().toLowerCase();
  if (q) list = list.filter(s => String(s.name).toLowerCase().includes(q));
  if (state.catBySize) {
    list = [...list].sort((a, b) =>
      (b.width_mm * b.height_mm) - (a.width_mm * a.height_mm));
  }

  if (!list.length) {
    el.innerHTML = `<div class="empty">${q ? "Ничего не нашлось." :
      `В зону ${SIDE_NAMES[side]} (${Math.round(zoneOf(side).w_mm)}×` +
      `${Math.round(zoneOf(side).h_mm)} мм) ни один принт не помещается.`}</div>`;
    return;
  }

  // Превью в общем масштабе: крупный принт и выглядит крупнее мелкого
  const maxW = Math.max(...list.map(s => s.width_mm));
  const maxH = Math.max(...list.map(s => s.height_mm));
  const k = Math.min(88 / maxW, 72 / maxH);

  for (const s of list) {
    const left = (s.stock == null ? Infinity : s.stock) - usedCount(s.id);
    const out = left <= 0;
    const d = document.createElement("div");
    d.className = "cat-item" + (out ? " out" : "");
    d.innerHTML = `<div class="ph"><img src="/stickers/${s.file}"
        style="width:${Math.max(10, s.width_mm * k)}px;height:${Math.max(10, s.height_mm * k)}px"></div>
      <div class="nm">${s.name}</div>
      <div class="sz">${s.width_mm}×${s.height_mm} мм</div>
      ${out ? '<div class="soldout">SOLD OUT</div>' : ""}`;
    if (!out) d.onclick = () => addSticker(s);
    el.appendChild(d);
  }
}

function addSticker(s) {
  if (state.placed.length >= state.cfg.max_prints) {
    toast(`Максимум ${state.cfg.max_prints} принтов на футболку`, { warn: true });
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
  markGone();
  // Лист закрываем сразу: человек должен увидеть, куда лёг принт.
  closeSheet();
  renderAll(); renderCatalog(); renderSizes(); haptic();
  toast(`«${s.name}» на ${SIDE_NAMES[side]}`, { ms: 2000 });
}

function removeSelected() {
  const p = state.placed.find(x => x.uid === state.sel);
  if (!p) return;
  const idx = state.placed.indexOf(p);
  state.placed.splice(idx, 1);
  state.sel = null;
  markGone();
  renderAll(); renderCatalog(); renderSizes(); haptic();
  toast(`Убрали «${p.s.name}»`, {
    action: {
      label: "Вернуть",
      fn: () => {
        state.placed.splice(Math.min(idx, state.placed.length), 0, p);
        state.sel = p.uid;
        markGone();
        renderAll(); renderCatalog(); renderSizes(); haptic();
      },
    },
  });
}

/* ---------- Панель и цена ---------- */

function goodsPrice() {
  const n = state.placed.length;
  const extra = Math.max(0, n - state.cfg.included_prints);
  return state.cfg.base_price + extra * state.cfg.extra_print_price;
}

function totalPrice() { return goodsPrice() + (state.delivery.price || 0); }

function updateBar() {
  const n = state.placed.length, c = state.cfg;
  $("price").innerHTML = n
    ? `${goodsPrice()} ₽<small>${n} принт(а) · ${c.included_prints} включено</small>`
    : `${c.base_price} ₽<small>${c.included_prints} принта включено</small>`;
  $("btnOrder").disabled = n === 0 || anyTooClose() || state.gone.size > 0;
}

/* ---------- Оформление ---------- */

function openCheckout() {
  // Пока доставка выключена, второй экран не нужен: способ получения один.
  if (!state.cfg.delivery?.cdek) return submitOrder();
  show($("checkout"));
  pushNav(closeCheckout);
  renderCheckout();
}

function closeCheckout() { dropNav(closeCheckout); hide($("checkout")); }

function setMethod(m) {
  if (state.delivery.method === m) return;
  state.delivery.method = m;
  state.delivery.price = 0; state.delivery.period = ""; state.delivery.error = "";
  renderCheckout();
  if (m !== "pickup" && state.delivery.city) recalcDelivery();
  haptic();
}

function renderCheckout() {
  const d = state.delivery;
  $("coGoods").textContent = `товар ${goodsPrice()} ₽`;
  const body = $("coBody");
  body.innerHTML = "";

  // способ получения
  const g1 = document.createElement("div");
  g1.className = "co-group";
  g1.innerHTML = '<div class="co-label">Как получишь</div>';
  const methods = document.createElement("div");
  methods.className = "co-methods";
  const opts = [
    ["pickup", "Самовывоз", "Бесплатно, в поп-апе"],
    ["cdek_pvz", "СДЭК, пункт выдачи", "Заберёшь в ближайшем ПВЗ"],
    ["cdek_door", "СДЭК, курьер", "Привезут по адресу"],
  ];
  for (const [m, title, sub] of opts) {
    const b = document.createElement("button");
    b.className = "co-method" + (d.method === m ? " active" : "");
    b.innerHTML = `<div>${title}</div><small>${sub}</small>`;
    b.onclick = () => setMethod(m);
    methods.appendChild(b);
  }
  g1.appendChild(methods);
  body.appendChild(g1);

  if (d.method === "pickup") {
    const g = document.createElement("div");
    g.className = "co-group";
    g.innerHTML = `<div class="co-label">Где забрать</div>
      <div class="co-note">${state.cfg.pickup_text}<br><br>
      Назови номер заказа на кассе. Храним ${state.cfg.pickup_hold_days} дней.</div>`;
    body.appendChild(g);
  } else {
    const g = document.createElement("div");
    g.className = "co-group";
    g.innerHTML = '<div class="co-label">Получатель</div>';

    const fName = field("text", "Имя и фамилия", d.name, v => { d.name = v; updateCheckoutInfo(); });
    const fPhone = field("tel", "+7 900 123-45-67", d.phone, v => { d.phone = v; updateCheckoutInfo(); });
    g.appendChild(fName); g.appendChild(fPhone);

    const city = document.createElement("button");
    city.className = "co-pickbtn"; city.id = "coCity";
    city.onclick = openCityPicker;
    g.appendChild(wrapField(city));

    if (d.method === "cdek_pvz") {
      const pt = document.createElement("button");
      pt.className = "co-pickbtn"; pt.id = "coPoint";
      pt.onclick = openPointPicker;
      g.appendChild(wrapField(pt));
    } else {
      const addr = document.createElement("textarea");
      addr.rows = 2; addr.placeholder = "Улица, дом, квартира";
      addr.value = d.address;
      addr.oninput = () => { d.address = addr.value; updateCheckoutInfo(); };
      g.appendChild(wrapField(addr));
    }

    const calc = document.createElement("div");
    calc.className = "co-calc"; calc.id = "coCalc";
    g.appendChild(calc);
    body.appendChild(g);
  }

  updateCheckoutInfo();
}

function wrapField(el) {
  const w = document.createElement("div");
  w.className = "co-field";
  w.appendChild(el);
  return w;
}

function field(type, placeholder, value, onInput) {
  const inp = document.createElement("input");
  inp.type = type; inp.placeholder = placeholder; inp.value = value || "";
  inp.oninput = () => onInput(inp.value);
  return wrapField(inp);
}

function updateCheckoutInfo() {
  const d = state.delivery;
  const cityBtn = $("coCity");
  if (cityBtn) {
    cityBtn.className = "co-pickbtn" + (d.city ? "" : " empty");
    cityBtn.innerHTML = d.city
      ? `${d.city.city}<small>${d.city.region || "город доставки"}</small>`
      : "Выбрать город";
  }
  const ptBtn = $("coPoint");
  if (ptBtn) {
    ptBtn.className = "co-pickbtn" + (d.point ? "" : " empty");
    ptBtn.innerHTML = d.point
      ? `${d.point.address}<small>${d.point.work_time || d.point.code}</small>`
      : "Выбрать пункт выдачи";
    ptBtn.disabled = !d.city;
  }
  const calc = $("coCalc");
  if (calc) {
    if (d.calcing) { calc.className = "co-calc load"; calc.textContent = "считаем доставку"; }
    else if (d.error) { calc.className = "co-calc err"; calc.textContent = d.error; }
    else if (!d.city) { calc.className = "co-calc"; calc.textContent = ""; }
    else if (d.price === 0) { calc.className = "co-calc"; calc.textContent = `доставка бесплатно${d.period ? ", " + d.period : ""}`; }
    else { calc.className = "co-calc"; calc.textContent = `доставка ${d.price} ₽${d.period ? ", " + d.period : ""}`; }
  }

  const parts = [`товар ${goodsPrice()} ₽`];
  if (d.method !== "pickup" && d.city && !d.error) parts.push(`доставка ${d.price} ₽`);
  $("coTotal").innerHTML = `${totalPrice()} ₽<small>${parts.join(" + ")}</small>`;
  $("coSubmit").disabled = !checkoutReady();
}

function checkoutReady() {
  const d = state.delivery;
  if (state.gone.size) return false;
  if (d.method === "pickup") return true;
  if (d.calcing || d.error) return false;
  if (d.name.trim().length < 2) return false;
  if (d.phone.replace(/\D/g, "").length < 10) return false;
  if (!d.city) return false;
  if (d.method === "cdek_pvz") return !!d.point;
  return d.address.trim().length >= 5;
}

async function recalcDelivery() {
  const d = state.delivery;
  if (d.method === "pickup" || !d.city) return;
  d.calcing = true; d.error = ""; updateCheckoutInfo();
  try {
    const r = await fetch("/api/delivery/calc", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...initHeaders() },
      body: JSON.stringify({ method: d.method, city_code: d.city.code,
                             items: state.placed.length }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "не посчиталось");
    d.price = data.price;
    d.period = (data.period_min != null && data.period_max != null)
      ? `${data.period_min}–${data.period_max} дн.` : "";
  } catch (e) {
    d.price = 0; d.period = "";
    d.error = String(e.message || e);
  } finally {
    d.calcing = false; updateCheckoutInfo();
  }
}

/* ---------- Выбиральщик: города и пункты выдачи ---------- */

let pickTimer = null;
let pickPoints = [];   // кэш ПВЗ текущего города

function openPicker(title, placeholder, onInput) {
  show($("pick"));
  pushNav(closePicker);
  $("pickTitle").textContent = title;
  const inp = $("pickInput");
  inp.value = ""; inp.placeholder = placeholder;
  inp.oninput = () => {
    clearTimeout(pickTimer);
    pickTimer = setTimeout(() => onInput(inp.value), 300);
  };
  $("pickList").innerHTML = "";
  setTimeout(() => inp.focus(), 120);
}

function closePicker() {
  dropNav(closePicker);
  hide($("pick"));
  clearTimeout(pickTimer);
}

function pickMessage(text) {
  $("pickList").innerHTML = `<div class="pick-empty">${text}</div>`;
}

function renderPickList(items, render, onPick) {
  const list = $("pickList");
  list.innerHTML = "";
  if (!items.length) return pickMessage("Ничего не нашлось");
  for (const it of items) {
    const d = document.createElement("div");
    d.className = "pick-item";
    d.innerHTML = render(it);
    d.onclick = (e) => {
      if (e.target.classList.contains("m")) return;   // ссылка на карту
      onPick(it);
    };
    list.appendChild(d);
  }
}

function openCityPicker() {
  openPicker("Город доставки", "Начни вводить название", async (q) => {
    if (q.trim().length < 2) return pickMessage("Введи хотя бы две буквы");
    pickMessage("Ищем…");
    try {
      const r = await fetch(`/api/delivery/cities?q=${encodeURIComponent(q)}`);
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || "СДЭК не отвечает");
      renderPickList(data,
        c => `<div class="t">${c.city}</div><div class="s">${c.region || ""}</div>`,
        c => {
          state.delivery.city = c;
          state.delivery.point = null;
          pickPoints = [];
          closePicker(); haptic();
          updateCheckoutInfo();
          recalcDelivery();
        });
    } catch (e) { pickMessage(String(e.message || e)); }
  });
  pickMessage("Введи название города");
}

async function openPointPicker() {
  const d = state.delivery;
  if (!d.city) return;
  openPicker(`Пункты выдачи, ${d.city.city}`, "Поиск по адресу", (q) => showPoints(q));
  pickMessage("Загружаем пункты…");
  if (!pickPoints.length) {
    try {
      const r = await fetch(`/api/delivery/points?city_code=${d.city.code}`);
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || "СДЭК не отвечает");
      pickPoints = data;
    } catch (e) { return pickMessage(String(e.message || e)); }
  }
  showPoints("");
}

function showPoints(q) {
  const needle = (q || "").trim().toLowerCase();
  const list = needle
    ? pickPoints.filter(p => (p.address + " " + p.name).toLowerCase().includes(needle))
    : pickPoints;
  if (!pickPoints.length) return pickMessage("В этом городе пунктов выдачи нет — выбери курьера");
  renderPickList(list.slice(0, 120),
    p => `<div class="t">${p.address}</div>
          <div class="s">${p.work_time || ""}</div>
          ${p.lat && p.lon ? `<span class="m" data-lat="${p.lat}" data-lon="${p.lon}">на карте</span>` : ""}`,
    p => {
      state.delivery.point = p;
      closePicker(); haptic(); updateCheckoutInfo();
    });
  // «на карте» открываем во внешнем браузере, внутри мини-аппа карта не нужна
  $("pickList").querySelectorAll(".m").forEach(el => {
    el.onclick = (e) => {
      e.stopPropagation();
      const url = `https://yandex.ru/maps/?pt=${el.dataset.lon},${el.dataset.lat}&z=17&l=map`;
      tg?.openLink ? tg.openLink(url) : window.open(url, "_blank");
    };
  });
}

/* ---------- Отправка заказа ---------- */

function deliveryPayload() {
  const d = state.delivery;
  if (d.method === "pickup") return { method: "pickup" };
  return {
    method: d.method,
    name: d.name.trim(),
    phone: d.phone.trim(),
    city_code: d.city.code,
    city_name: d.city.city,
    point_code: d.method === "cdek_pvz" ? d.point.code : null,
    address: d.method === "cdek_door" ? d.address.trim() : null,
  };
}

async function submitOrder() {
  const btn = state.cfg.delivery?.cdek ? $("coSubmit") : $("btnOrder");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "…";
  const body = {
    size: state.size,
    items: state.placed.map(p => ({
      side: p.side, sticker_id: p.s.id,
      x_mm: +p.x_mm.toFixed(1), y_mm: +p.y_mm.toFixed(1),
      rotation: Math.round(p.rotation) % 360,
    })),
    delivery: deliveryPayload(),
  };
  let r, data;
  try {
    r = await fetch("/api/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...initHeaders() },
      body: JSON.stringify(body),
    });
    data = await r.json().catch(() => ({}));
  } catch (e) {
    toast("Сеть подвела. Попробуй ещё раз.", { warn: true });
    btn.disabled = false; btn.textContent = label;
    return;
  }
  if (!r.ok) {
    tg?.showAlert?.(data.detail || "Не получилось создать заказ");
    btn.disabled = false; btn.textContent = label;
    refreshStock();
    return;
  }
  haptic("heavy");
  btn.disabled = false; btn.textContent = label;
  closeCheckout();
  showSuccess(data);
}

function showSuccess(data) {
  const hold = data.hold_minutes
    ? `<br><br>Ссылка ждёт ${data.hold_minutes} минут — потом принты вернутся в каталог.`
    : "";
  const where = data.delivery_method === "pickup"
    ? "Заберёшь в поп-апе — пришлём адрес, когда будет готово."
    : `Доставка: ${METHOD_NAMES[data.delivery_method]}. Трек-номер пришлём в чат.`;
  const ov = $("overlay");
  ov.innerHTML = `
    <h2>Заказ принят</h2>
    <div class="num">№${data.order_id}</div>
    <p>Сумма ${data.price} ₽${data.delivery_price ? ` (с доставкой ${data.delivery_price} ₽)` : ""}.
    ${data.pay_url ? "Ссылка на оплату — в чате с ботом." : "Реквизиты для оплаты придут в чат с ботом."}${hold}<br><br>
    ${where}</p>
    <div class="acts">
      ${data.pay_url ? '<button class="order-btn" id="ovPay">Оплатить</button>' : ""}
      <button class="ghost-btn" id="ovClose">Закрыть</button>
    </div>`;
  show(ov);
  const pay = $("ovPay");
  if (pay) pay.onclick = () => {
    tg?.openLink ? tg.openLink(data.pay_url) : window.open(data.pay_url, "_blank");
  };
  $("ovClose").onclick = () => tg?.close?.();
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

$("btnPreview").onclick = () => {
  state.preview = !state.preview;
  document.body.classList.toggle("preview", state.preview);
  $("btnPreview").classList.toggle("on", state.preview);
  $("btnPreview").textContent = state.preview ? "Показать разметку" : "Как выглядит";
  if (state.preview) state.sel = null;
  renderAll(); haptic();
};

$("btnCatalog").onclick = openSheet;
$("btnCloseSheet").onclick = closeSheet;
$("scrim").onclick = closeSheet;

$("catSearch").oninput = (e) => { state.catQuery = e.target.value; renderCatalog(); };
$("btnSort").onclick = () => {
  state.catBySize = !state.catBySize;
  $("btnSort").classList.toggle("on", state.catBySize);
  $("btnSort").textContent = state.catBySize ? "по размеру ✓" : "по размеру";
  renderCatalog();
};

$("btnRotate").onclick = () => {
  const p = state.placed.find(p => p.uid === state.sel); if (!p) return;
  p.rotation = normAngle(Math.round(p.rotation / 90) * 90 + 90);
  clamp(p); renderAll(); haptic();
};
$("btnDelete").onclick = removeSelected;
$("btnOrder").onclick = openCheckout;
$("coBack").onclick = closeCheckout;
$("coSubmit").onclick = submitOrder;
$("pickBack").onclick = closePicker;
window.addEventListener("resize", () => { layout(); renderAll(); });

/* Пока тащим принт, страница не должна прокручиваться: именно эта прокрутка
   на телефоне и превращается в жест «свернуть мини-апп». Каталога и экрана
   оформления не касается — класс висит только во время перетаскивания. */
document.addEventListener("touchmove", (e) => {
  if (document.body.classList.contains("dragging")) e.preventDefault();
}, { passive: false });

boot();
