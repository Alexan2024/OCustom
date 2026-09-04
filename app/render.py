"""Картинка раскладки заказа.

Нужна для карточки «Мои заказы» в боте: человек должен увидеть свою футболку,
а не читать столбик миллиметров. Рисуем то же, что показывает мини-апп, —
фото бланка с принтами, а рукава схемой (на фронтальном кадре рукав уходит
под углом, и по нему нельзя честно отмерить миллиметры).

Считаем всё один в один с webapp/app.js: масштаб берём от ширины по груди,
верх зоны привязан к линии плеча.
"""
import io
import logging

from PIL import Image, ImageDraw, ImageFont

from . import config

log = logging.getLogger("render")

PAPER = (239, 239, 239, 255)
LINE = (209, 209, 209, 255)
MUTED = (134, 130, 122, 255)
INK = (23, 22, 19, 255)

SHIRT_W_PX = 760          # ширина бланка в панели
PAD = 28                  # поля вокруг панелей
GAP = 24                  # просвет между панелями
LABEL_H = 30
MAX_TOTAL_W = 1280        # шире Telegram всё равно ужмёт

_font_cache: dict[int, ImageFont.ImageFont] = {}


def _font(size: int):
    if size in _font_cache:
        return _font_cache[size]
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ):
        try:
            f = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    else:
        f = ImageFont.load_default()
    _font_cache[size] = f
    return f


def _sticker_image(file: str) -> Image.Image | None:
    path = config.STICKERS_DIR / file
    if not path.exists():
        log.warning("Нет картинки принта %s", file)
        return None
    try:
        return Image.open(path).convert("RGBA")
    except Exception as e:
        log.warning("Не открылся принт %s: %s", file, e)
        return None


def _paste_prints(panel: Image.Image, items: list, ppm: float,
                  zone_left: float, zone_top: float, zone_w_mm: float):
    """Кладёт принты в панель. Поворот в CSS идёт по часовой стрелке,
    в PIL — против, поэтому угол берём со знаком минус."""
    for it in items:
        src = _sticker_image(it["file"])
        if src is None:
            continue
        w = max(1, int(round(it["width_mm"] * ppm)))
        h = max(1, int(round(it["height_mm"] * ppm)))
        img = src.resize((w, h), Image.LANCZOS)
        rot = int(it.get("rotation") or 0) % 360
        if rot:
            img = img.rotate(-rot, expand=True, resample=Image.BICUBIC)
        cx = zone_left + (zone_w_mm / 2 + it["x_mm"]) * ppm
        cy = zone_top + it["y_mm"] * ppm
        panel.alpha_composite(img, (int(round(cx - img.width / 2)),
                                    int(round(cy - img.height / 2))))


def _body_panel(size: str, side: str, items: list) -> Image.Image:
    ph = config.SHIRT_PHOTO
    path = config.BASE_DIR / "webapp" / f"shirt_{side}.webp"
    aspect = ph["w_px"] / ph["h_px"]
    k = config.PHOTO_STRETCH_Y or 1.0

    shirt_w = SHIRT_W_PX
    shirt_h = int(round(shirt_w / aspect * k))
    panel = Image.new("RGBA", (shirt_w, shirt_h + LABEL_H), (0, 0, 0, 0))

    try:
        shirt = Image.open(path).convert("RGBA").resize((shirt_w, shirt_h), Image.LANCZOS)
        panel.alpha_composite(shirt, (0, LABEL_H))
    except Exception as e:
        log.warning("Не открылось фото бланка %s: %s", path, e)

    chest_px = shirt_w * (ph["chest_x1"] - ph["chest_x0"]) / ph["w_px"]
    ppm = chest_px / config.SIZES[size]["chest_mm"]

    z = config.zone(size, side)
    center_x = shirt_w * ((ph["chest_x0"] + ph["chest_x1"]) / 2) / ph["w_px"]
    zone_left = center_x - z["w_mm"] * ppm / 2
    zone_top = LABEL_H + shirt_h * ph["shoulder_y"] / ph["h_px"] + z["top_mm"] * ppm

    _paste_prints(panel, items, ppm, zone_left, zone_top, z["w_mm"])

    d = ImageDraw.Draw(panel)
    d.text((0, 4), "ПЕРЕД" if side == "front" else "СПИНА",
           font=_font(19), fill=MUTED)
    return panel


def _sleeve_panel(size: str, side: str, items: list, ppm: float) -> Image.Image:
    z = config.zone(size, side)
    w = max(1, int(round(z["w_mm"] * ppm)))
    h = max(1, int(round(z["h_mm"] * ppm)))
    label = "ЛЕВЫЙ РУКАВ" if side == "sleeve_l" else "ПРАВЫЙ РУКАВ"
    font = _font(19)
    # Панель не уже подписи, иначе «ЛЕВЫЙ РУКАВ» обрежется по краю
    label_w = int(round(font.getlength(label))) if hasattr(font, "getlength") else len(label) * 10
    # Поля вокруг зоны: повёрнутый принт может высунуться за пунктир,
    # и это должно быть видно, а не обрезаться краем панели.
    m = 34
    panel_w = max(w + m * 2, label_w + 4)
    panel = Image.new("RGBA", (panel_w, h + m * 2 + LABEL_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(panel)
    x0 = (panel_w - w) // 2
    y0 = LABEL_H + m
    d.rectangle([x0 - 1, y0 - 1, x0 + w, y0 + h], outline=LINE, width=2)
    _paste_prints(panel, items, ppm, x0, y0, z["w_mm"])
    d.text((0, 4), label, font=font, fill=MUTED)
    return panel


def order_image(order: dict) -> bytes | None:
    """PNG с раскладкой заказа. None — если нечего рисовать."""
    try:
        size = order["size"]
        if size not in config.SIZES:
            return None
        by_side = {s: [i for i in order["items"] if i["side"] == s]
                   for s in config.SIDES}
        used = [s for s in config.SIDES if by_side[s]]
        if not used:
            return None

        # Единый масштаб для рукавов — тот же, что у корпуса
        ph = config.SHIRT_PHOTO
        chest_px = SHIRT_W_PX * (ph["chest_x1"] - ph["chest_x0"]) / ph["w_px"]
        ppm = chest_px / config.SIZES[size]["chest_mm"]

        panels = []
        for s in used:
            if s in config.SLEEVE_SIDES:
                panels.append(_sleeve_panel(size, s, by_side[s], ppm))
            else:
                panels.append(_body_panel(size, s, by_side[s]))

        total_w = PAD * 2 + sum(p.width for p in panels) + GAP * (len(panels) - 1)
        total_h = PAD * 2 + max(p.height for p in panels)
        canvas = Image.new("RGBA", (total_w, total_h), PAPER)

        x = PAD
        for p in panels:
            canvas.alpha_composite(p, (x, PAD + (total_h - PAD * 2 - p.height) // 2))
            x += p.width + GAP

        d = ImageDraw.Draw(canvas)
        d.text((PAD, total_h - PAD + 4),
               f"{config.BRAND} · заказ №{order['id']} · размер {size}",
               font=_font(20), fill=INK, anchor="ls")

        if canvas.width > MAX_TOTAL_W:
            ratio = MAX_TOTAL_W / canvas.width
            canvas = canvas.resize(
                (MAX_TOTAL_W, int(round(canvas.height * ratio))), Image.LANCZOS)

        buf = io.BytesIO()
        canvas.convert("RGB").save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        log.warning("Не удалось нарисовать раскладку заказа №%s: %s",
                    order.get("id"), e)
        return None
