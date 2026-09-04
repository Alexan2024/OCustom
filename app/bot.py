"""Бот: точка входа для покупателя + рабочее место сотрудника (карточки заказов)."""
import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)

from . import cdek, config, db, payments

log = logging.getLogger("bot")

bot = Bot(config.BOT_TOKEN)
dp = Dispatcher()

STATUS_LABELS = {
    "new": "🕐 Ожидает оплаты",
    "paid": "✅ Оплачен, в очереди",
    "in_progress": "🔥 Запечатывается",
    "ready": "📦 Готов",
    "shipped": "🚚 Передан в СДЭК",
    "done": "🤝 Выдан",
    "cancelled": "❌ Отменён",
}

DELIVERY_LABELS = {
    "pickup": "Самовывоз",
    "cdek_pvz": "СДЭК, пункт выдачи",
    "cdek_door": "СДЭК, курьер до двери",
}

# Заголовки сторон в карточке. Левый/правый — как на человеке, а не как
# на экране: сотрудник берёт вещь в руки, ему нужна эта система координат.
SIDE_TITLES = {
    "front": "ПЕРЕД",
    "back": "СПИНА",
    "sleeve_l": "ЛЕВЫЙ РУКАВ (как на человеке)",
    "sleeve_r": "ПРАВЫЙ РУКАВ (как на человеке)",
}

# Переходы, доступные сотруднику из каждого статуса.
# У самовывоза после «Готово» сразу выдача, у доставки — сначала передача в СДЭК.
STAFF_FLOW_PICKUP = {
    "new": [("paid", "Оплачен ✓"), ("cancelled", "Отменить ✕")],
    "paid": [("in_progress", "Взять в работу"), ("cancelled", "Отменить ✕")],
    "in_progress": [("ready", "Готово 📦")],
    "ready": [("done", "Выдан 🤝")],
}
STAFF_FLOW_DELIVERY = {
    "new": [("paid", "Оплачен ✓"), ("cancelled", "Отменить ✕")],
    "paid": [("in_progress", "Взять в работу"), ("cancelled", "Отменить ✕")],
    "in_progress": [("ready", "Готово 📦")],
    "ready": [("shipped", "Сдал в СДЭК 🚚")],
    "shipped": [("done", "Вручён 🤝")],
}


def staff_flow(o: dict) -> dict:
    return STAFF_FLOW_PICKUP if o["delivery_method"] == "pickup" else STAFF_FLOW_DELIVERY


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

