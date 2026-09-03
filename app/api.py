"""HTTP API для мини-аппа + раздача статики."""
import asyncio
import logging

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, bot as tgbot, config, db, payments

log = logging.getLogger("api")
app = FastAPI(title=f"{config.BRAND} custom station")


def require_user(init_data: str | None) -> dict:
    if not init_data:
        raise HTTPException(401, "Открой мини-апп из Telegram")
    user = auth.validate_init_data(init_data)
    if not user:
        raise HTTPException(401, "Невалидная подпись Telegram")
    return user


@app.get("/api/config")
def get_config():
    return {
        "brand": config.BRAND,
        "sizes": {
            s: {**geo, "stock": config.SHIRT_STOCK.get(s, 0),
                "zones": config.zones(s)}
            for s, geo in config.SIZES.items()
        },
        "photo": config.SHIRT_PHOTO,
        "min_gap_mm": config.MIN_GAP_MM,
        "base_price": config.BASE_PRICE,
        "included_prints": config.INCLUDED_PRINTS,
        "extra_print_price": config.EXTRA_PRINT_PRICE,
        "max_prints": config.MAX_PRINTS,
        "quota_left": db.quota_left(),
        "pickup_text": config.PICKUP_TEXT,
        "pickup_hold_days": config.PICKUP_HOLD_DAYS,
        "online_payment": payments.enabled(),
    }


@app.get("/api/stickers")
def get_stickers():
    return db.active_stickers()


class Item(BaseModel):
    side: str = Field(pattern="^(front|back|sleeve_l|sleeve_r)$")
    sticker_id: int
    x_mm: float
    y_mm: float
    rotation: int = 0


class NewOrder(BaseModel):
    size: str
    items: list[Item] = Field(min_length=1)


def calc_price(n_items: int) -> int:
    extra = max(0, n_items - config.INCLUDED_PRINTS)
    return config.BASE_PRICE + extra * config.EXTRA_PRINT_PRICE


def validate_geometry(size: str, items: list[Item], stickers: dict):
    """Каждый принт должен лежать в своей зоне и не подходить к соседу
    ближе, чем на MIN_GAP_MM: пресс жмёт наклейки по одной, и край платена
    не должен накрыть уже запечатанный принт."""
    boxes: dict[str, list] = {s: [] for s in config.SIDES}
    gap = config.MIN_GAP_MM
    for it in items:
        s = stickers.get(it.sticker_id)
        if not s:
            raise HTTPException(400, f"Стикер {it.sticker_id} не найден")
        z = config.zone(size, it.side)
        w, h = s["width_mm"], s["height_mm"]
        if it.rotation not in (0, 90, 180, 270):
            raise HTTPException(400, "Поворот только 0/90/180/270")
        if it.rotation in (90, 270):
            w, h = h, w
        if w > z["w_mm"] + 0.5 or h > z["h_mm"] + 0.5:
            raise HTTPException(
                400, f"«{s['name']}» не помещается в зону "
                     f"({z['w_mm']:.0f}×{z['h_mm']:.0f} мм)")
        half_w, half_h = w / 2, h / 2
        # внутри печатной зоны
        if (abs(it.x_mm) + half_w > z["w_mm"] / 2 + 0.5
                or it.y_mm - half_h < -0.5
                or it.y_mm + half_h > z["h_mm"] + 0.5):
            raise HTTPException(400, f"«{s['name']}» выходит за печатную зону")
        # просвет до соседей на той же стороне
        box = (it.x_mm - half_w, it.y_mm - half_h, it.x_mm + half_w, it.y_mm + half_h)
        for b in boxes[it.side]:
            if (box[0] < b[2] + gap and box[2] > b[0] - gap
                    and box[1] < b[3] + gap and box[3] > b[1] - gap):
                raise HTTPException(
                    400, f"Между принтами нужно хотя бы {gap} мм — иначе пресс "
                         "заденет соседний")
        boxes[it.side].append(box)


