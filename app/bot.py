"""Бот: точка входа для покупателя + рабочее место сотрудника (карточки заказов)."""
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)

from . import config, db, payments

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

CUSTOMER_NOTIFY = {
    "paid": "Оплата получена ✅ Заказ №{id} в очереди на запечатку.",
    "in_progress": "Заказ №{id} взяли в работу 🔥",
    "ready": ("Заказ №{id} готов! 📦 Забирай: {pickup}\n"
              "Назови номер заказа на кассе. Храним {days} дней."),
    "done": "Заказ №{id} выдан. Носи с удовольствием 🖤",
    "cancelled": "Заказ №{id} отменён. Если это ошибка — напиши нам.",
    "expired": ("Заказ №{id} отменён: оплата не пришла за {minutes} минут, "
                "принты вернулись в каталог. Собери заново, если ещё актуально."),
}


def webapp_button(text="Собрать футболку 👕", path="/webapp/"):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, web_app=WebAppInfo(url=config.WEBAPP_URL + path))
    ]])


def phone_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


# ---------- Команды ----------

@dp.message(Command("chatid"))
async def chat_id(m: Message):
    """Отправь эту команду в группе сотрудников — бот пришлёт ID этой группы."""
    await m.answer(
        f"ID этого чата:\n\n<code>{m.chat.id}</code>\n\n"
        "Скопируй его в переменную STAFF_CHAT_ID на Railway.",
        parse_mode="HTML",
    )


@dp.message(CommandStart(deep_link=True))
async def start_deep(m: Message, command: CommandObject):
    """Возврат со страницы оплаты: /start paid_42."""
    arg = (command.args or "").strip()
    if not arg.startswith("paid_"):
        await start(m)
        return
    try:
        oid = int(arg.split("_", 1)[1])
    except (IndexError, ValueError):
        await start(m)
        return

    o = db.get_order(oid)
    if not o or o["user_id"] != m.from_user.id:
        await start(m)
        return

    if o["status"] != "new":
        await m.answer(f"Заказ №{oid}: {STATUS_LABELS.get(o['status'], o['status'])}")
        return

    # Вебхук мог не дойти или опоздать — спрашиваем ЮKassa напрямую.
    if o["payment_id"] and payments.enabled():
        try:
            if await payments.confirm_payment(o["payment_id"], o):
                if db.mark_paid(oid, o["payment_id"]):
                    o = db.get_order(oid)
                    await notify_customer_status(o, "paid")
                    await refresh_or_send_staff_card(o)
                return
        except payments.PaymentError as e:
            log.warning("Проверка платежа по заказу №%s не удалась: %s", oid, e)

    await m.answer(
        f"Заказ №{oid} пока числится неоплаченным. Если деньги ушли — подожди "
        "минуту и напиши нам, разберёмся вручную.")


@dp.message(CommandStart())
async def start(m: Message):
    left = db.quota_left()
    quota_line = (
        f"На сегодня осталось мест: {left}."
        if left > 0 else "На сегодня места закончились — заказ уедет на завтра."
    )
    await m.answer(
        f"Привет! Это кастом-станция {config.BRAND}.\n\n"
        "Собери свою футболку: выбери принты, расставь их как хочешь — "
        "мы запечатаем, а ты просто заберёшь готовую.\n\n"
        f"📍 {config.PICKUP_TEXT}\n"
        f"⚡ {quota_line}\n"
        f"🕐 Храним готовый заказ {config.PICKUP_HOLD_DAYS} дней.",
        reply_markup=webapp_button(),
    )
    if config.YOOKASSA_SEND_RECEIPT and not db.get_phone(m.from_user.id):
        await ask_phone(m.from_user.id)


async def ask_phone(user_id: int):
    await bot.send_message(
        user_id,
        "Ещё одно: для чека нужен номер телефона — туда придёт электронный чек "
        "после оплаты. Жми кнопку внизу, вводить ничего не надо.",
        reply_markup=phone_keyboard(),
    )


async def ask_phone_safe(user_id: int):
    try:
        await ask_phone(user_id)
    except Exception as e:
        log.warning("Не удалось запросить телефон у %s: %s", user_id, e)


@dp.message(F.contact)
async def got_contact(m: Message):
    if m.contact.user_id and m.contact.user_id != m.from_user.id:
        await m.answer("Нужен твой номер, а не чужой контакт.",
                       reply_markup=phone_keyboard())
        return
    db.set_phone(m.from_user.id, m.contact.phone_number)
    await m.answer("Записал, спасибо. Возвращайся в конструктор 👕",
                   reply_markup=ReplyKeyboardRemove())


# ---------- Карточка заказа в чате сотрудников ----------

def order_card_text(o: dict) -> str:
    contact = f"@{o['username']}" if o["username"] else "—"
    lines = [
        f"🧾 Заказ №{o['id']} — {STATUS_LABELS.get(o['status'], o['status'])}",
        f"Клиент: {o['first_name'] or ''} {contact} (id {o['user_id']})",
    ]
    if o.get("phone"):
        lines.append(f"Телефон: {o['phone']}")
    lines += [
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


async def refresh_or_send_staff_card(o: dict):
    """Обновляет карточку в чате сотрудников, а если её нет — отправляет новую."""
    if not config.STAFF_CHAT_ID:
        return
    if not o.get("staff_msg_id"):
        await notify_staff_new_order(o)
        return
    try:
        await bot.edit_message_text(
            chat_id=config.STAFF_CHAT_ID, message_id=o["staff_msg_id"],
            text=order_card_text(o), reply_markup=order_card_kb(o),
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("Не удалось обновить карточку заказа №%s: %s", o["id"], e)


async def alert_staff(text: str):
    if not config.STAFF_CHAT_ID:
        log.error("STAFF_CHAT_ID не задан, а есть что сказать: %s", text)
        return
    try:
        await bot.send_message(config.STAFF_CHAT_ID, text)
    except Exception as e:
        log.warning("Не удалось написать в чат сотрудников: %s", e)


async def notify_customer_status(o: dict, status: str):
    tpl = CUSTOMER_NOTIFY.get(status)
    if not tpl:
        return
    try:
        await bot.send_message(o["user_id"], tpl.format(
            id=o["id"], pickup=config.PICKUP_TEXT, days=config.PICKUP_HOLD_DAYS,
            minutes=config.ORDER_HOLD_MINUTES))
    except Exception as e:
        log.warning("Не удалось написать клиенту %s: %s", o["user_id"], e)


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
    await notify_customer_status(o, new_status)
    await cb.answer("Ок")


async def notify_customer_order_created(o: dict, pay_url: str | None):
    if pay_url:
        text = (f"Заказ №{o['id']} создан! Сумма {o['price']} ₽.\n"
                f"Оплати по ссылке — после оплаты возьмём в работу:\n{pay_url}")
        if config.ORDER_HOLD_MINUTES:
            text += (f"\n\nСсылка ждёт {config.ORDER_HOLD_MINUTES} минут: если не "
                     "оплатить, принты вернутся в каталог.")
    else:
        text = (f"Заказ №{o['id']} создан! Сумма {o['price']} ₽.\n"
                "Сейчас напишем тебе реквизиты для оплаты. "
                "После подтверждения оплаты возьмём футболку в работу.")
    await bot.send_message(o["user_id"], text)