# Для доставки часть сообщений другая: забирать никуда не надо.
CUSTOMER_NOTIFY_DELIVERY = {
    "ready": ("Заказ №{id} готов 📦 Упаковали и везём в СДЭК. "
              "Как только присвоят трек-номер — пришлём его сюда."),
    "shipped": "Заказ №{id} уехал 🚚 Отследить: {track_url}",
    "done": "Заказ №{id} вручён. Носи с удовольствием 🖤",
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
    get_line = (f"📍 Самовывоз: {config.PICKUP_TEXT}\n🚚 Или доставка СДЭК по России."
                if cdek.enabled() else f"📍 {config.PICKUP_TEXT}")
    await m.answer(
        f"Привет! Это кастом-станция {config.BRAND}.\n\n"
        "Собери свою футболку: выбери принты, расставь их как хочешь — "
        "на груди, на спине и на рукавах. Мы запечатаем, а ты просто "
        "заберёшь готовую.\n\n"
        f"{get_line}\n"
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

def _placement_line(item: dict, side: str) -> str:
    """Одна строка спеки: где именно лежит принт, в миллиметрах."""
    x, y = item["x_mm"], item["y_mm"]
    if side in config.SLEEVE_SIDES:
        # По рукаву ось X идёт вокруг руки: минус — к переду, плюс — к спине.
        if abs(x) < 0.5:
            dx = "ровно по центру сбоку"
        else:
            dx = f"{abs(x):.0f} мм {'к спине' if x > 0 else 'к переду'} от центра"
        dy = f"{y:.0f} мм от проймы"
    else:
        if abs(x) < 0.5:
            dx = "по центру"
        else:
            dx = f"{abs(x):.0f} мм {'правее' if x > 0 else 'левее'} центра"
        dy = f"{y:.0f} мм от верха зоны"
    rot = f", поворот {item['rotation']}°" if item["rotation"] else ""
    return (f"  • «{item['name']}» ({item['width_mm']:.0f}×{item['height_mm']:.0f} мм): "
            f"центр {dx}, {dy}{rot}")


def _delivery_lines(o: dict) -> list[str]:
    """Блок «куда едет». Для самовывоза — одна строка, чтобы не шуметь."""
    method = o["delivery_method"]
    if method == "pickup":
        return ["Получение: самовывоз в поп-апе"]
    lines = [f"— {DELIVERY_LABELS.get(method, method)} —"]
    if o.get("recipient_name"):
        lines.append(f"Получатель: {o['recipient_name']}")
    if o.get("city_name"):
        lines.append(f"Город: {o['city_name']}")
    if method == "cdek_pvz" and o.get("pvz_address"):
        lines.append(f"Пункт выдачи {o.get('pvz_code') or ''}: {o['pvz_address']}")
    if method == "cdek_door" and o.get("address"):
        lines.append(f"Адрес: {o['address']}")
    lines.append(f"Доставка: {o.get('delivery_price') or 0} ₽")
    if o.get("cdek_number"):
        lines.append(f"Накладная: {o['cdek_number']} — {cdek.tracking_url(o['cdek_number'])}")
    elif o.get("cdek_uuid"):
        lines.append("Накладная создана, номер ещё не присвоен")
    if o.get("cdek_status_text"):
        lines.append(f"Статус СДЭК: {o['cdek_status_text']}")
    return lines


def order_card_text(o: dict) -> str:
    contact = f"@{o['username']}" if o["username"] else "—"
    goods = payments.goods_price(o)
    lines = [
        f"🧾 Заказ №{o['id']} — {STATUS_LABELS.get(o['status'], o['status'])}",
        f"Клиент: {o['first_name'] or ''} {contact} (id {o['user_id']})",
    ]
    if o.get("phone"):
        lines.append(f"Телефон: {o['phone']}")
    sum_line = f"Размер: {o['size']}  |  Сумма: {o['price']} ₽"
    if o.get("delivery_price"):
        sum_line += f" ({goods} + {o['delivery_price']} доставка)"
    lines.append(sum_line)
    lines.append("")
    lines += _delivery_lines(o)
    lines.append("")
    for side in config.SIDES:
        items = [i for i in o["items"] if i["side"] == side]
        if not items:
            continue
        lines.append(f"— {SIDE_TITLES[side]} —")
        for i in items:
            lines.append(_placement_line(i, side))
    lines.append("")
    lines.append(f"👁 Раскладка: {config.WEBAPP_URL}/webapp/?view={o['id']}&key={o['view_token']}")
    return "\n".join(lines)


def order_card_kb(o: dict) -> InlineKeyboardMarkup | None:
    rows = [[InlineKeyboardButton(text=t, callback_data=f"st:{o['id']}:{s}")]
            for s, t in staff_flow(o).get(o["status"], [])]
    # Накладную можно перевыпустить руками: СДЭК мог не ответить в момент,
    # когда заказ переводили в «Готово».
    if (o["delivery_method"] != "pickup" and cdek.enabled()
            and not o.get("cdek_uuid") and o["status"] in ("ready", "shipped")):
        rows.append([InlineKeyboardButton(text="Создать накладную СДЭК ↻",
                                          callback_data=f"cd:{o['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


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
    if not config.STAFF_CHAT_ID or not o:
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
    delivery = o["delivery_method"] != "pickup"
    tpl = (CUSTOMER_NOTIFY_DELIVERY.get(status) if delivery else None) \
        or CUSTOMER_NOTIFY.get(status)
    if not tpl:
        return
    track = o.get("cdek_number") or ""
    try:
        await bot.send_message(o["user_id"], tpl.format(
            id=o["id"], pickup=config.PICKUP_TEXT, days=config.PICKUP_HOLD_DAYS,
            minutes=config.ORDER_HOLD_MINUTES, track=track,
            track_url=cdek.tracking_url(track) if track else "мы пришлём трек отдельно"))
    except Exception as e:
        log.warning("Не удалось написать клиенту %s: %s", o["user_id"], e)


# ---------- Накладная СДЭК ----------

async def ensure_shipment(o: dict):
    """Создаёт накладную, если её ещё нет. Дальше номер и статусы подтянет
    sync_shipment — вебхуком или фоновой проверкой."""
    if not o or o["delivery_method"] == "pickup" or not cdek.enabled():
        return
    if o.get("cdek_uuid"):
        return
    try:
        uuid = await cdek.create_shipment(o)
    except cdek.CdekError as e:
        log.error("Накладная СДЭК по заказу №%s не создалась: %s", o["id"], e)
        await alert_staff(
            f"⚠️ Не удалось создать накладную СДЭК по заказу №{o['id']}:\n{e}\n"
            "Кнопка «Создать накладную СДЭК ↻» в карточке — повторить попытку.")
        return
    if not db.set_cdek_uuid(o["id"], uuid):
        return
    log.info("Заказ №%s заведён в СДЭК: %s", o["id"], uuid)
    # Номер присваивается не мгновенно — даём СДЭК несколько секунд.
    await asyncio.sleep(6)
    await sync_shipment(o["id"])


async def sync_shipment(oid: int):
    """Спрашивает у СДЭК настоящее состояние накладной и подтягивает его
    в заказ: трек-номер клиенту, статус в карточку, вручение — в 'done'."""
    o = db.get_order(oid)
    if not o or not o.get("cdek_uuid") or not cdek.enabled():
        return
    try:
        info = await cdek.fetch_shipment(o["cdek_uuid"])
    except cdek.CdekError as e:
        log.warning("Не смог прочитать накладную по заказу №%s: %s", oid, e)
        return

    had_number = bool(o.get("cdek_number"))
    changed = db.set_cdek_state(oid, info["number"], info["status"], info["text"])
    if not changed:
        return
    o = db.get_order(oid)

    if info["invalid"]:
        await alert_staff(
            f"⚠️ СДЭК отклонил накладную по заказу №{oid}: {info['error']}\n"
            "Проверь адрес и телефон получателя, потом жми «Создать накладную СДЭК ↻».")

    if info["number"] and not had_number:
        await notify_customer_status(o, "shipped")

    if info["status"] in cdek.DONE_STATUSES and o["status"] not in ("done", "cancelled"):
        db.set_status(oid, "done")
        o = db.get_order(oid)
        await notify_customer_status(o, "done")
    elif info["status"] in cdek.ALERT_STATUSES:
        await alert_staff(
            f"⚠️ Заказ №{oid}: СДЭК сообщает «{info['text']}». Нужен человек.")

    await refresh_or_send_staff_card(o)


@dp.callback_query(F.data.startswith("cd:"))
async def staff_make_shipment(cb: CallbackQuery):
    oid = int(cb.data.split(":", 1)[1])
    o = db.get_order(oid)
    if not o:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    await cb.answer("Создаю накладную…")
    await ensure_shipment(o)
    await refresh_or_send_staff_card(db.get_order(oid))


@dp.callback_query(F.data.startswith("st:"))
async def staff_set_status(cb: CallbackQuery):
    _, oid, new_status = cb.data.split(":")
    o = db.get_order(int(oid))
    if not o:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    allowed = [s for s, _ in staff_flow(o).get(o["status"], [])]
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

    # Готовый заказ с доставкой сразу заводим в СДЭК: сотруднику останется
    # распечатать наклейку и отнести пакет.
    if new_status == "ready" and o["delivery_method"] != "pickup":
        asyncio.create_task(_shipment_task(o))


async def _shipment_task(o: dict):
    try:
        await ensure_shipment(o)
        await refresh_or_send_staff_card(db.get_order(o["id"]))
    except Exception as e:
        log.warning("Фоновое создание накладной по заказу №%s упало: %s", o["id"], e)


async def notify_customer_order_created(o: dict, pay_url: str | None):
    tail = ""
    if o["delivery_method"] != "pickup":
        where = o.get("pvz_address") or o.get("address") or o.get("city_name") or ""
        tail = f"\n\nДоставка: {DELIVERY_LABELS.get(o['delivery_method'])}\n{where}"
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
    await bot.send_message(o["user_id"], text + tail)