@app.post("/api/orders")
async def create_order(body: NewOrder,
                       x_telegram_init_data: str | None = Header(default=None)):
    user = require_user(x_telegram_init_data)
    if body.size not in config.SIZES:
        raise HTTPException(400, "Неизвестный размер")
    if config.SHIRT_STOCK.get(body.size, 0) <= 0:
        raise HTTPException(400, f"Размер {body.size} закончился")
    if len(body.items) > config.MAX_PRINTS:
        raise HTTPException(400, f"Максимум {config.MAX_PRINTS} принтов")

    phone = db.get_phone(user["id"])
    # Чек по 54-ФЗ без контакта покупателя пробить нельзя.
    if payments.enabled() and config.YOOKASSA_SEND_RECEIPT and not phone:
        asyncio.create_task(tgbot.ask_phone_safe(user["id"]))
        raise HTTPException(
            400, "Для чека нужен номер телефона. Открой чат с ботом — там кнопка "
                 "«Поделиться номером», это один тап. Потом возвращайся и оформи заказ.")

    stickers = db.sticker_map({i.sticker_id for i in body.items})
    validate_geometry(body.size, body.items, stickers)
    price = calc_price(len(body.items))
    try:
        order = db.create_order(user, body.size,
                                [i.model_dump() for i in body.items], price, phone)
    except db.OrderError as e:
        raise HTTPException(409, str(e))

    pay_url = None
    try:
        created = await payments.create_payment(order)
        if created:
            pay_url, payment_id = created
            db.set_payment_id(order["id"], payment_id)
            order["payment_id"] = payment_id
    except payments.PaymentError as e:
        # Не роняем заказ: сотрудник сможет принять оплату руками.
        log.error("Не удалось создать платёж по заказу №%s: %s", order["id"], e)

    # уведомления не должны валить создание заказа
    asyncio.create_task(_notify_safe(order, pay_url))
    return {"order_id": order["id"], "price": price, "pay_url": pay_url,
            "hold_minutes": config.ORDER_HOLD_MINUTES if pay_url else 0}


async def _notify_safe(order: dict, pay_url: str | None):
    try:
        await tgbot.notify_customer_order_created(order, pay_url)
    except Exception as e:
        log.warning("notify customer failed: %s", e)
    try:
        await tgbot.notify_staff_new_order(order)
    except Exception as e:
        log.warning("notify staff failed: %s", e)


@app.get("/api/orders/{oid}")
def view_order(oid: int, key: str):
    """Просмотр раскладки заказа (ссылка из карточки сотрудника)."""
    o = db.get_order(oid)
    if not o or o["view_token"] != key:
        raise HTTPException(404, "Заказ не найден")
    return o


@app.post("/api/payments/yookassa/webhook")
async def yookassa_webhook(request: Request):
    """Уведомление от ЮKassa.

    Телу не доверяем: берём из него id платежа и сами спрашиваем у ЮKassa,
    что с ним на самом деле. Отвечаем 200 всегда, когда разобрали запрос —
    иначе ЮKassa будет долбить повторами.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad json")

    oid, payment_id = payments.order_id_from_webhook(body)
    if not payment_id:
        raise HTTPException(400, "no payment id")

    order = db.get_order(oid) if oid else None
    if not order:
        log.warning("Вебхук по неизвестному заказу: %s (платёж %s)", oid, payment_id)
        return {"ok": True}

    try:
        confirmed = await payments.confirm_payment(payment_id, order)
    except payments.PaymentError as e:
        log.error("Не удалось проверить платёж %s: %s", payment_id, e)
        raise HTTPException(503, "cannot verify")

    if not confirmed:
        return {"ok": True}

    if order["status"] == "cancelled":
        # Деньги пришли по заказу, который мы уже отменили по таймауту.
        # Молча съесть такое нельзя — зовём сотрудника.
        await tgbot.alert_staff(
            f"⚠️ Оплата {order['price']} ₽ пришла по отменённому заказу №{order['id']}.\n"
            f"Платёж {payment_id}. Нужно вернуть деньги или собрать заказ вручную.")
        return {"ok": True}

    if not db.mark_paid(order["id"], payment_id):
        # Уже отмечен оплаченным — это повторная доставка того же уведомления.
        return {"ok": True}

    order = db.get_order(order["id"])
    await tgbot.notify_customer_status(order, "paid")
    await tgbot.refresh_or_send_staff_card(order)
    return {"ok": True}


@app.get("/")
def root():
    return RedirectResponse("/webapp/")


app.mount("/stickers", StaticFiles(directory=config.STICKERS_DIR), name="stickers")
app.mount("/webapp", StaticFiles(directory=config.BASE_DIR / "webapp", html=True), name="webapp")
