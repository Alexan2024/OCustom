"""Бот: точка входа для покупателя + рабочее место сотрудника (карточки заказов)."""
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, WebAppInfo)

from . import config, db

log = logging.getLogger("bot")

bot = Bot(config.BOT_TOKEN)
dp = Dispatcher()

STATUS_LABELS = {
    "new": "🕐 Ожидает оплаты",
    "paid": "✅ Оплачен, в очереди",
    "in_progress": "🔥 Запечатывается",
    "ready": "📦 Готов к выдаче",
    "done": "🤝 Выдан",
    "cancelled": "❌ Отменён",
}

# Переходы, доступные сотруднику из каждого статуса
STAFF_FLOW = {
    "new": [("paid", "Оплачен ✓"), ("cancelled", "Отменить ✕")],
    "paid": [("in_progress", "Взять в работу"), ("cancelled", "Отменить ✕")],
    "in_progress": [("ready", "Готово 📦")],
    "ready": [("done", "Выдан 🤝")],
}


def webapp_button(text="Собрать футболку 👕", path="/webapp/"):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, web_app=WebAppInfo(url=config.WEBAPP_URL + path))
    ]])


@dp.message(Command("chatid"))
async def chat_id(m: Message):
    """Отправь эту команду в группе сотрудников — бот пришлёт ID этой группы."""
    await m.answer(
        f"ID этого чата:\n\n<code>{m.chat.id}</code>\n\n"
        "Скопируй его в переменную STAFF_CHAT_ID на Railway.",
        parse_mode="HTML",
    )


@dp.message(CommandStart())
async def start(m: Message):
    left = db.quota_left()
    quota_line = (
        f"На сегодня осталось мест: {left}."
        if left > 0 else "На сегодня места закончились — заказ уедет на завтра."
    )
    await m.answer(
        "Привет! Это кастом-станция AMANG.\n\n"
        "Собери свою футболку: выбери принты, расставь их как хочешь — "
        "мы запечатаем, а ты просто заберёшь готовую.\n\n"
        f"📍 {config.PICKUP_TEXT}\n"
        f"⚡ {quota_line}\n"
        f"🕐 Храним готовый заказ {config.PICKUP_HOLD_DAYS} дней.",
        reply_markup=webapp_button(),
    )


# ---------- Карточка заказа в чате сотрудников ----------

def order_card_text(o: dict) -> str:
    lines = [
        f"🧾 Заказ №{o['id']} — {STATUS_LABELS.get(o['status'], o['status'])}",
        f"Клиент: {o['first_name'] or ''} @{o['username'] or '—'} (id {o['user_id']})",
        f"Размер: {o['size']}  |  Сумма: {o['price']} ₽",
        "",
    ]
    for side, title in (("front", "ПЕРЕД"), ("back", "СПИНА")):
        items = [i for i in o["items"] if i["side"] == side]
        if not items:
            continue
        lines.append(f"— {title} —")
        for i in items:
            if abs(i["x_mm"]) < 0.5:
                dx = "по центру"
            else:
                dx = f"{abs(i['x_mm']):.0f} мм {'правее' if i['x_mm'] > 0 else 'левее'} центра"
            rot = f", поворот {i['rotation']}°" if i["rotation"] else ""
            lines.append(
                f"  • «{i['name']}» ({i['width_mm']:.0f}×{i['height_mm']:.0f} мм): "
                f"центр {dx}, {i['y_mm']:.0f} мм от верха зоны{rot}"
            )
    lines.append("")
    lines.append(f"👁 Раскладка: {config.WEBAPP_URL}/webapp/?view={o['id']}&key={o['view_token']}")
    return "\n".join(lines)


def order_card_kb(o: dict) -> InlineKeyboardMarkup | None:
    steps = STAFF_FLOW.get(o["status"])
    if not steps:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=f"st:{o['id']}:{s}")] for s, t in steps
    ])


async def notify_staff_new_order(o: dict):
    if not config.STAFF_CHAT_ID:
        log.warning("STAFF_CHAT_ID не задан — карточка заказа не отправлена")
        return
    msg = await bot.send_message(
        config.STAFF_CHAT_ID, order_card_text(o), reply_markup=order_card_kb(o),
        disable_web_page_preview=True,
    )
    db.set_staff_msg(o["id"], msg.message_id)


CUSTOMER_NOTIFY = {
    "paid": "Оплата получена ✅ Заказ №{id} в очереди на запечатку.",
    "in_progress": "Заказ №{id} взяли в работу 🔥",
    "ready": ("Заказ №{id} готов! 📦 Забирай: {pickup}\n"
              "Назови номер заказа на кассе. Храним {days} дней."),
    "done": "Заказ №{id} выдан. Носи с удовольствием 🖤",
    "cancelled": "Заказ №{id} отменён. Если это ошибка — напиши нам.",
}


@dp.callback_query(F.data.startswith("st:"))
async def staff_set_status(cb: CallbackQuery):
    _, oid, new_status = cb.data.split(":")
    o = db.get_order(int(oid))
    if not o:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    allowed = [s for s, _ in STAFF_FLOW.get(o["status"], [])]
    if new_status not in allowed:
        await cb.answer("Статус уже изменён", show_alert=True)
        return
    db.set_status(o["id"], new_status, restock=(new_status == "cancelled"))
    o = db.get_order(o["id"])
    await cb.message.edit_text(
        order_card_text(o), reply_markup=order_card_kb(o), disable_web_page_preview=True
    )
    tpl = CUSTOMER_NOTIFY.get(new_status)
    if tpl:
        try:
            await bot.send_message(o["user_id"], tpl.format(
                id=o["id"], pickup=config.PICKUP_TEXT, days=config.PICKUP_HOLD_DAYS))
        except Exception as e:
            log.warning("Не удалось написать клиенту %s: %s", o["user_id"], e)
    await cb.answer("Ок")


async def notify_customer_order_created(o: dict, pay_url: str | None):
    if pay_url:
        text = (f"Заказ №{o['id']} создан! Сумма {o['price']} ₽.\n"
                f"Оплати по ссылке — после оплаты возьмём в работу:\n{pay_url}")
    else:
        text = (f"Заказ №{o['id']} создан! Сумма {o['price']} ₽.\n"
                "Сейчас напишем тебе реквизиты для оплаты. "
                "После подтверждения оплаты возьмём футболку в работу.")
    await bot.send_message(o["user_id"], text)
