"""HTTP API для мини-аппа + раздача статики."""
import asyncio
import logging

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, bot as tgbot, config, db, payments

log = logging.getLogger("api")
app = FastAPI(title="AMANG custom station")


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
        "sizes": {
            s: {**geo, "stock": config.SHIRT_STOCK.get(s, 0)}
            for s, geo in config.SIZES.items()
        },
        "base_price": config.BASE_PRICE,
        "included_prints": config.INCLUDED_PRINTS,
        "extra_print_price": config.EXTRA_PRINT_PRICE,
        "max_prints": config.MAX_PRINTS,
        "quota_left": db.quota_left(),
        "pickup_text": config.PICKUP_TEXT,
        "pickup_hold_days": config.PICKUP_HOLD_DAYS,
    }


@app.get("/api/stickers")
def get_stickers():
    return db.active_stickers()


class Item(BaseModel):
    side: str = Field(pattern="^(front|back)$")
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
    geo = config.SIZES[size]
    boxes = {"front": [], "back": []}
    for it in items:
        s = stickers.get(it.sticker_id)
        if not s:
            raise HTTPException(400, f"Стикер {it.sticker_id} не найден")
        w, h = s["width_mm"], s["height_mm"]
        if it.rotation not in (0, 90, 180, 270):
            raise HTTPException(400, "Поворот только 0/90/180/270")
        if it.rotation in (90, 270):
            w, h = h, w
        half_w, half_h = w / 2, h / 2
        # внутри печатной зоны
        if (abs(it.x_mm) + half_w > geo["print_w_mm"] / 2 + 0.5
                or it.y_mm - half_h < -0.5
                or it.y_mm + half_h > geo["print_h_mm"] + 0.5):
            raise HTTPException(400, f"«{s['name']}» выходит за печатную зону")
        # без пересечений (наложение = дольше запечатка, запрещено)
        box = (it.x_mm - half_w, it.y_mm - half_h, it.x_mm + half_w, it.y_mm + half_h)
        for b in boxes[it.side]:
            if box[0] < b[2] and box[2] > b[0] and box[1] < b[3] and box[3] > b[1]:
                raise HTTPException(400, "Принты не должны пересекаться")
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
    stickers = db.sticker_map({i.sticker_id for i in body.items})
    validate_geometry(body.size, body.items, stickers)
    price = calc_price(len(body.items))
    try:
        order = db.create_order(user, body.size, [i.model_dump() for i in body.items], price)
    except db.OrderError as e:
        raise HTTPException(409, str(e))
    try:
        pay_url = payments.create_payment_url(order)
    except payments.PaymentError as e:
        log.error("payment: %s", e)
        pay_url = None
    # уведомления не должны валить создание заказа
    asyncio.create_task(_notify_safe(order, pay_url))
    return {"order_id": order["id"], "price": price, "pay_url": pay_url}


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


@app.post("/api/payments/ypmn/webhook")
async def ypmn_webhook(request: Request):
    body = await request.body()
    oid = payments.verify_webhook(dict(request.headers), body)
    if oid is None:
        raise HTTPException(400, "bad signature")
    o = db.get_order(oid)
    if o and o["status"] == "new":
        db.set_status(oid, "paid")
        o = db.get_order(oid)
        await tgbot.bot.send_message(
            o["user_id"], f"Оплата получена ✅ Заказ №{oid} в очереди на запечатку.")
        await tgbot.notify_staff_new_order(o)
    return {"ok": True}


@app.get("/")
def root():
    return RedirectResponse("/webapp/")


app.mount("/stickers", StaticFiles(directory=config.STICKERS_DIR), name="stickers")
app.mount("/webapp", StaticFiles(directory=config.BASE_DIR / "webapp", html=True), name="webapp")
