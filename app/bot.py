"""Бот: точка входа для покупателя + рабочее место сотрудника (карточки заказов)."""
import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BotCommandScopeAllPrivateChats,
                           BufferedInputFile, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove, WebAppInfo)

from . import cdek, config, db, payments, render

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


def start_kb():
    """Кнопки под приветствием: конструктор и свои заказы."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Собрать футболку 👕",
                              web_app=WebAppInfo(url=config.WEBAPP_URL + "/webapp/"))],
        [InlineKeyboardButton(text="Мои заказы 🧾", callback_data="my:list")],
    ])


async def setup_commands():
    """Меню команд бота (кнопка «/» рядом с полем ввода)."""
    try:
        await bot.set_my_commands(
            [BotCommand(command="start", description="Собрать футболку"),
             BotCommand(command="orders", description="Мои заказы")],
            scope=BotCommandScopeAllPrivateChats(),
        )
    except Exception as e:
        log.warning("Не удалось установить меню команд: %s", e)


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


def _secret_state(value: str) -> str:
    if not value:
        return "не задан"
    return f"задан ({len(value)} симв.)"


@dp.message(Command("diag"))
async def diag(m: Message):
    """Самопроверка. Работает только в чате сотрудников: показывает, какие
    настройки доехали и что о нас думают ЮKassa и СДЭК. Значения ключей
    не печатает — только «задан / не задан»."""
    if config.STAFF_CHAT_ID and m.chat.id != config.STAFF_CHAT_ID:
        return
    lines = [f"🩺 {config.BRAND}, состояние", ""]

    lines.append(f"Режим оплаты: {config.PAYMENT_MODE}")
    if config.PAYMENT_MODE not in ("manual", "yookassa"):
        lines.append("  ❌ значение не распознано — ссылки на оплату не создаются")
    if payments.enabled():
        lines.append(f"  SHOP_ID: {_secret_state(config.YOOKASSA_SHOP_ID)}")
        lines.append(f"  SECRET_KEY: {_secret_state(config.YOOKASSA_SECRET_KEY)}")
        lines.append(f"  Чек 54-ФЗ: "
                     f"{'передаём' if config.YOOKASSA_SEND_RECEIPT else 'выключен'}")
        if config.YOOKASSA_SEND_RECEIPT:
            lines.append(f"  Ставка НДС: код {config.YOOKASSA_VAT_CODE}")
            lines.append(f"  Способ расчёта: {config.YOOKASSA_PAYMENT_MODE}")
            if config.YOOKASSA_PAYMENT_MODE not in ("full_payment", "full_prepayment"):
                lines.append("  ❌ ЮKassa знает только full_payment и full_prepayment")
        try:
            shop = await payments.fetch_shop()
            lines.append(f"  ✅ магазин отвечает, статус «{shop.get('status')}»")
            fisc = payments.fiscalization_on(shop)
            if fisc is None:
                lines.append("  ⚠️ не понял, включена ли фискализация")
            elif fisc and not config.YOOKASSA_SEND_RECEIPT:
                lines.append("  ❌ КАССА ПОДКЛЮЧЕНА, а YOOKASSA_SEND_RECEIPT=false.")
                lines.append("     Поэтому платежи и отклоняются. Поставь "
                             "YOOKASSA_SEND_RECEIPT=true и YOOKASSA_VAT_CODE.")
            elif not fisc and config.YOOKASSA_SEND_RECEIPT:
                lines.append("  ⚠️ чек собираем, но касса не подключена — "
                             "чеки уходят в никуда")
            else:
                lines.append("  ✅ чек и касса согласованы")
        except payments.PaymentError as e:
            lines.append(f"  ❌ {e}")

    lines.append("")
    if cdek.enabled():
        lines.append("Доставка: СДЭК, "
                     + ("ПЕСОЧНИЦА" if config.CDEK_TEST else "боевой контур"))
        lines.append(f"  ACCOUNT: {_secret_state(config.CDEK_ACCOUNT)}")
        lines.append(f"  PASSWORD: {_secret_state(config.CDEK_PASSWORD)}")
        try:
            for s in await cdek.check():
                lines.append("  " + s)
        except cdek.CdekError as e:
            lines.append(f"  ❌ {e}")
    else:
        lines.append("Доставка: только самовывоз (DELIVERY_CDEK выключен)")

    quota = (f"{db.orders_today()} из {config.ONLINE_QUOTA_PER_DAY}"
             if config.quota_enabled()
             else f"{db.orders_today()} (дневная квота выключена)")
    lines += ["", f"Мини-апп: {config.WEBAPP_URL}/webapp/",
              f"Заказов сегодня: {quota}"]
    await m.answer("\n".join(lines), disable_web_page_preview=True)


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
    get_line = (f"📍 Самовывоз: {config.PICKUP_TEXT}\n🚚 Или доставка СДЭК по России."
                if cdek.enabled() else f"📍 {config.PICKUP_TEXT}")
    await m.answer(
        f"Привет! Это кастом-станция {config.BRAND}.\n\n"
        "Собери свою футболку: выбери принты, расставь их как хочешь — "
        "на груди, на спине и на рукавах. Мы запечатаем, а ты просто "
        "заберёшь готовую.\n\n"
        f"{get_line}\n"
        f"🕐 Храним готовый заказ {config.PICKUP_HOLD_DAYS} дней.",
        reply_markup=start_kb(),
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


# ---------- Мои заказы: карточка для покупателя ----------

# Покупателю показываем состояние человеческим языком: у сотрудника
# в тех же статусах свои формулировки, они здесь не подходят.
CUSTOMER_STATUS = {
    "new": "🕐 Ждём оплату",
    "paid": "✅ Оплачен, в очереди",
    "in_progress": "🔥 Запечатываем",
    "ready": "📦 Готов",
    "shipped": "🚚 Едет",
    "done": "🤝 Выдан",
    "cancelled": "❌ Отменён",
}


def customer_card_text(o: dict) -> str:
    """Подпись под картинкой раскладки: номер, статус, сумма, куда едет."""
    goods = payments.goods_price(o)
    lines = [
        f"🧾 Заказ №{o['id']} — {CUSTOMER_STATUS.get(o['status'], o['status'])}",
        f"Размер {o['size']} · принтов: {len(o['items'])}",
    ]
    if o.get("delivery_price"):
        lines.append(f"Сумма {o['price']} ₽ ({goods} + {o['delivery_price']} доставка)")
    else:
        lines.append(f"Сумма {o['price']} ₽")

    method = o["delivery_method"]
    if method == "pickup":
        lines.append(f"Получение: самовывоз — {config.PICKUP_TEXT}")
    else:
        lines.append(f"Получение: {DELIVERY_LABELS.get(method, method)}")
        where = o.get("pvz_address") or o.get("address") or o.get("city_name")
        if where:
            lines.append(where)
    if o.get("cdek_number"):
        lines.append(f"Трек-номер: {o['cdek_number']}")
    if o.get("cdek_status_text"):
        lines.append(f"СДЭК: {o['cdek_status_text']}")

    if o["status"] == "new" and config.ORDER_HOLD_MINUTES:
        lines.append(f"\nБез оплаты заказ живёт {config.ORDER_HOLD_MINUTES} минут.")
    if o["status"] == "ready" and method == "pickup":
        lines.append(f"\nНазови номер заказа на кассе. Храним {config.PICKUP_HOLD_DAYS} дней.")
    return "\n".join(lines)


def customer_card_kb(o: dict) -> InlineKeyboardMarkup:
    rows = []
    if o["status"] == "new" and payments.enabled():
        rows.append([InlineKeyboardButton(text="Оплатить 💳",
                                          callback_data=f"my:pay:{o['id']}")])
    if o.get("cdek_number"):
        rows.append([InlineKeyboardButton(text="Отследить 🚚",
                                          url=cdek.tracking_url(o["cdek_number"]))])
    rows.append([InlineKeyboardButton(text="Обновить ↻", callback_data=f"my:one:{o['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_customer_card(o: dict, chat_id: int):
    """Одна карточка: картинка раскладки + весь текст подписью.

    Если раскладку нарисовать не удалось (нет картинки принта, битый файл),
    отправляем то же самое текстом — заказ важнее картинки.
    """
    text, kb = customer_card_text(o), customer_card_kb(o)
    img = render.order_image(o)
    if img:
        try:
            await bot.send_photo(
                chat_id, BufferedInputFile(img, filename=f"order_{o['id']}.png"),
                caption=text, reply_markup=kb)
            return
        except Exception as e:
            log.warning("Карточка заказа №%s не ушла картинкой: %s", o["id"], e)
    await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)


async def show_my_orders(user_id: int, chat_id: int):
    orders = db.active_orders(user_id)
    if not orders:
        await bot.send_message(
            chat_id, "Активных заказов нет. Собери футболку — она появится здесь.",
            reply_markup=webapp_button())
        return
    for o in orders:
        await send_customer_card(o, chat_id)


@dp.message(Command("orders"))
async def my_orders(m: Message):
    await show_my_orders(m.from_user.id, m.chat.id)


@dp.callback_query(F.data == "my:list")
async def cb_my_list(cb: CallbackQuery):
    await cb.answer()
    await show_my_orders(cb.from_user.id, cb.message.chat.id)


@dp.callback_query(F.data.startswith("my:one:"))
async def cb_my_refresh(cb: CallbackQuery):
    oid = int(cb.data.rsplit(":", 1)[1])
    o = db.get_order(oid)
    if not o or o["user_id"] != cb.from_user.id:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    text, kb = customer_card_text(o), customer_card_kb(o)
    try:
        if cb.message.photo:
            await cb.message.edit_caption(caption=text, reply_markup=kb)
        else:
            await cb.message.edit_text(text, reply_markup=kb,
                                       disable_web_page_preview=True)
        await cb.answer("Обновил")
    except Exception:
        # Телеграм не даёт переписать сообщение тем же текстом — это не ошибка
        await cb.answer("Пока без изменений")


@dp.callback_query(F.data.startswith("my:pay:"))
async def cb_my_pay(cb: CallbackQuery):
    """Ссылку выпускаем заново: старая могла протухнуть или не дойти.
    Ключ идемпотентности меняем временем, иначе ЮKassa вернёт тот же платёж."""
    oid = int(cb.data.rsplit(":", 1)[1])
    o = db.get_order(oid)
    if not o or o["user_id"] != cb.from_user.id:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if o["status"] != "new":
        await cb.answer("Этот заказ уже оплачен", show_alert=True)
        return
    await cb.answer("Готовлю ссылку…")
    try:
        created = await payments.create_payment(o, attempt=int(time.time()))
    except payments.PaymentError as e:
        log.warning("Ссылка на оплату по заказу №%s не выпустилась: %s", oid, e)
        await bot.send_message(cb.message.chat.id,
                               "Со ссылкой вышла заминка — уже разбираемся.")
        await alert_staff(f"⚠️ Заказ №{oid}: покупатель нажал «Оплатить», "
                          f"ссылка не выпустилась.\n{e}")
        return
    if not created:
        await bot.send_message(cb.message.chat.id,
                               "Оплату принимаем вручную — сейчас пришлём реквизиты.")
        await alert_staff(f"Заказ №{oid}: покупатель просит реквизиты для оплаты.")
        return
    pay_url, payment_id = created
    db.set_payment_id(oid, payment_id)
    await bot.send_message(cb.message.chat.id,
                           f"Заказ №{oid}, сумма {o['price']} ₽:\n{pay_url}")


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
    # Ссылку на оплату можно перевыпустить: ЮKassa могла не ответить
    # в момент создания заказа, и человек остался без ссылки.
    if o["status"] == "new" and payments.enabled():
        rows.append([InlineKeyboardButton(text="Выслать ссылку на оплату ↻",
                                          callback_data=f"pay:{o['id']}")])
    # Накладную тоже можно перевыпустить руками: СДЭК мог не ответить
    # в момент, когда заказ переводили в «Готово».
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


@dp.callback_query(F.data.startswith("pay:"))
async def staff_resend_link(cb: CallbackQuery):
    """Перевыпуск ссылки на оплату. Ключ идемпотентности меняем временем —
    иначе ЮKassa вернёт тот же самый (неудавшийся) платёж."""
    oid = int(cb.data.split(":", 1)[1])
    o = db.get_order(oid)
    if not o:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if o["status"] != "new":
        await cb.answer("Заказ уже не ждёт оплаты", show_alert=True)
        return
    await cb.answer("Выпускаю ссылку…")
    try:
        created = await payments.create_payment(o, attempt=int(time.time()))
    except payments.PaymentError as e:
        await alert_staff(f"⚠️ Заказ №{oid}: ссылка снова не выпустилась.\n{e}")
        return
    if not created:
        await alert_staff(f"Заказ №{oid}: включён ручной режим оплаты, "
                          "ссылку выпускать нечем.")
        return
    pay_url, payment_id = created
    db.set_payment_id(oid, payment_id)
    try:
        await bot.send_message(
            o["user_id"],
            f"Ссылка на оплату заказа №{oid}, сумма {o['price']} ₽:\n{pay_url}")
        await alert_staff(f"✅ Заказ №{oid}: ссылка на оплату ушла покупателю.")
    except Exception as e:
        await alert_staff(f"⚠️ Заказ №{oid}: ссылка выпущена, но покупателю "
                          f"не доставилась ({e}). Вот она:\n{pay_url}")


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
    elif payments.enabled():
        # Ссылка не выпустилась. Обещать «реквизиты» нельзя — это текст
        # ручного режима, а сотрудники в этот момент ничего не знают.
        # Знать они будут: рядом уходит предупреждение в рабочий чат.
        text = (f"Заказ №{o['id']} создан! Сумма {o['price']} ₽.\n"
                "Со ссылкой на оплату вышла заминка — уже разбираемся, "
                "пришлём её сюда в ближайшие минуты.")
    else:
        text = (f"Заказ №{o['id']} создан! Сумма {o['price']} ₽.\n"
                "Сейчас напишем тебе реквизиты для оплаты. "
                "После подтверждения оплаты возьмём футболку в работу.")
    await bot.send_message(
        o["user_id"], text + tail,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Мои заказы 🧾", callback_data="my:list")]]))
