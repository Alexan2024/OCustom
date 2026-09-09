"""Бот: точка входа для покупателя + рабочее место сотрудника (карточки заказов)."""
import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BotCommandScopeAllPrivateChats,
                           BufferedInputFile, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, WebAppInfo)

from . import assets, cdek, config, db, payments, render

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
    "in_progress": "Заказ №{id} взяли в работу!",
    "ready": ("Заказ №{id} готов! 📦 Забирай: {pickup}\n"
              "Назови номер заказа на кассе. Храним {days} дней."),
    "done": "Заказ №{id} выдан. Носи с удовольствием 🖤",
    "cancelled": "Заказ №{id} отменён. Если это ошибка — напиши нам.",
    "expired": ("Заказ №{id} отменён: оплата не пришла за {minutes} минут, "
                "принты вернулись в каталог. Собери заново, если ещё актуально."),
}

# Для доставки часть сообщений другая: забирать никуда не надо.
CUSTOMER_NOTIFY_DELIVERY = {
    # Накладная заводится сразу после оплаты, поэтому трек прилетает человеку
    # задолго до того, как посылка поедет: сначала мы говорим, что печатаем,
    # и только потом обещаем передачу в СДЭК — иначе через день придут
    # спрашивать, почему трекинг пустой.
    "tracked": ("Заказ №{id}: трек-номер {track}\n{track_url}\n\n"
                "Сейчас запечатываем футболку. Скоро передадим посылку "
                "в СДЭК — обычно через день-два."),
    "ready": "Заказ №{id} готов 📦 Упаковали, отвозим в СДЭК.",
    "shipped": "Заказ №{id} уехал 🚚 Отследить: {track_url}",
    "done": "Заказ №{id} вручён. Носи с удовольствием 🖤",
}


def webapp_url(path: str = "/webapp/") -> str:
    """Адрес мини-аппа с номером сборки.

    Telegram кэширует страницу по адресу, поэтому после деплоя адрес обязан
    измениться — иначе часть людей продолжает открывать прошлую версию,
    пока их кэш не протухнет сам. Номер считает assets.build() от
    содержимого файлов мини-аппа, руками его трогать не нужно.
    """
    sep = "&" if "?" in path else "?"
    return f"{config.WEBAPP_URL}{path}{sep}b={assets.build()}"


def webapp_button(text="Собрать футболку 👕", path="/webapp/"):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, web_app=WebAppInfo(url=webapp_url(path)))
    ]])


def start_kb():
    """Кнопки под приветствием: конструктор, свои заказы, условия."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Собрать футболку 👕",
                              web_app=WebAppInfo(url=webapp_url()))],
        [InlineKeyboardButton(text="Мои заказы 🧾", callback_data="my:list")],
        [InlineKeyboardButton(text="Условия возврата ⚖️", callback_data="terms")],
    ])


async def setup_commands():
    """Меню команд бота (кнопка «/» рядом с полем ввода)."""
    try:
        await bot.set_my_commands(
            [BotCommand(command="start", description="Собрать футболку"),
             BotCommand(command="orders", description="Мои заказы"),
             BotCommand(command="terms", description="Условия возврата")],
            scope=BotCommandScopeAllPrivateChats(),
        )
    except Exception as e:
        log.warning("Не удалось установить меню команд: %s", e)


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
            lines.append(f"  Способ расчёта: {payments.payment_mode_label()}")
            lines.append(f"  Позиция в чеке: «{payments.item_name('M')}»")
            lines.append(f"  Предмет расчёта: {config.RECEIPT_ITEM_SUBJECT}")
            lines.append("  Чек уходит на почту, её спрашиваем при оформлении")
            if config.YOOKASSA_PAYMENT_MODE not in ("full_payment", "full_prepayment"):
                lines.append("  ❌ ЮKassa знает только full_payment и full_prepayment")
            if config.RECEIPT_ITEM_SUBJECT not in ("commodity", "service", "work"):
                lines.append("  ❌ RECEIPT_ITEM_SUBJECT: касса ждёт "
                             "commodity, service или work")
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
    blanks = " · ".join(f"{s} {n}" for s, n in db.shirt_stock().items())
    lines += ["", f"Мини-апп: {config.WEBAPP_URL}/webapp/",
              f"Сборка мини-аппа: {assets.build()}",
              f"Заказов сегодня: {quota}",
              f"Бланки: {blanks} (правится командой /stock)"]
    await m.answer("\n".join(lines), disable_web_page_preview=True)


# ---------- Остаток бланков ----------

# Шаги кнопок в панели. ±1 — обычная продажа мимо мини-аппа, ±5 — привезли
# пачку. Точное число ставится командой, кнопками до сорока штук не долистать.
STOCK_STEPS = (-5, -1, 1, 5)


def stock_text() -> str:
    lines = ["👕 Бланки на складе", ""]
    for size, n in db.shirt_stock().items():
        if n <= 0:
            mark, tail = "❌", " — размер скрыт в мини-аппе"
        elif n <= config.SHIRT_STOCK_LOW:
            mark, tail = "⚠️", " — заканчивается"
        else:
            mark, tail = "•", ""
        lines.append(f"{mark} {size}: {n} шт.{tail}")
    lines.append("")
    if config.SHIRT_STOCK_AUTO:
        lines.append("Заказ через мини-апп списывает бланк сам, отмена — возвращает.")
    else:
        lines.append("Автосписание выключено (SHIRT_STOCK_AUTO=false): "
                     "остаток меняется только руками.")
    lines.append("Точное число: /stock M 20")
    return "\n".join(lines)


def stock_kb() -> InlineKeyboardMarkup:
    rows = []
    for size, n in db.shirt_stock().items():
        row = []
        labelled = False
        for step in STOCK_STEPS:
            if step > 0 and not labelled:
                # Само число — посередине ряда, между «отнять» и «прибавить».
                # Кнопка неактивная: нажатие просто перерисовывает панель.
                row.append(InlineKeyboardButton(
                    text=f"{size}: {n}", callback_data="stk:-:0"))
                labelled = True
            row.append(InlineKeyboardButton(
                text=f"{step:+d}", callback_data=f"stk:{size}:{step}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Обновить ↻", callback_data="stk:-:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("stock"))
async def stock_cmd(m: Message, command: CommandObject):
    """/stock — панель с кнопками. /stock M 20 — поставить точное число.
    /stock M +5 — прибавить. Работает только в чате сотрудников."""
    if config.STAFF_CHAT_ID and m.chat.id != config.STAFF_CHAT_ID:
        return
    args = (command.args or "").split()
    if not args:
        await m.answer(stock_text(), reply_markup=stock_kb())
        return

    size = args[0].upper()
    if size not in config.SIZES:
        await m.answer("Такого размера нет. Есть: " + ", ".join(config.SIZES))
        return
    if len(args) < 2:
        await m.answer(f"Сколько бланков {size}? Например: /stock {size} 20")
        return
    raw = args[1]
    try:
        if raw[0] in "+-":
            was = db.shirt_stock_of(size)
            now = db.add_shirt_stock(size, int(raw))
        else:
            was = db.shirt_stock_of(size)
            now = db.set_shirt_stock(size, int(raw))
    except ValueError:
        await m.answer("Второе слово — число: /stock M 20 или /stock M +5")
        return
    log.info("Остаток бланков %s: %d → %d (@%s)", size, was, now,
             m.from_user.username or m.from_user.id)
    await m.answer(stock_text(), reply_markup=stock_kb())


@dp.callback_query(F.data.startswith("stk:"))
async def staff_stock_edit(cb: CallbackQuery):
    """Кнопки панели остатков. Считает база, а не бот: по кнопкам в общем
    чате жмут вдвоём, и «прочитал — сложил — записал» теряло бы нажатия."""
    if config.STAFF_CHAT_ID and cb.message.chat.id != config.STAFF_CHAT_ID:
        await cb.answer()
        return
    _, size, delta = cb.data.split(":")
    if size in config.SIZES and delta not in ("0", ""):
        now = db.add_shirt_stock(size, int(delta))
        log.info("Остаток бланков %s → %d (@%s)", size, now,
                 cb.from_user.username or cb.from_user.id)
        await cb.answer(f"{size}: {now} шт.")
    else:
        await cb.answer()
    try:
        await cb.message.edit_text(stock_text(), reply_markup=stock_kb())
    except Exception:
        pass   # «message is not modified» — жали кнопку, ничего не изменилось


@dp.message(Command("receipt"))
async def receipt_check(m: Message, command: CommandObject):
    """/receipt 42 — что случилось с чеком по заказу.

    Отвечает на единственный вопрос, который возникает, когда покупатель
    говорит «чек не пришёл»: дошёл ли чек до кассы и на какой адрес его
    отправляли. Работает только в чате сотрудников.
    """
    if config.STAFF_CHAT_ID and m.chat.id != config.STAFF_CHAT_ID:
        return
    try:
        oid = int((command.args or "").strip())
    except ValueError:
        await m.answer("Напиши номер заказа: /receipt 42")
        return

    o = db.get_order(oid)
    if not o:
        await m.answer(f"Заказа №{oid} нет в базе.")
        return

    lines = [f"🧾 Чек по заказу №{oid}", ""]
    lines.append(f"Статус заказа: {STATUS_LABELS.get(o['status'], o['status'])}")
    lines.append(f"Почта в заказе: {o.get('email') or '— не указана'}")
    if o.get("phone"):
        lines.append(f"Телефон в заказе: {o['phone']}")
    lines.append(f"Чек 54-ФЗ: "
                 f"{'передаём' if config.YOOKASSA_SEND_RECEIPT else 'ВЫКЛЮЧЕН'}")

    if not config.YOOKASSA_SEND_RECEIPT:
        lines.append("")
        lines.append("Чек не пробивался: YOOKASSA_SEND_RECEIPT=false на Railway. "
                     "Пока флаг выключен, ни один чек не уйдёт.")
        await m.answer("\n".join(lines))
        return

    if not o.get("payment_id"):
        lines.append("")
        lines.append("Платежа нет: ссылка на оплату не выпускалась, "
                     "либо оплату принимали вручную. Чеку взяться неоткуда.")
        await m.answer("\n".join(lines))
        return

    lines.append(f"Платёж: {o['payment_id']}")
    try:
        payment = await payments.fetch_payment(o["payment_id"])
        receipts = await payments.fetch_receipts(o["payment_id"])
    except payments.PaymentError as e:
        lines += ["", f"❌ {e}"]
        await m.answer("\n".join(lines))
        return

    lines.append(f"Статус платежа: {payment.get('status')}")
    reg = payment.get("receipt_registration")
    lines.append(f"Регистрация чека: {reg or '— поля нет в ответе'}")
    lines.append(f"Чеков в ЮKassa: {len(receipts)}")
    for r in receipts:
        lines.append(f"  • {r.get('type')} — {r.get('status')}")

    lines.append("")
    if payment.get("status") != "succeeded":
        lines.append("Платёж не завершён. Чек пробивается только после оплаты — "
                     "возможно, покупатель до конца не дошёл.")
    elif reg == "succeeded" or any(r.get("status") == "succeeded" for r in receipts):
        lines.append("Чек пробит и ушёл в ОФД. Дальше письмо отправляет ОФД, "
                     "а не мы: пусть покупатель посмотрит папку «Спам» и "
                     "проверит, верно ли записан адрес выше. Если письма нет "
                     "и там — вопрос к Паше, включена ли у ОФД отправка "
                     "на e-mail.")
    elif reg == "pending":
        lines.append("Касса ещё не пробила чек. Обычно это минуты; если висит "
                     "дольше — Паше стоит посмотреть очередь в Атоле.")
    elif reg == "canceled":
        lines.append("Касса отклонила чек. Причина видна в ЛК ЮKassa, раздел "
                     "«Чеки». Чаще всего это НДС или СНО — их показывает /diag.")
    else:
        lines.append("Чек до кассы не дошёл. Проверь /diag: похоже, "
                     "фискализация в ЮKassa не включена.")
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
                    await after_paid(o)
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


# ---------- Условия возврата ----------

# Тот же текст покупатель видит и подтверждает галочкой на экране оформления;
# здесь он лежит, чтобы к нему можно было вернуться после покупки.
TERMS_TEXT = f"⚖️ Условия возврата\n\n{config.RETURN_POLICY_TEXT}"


@dp.message(Command("terms"))
async def terms(m: Message):
    await m.answer(TERMS_TEXT)


@dp.callback_query(F.data == "terms")
async def cb_terms(cb: CallbackQuery):
    await cb.answer()
    await bot.send_message(cb.message.chat.id, TERMS_TEXT)


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
    if o.get("email"):
        lines.append(f"Почта (чек): {o['email']}")
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
    lines.append("👁 Раскладка: "
                 + webapp_url(f"/webapp/?view={o['id']}&key={o['view_token']}"))
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
            and not o.get("cdek_uuid")
            and o["status"] in ("paid", "in_progress", "ready", "shipped")):
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


async def cancel_shipment(o: dict):
    """Гасит накладную у отменённого заказа.

    Накладную заводим сразу после оплаты, поэтому у отменённого заказа она,
    скорее всего, уже есть. Если СДЭК её удалить не даст (пакет успели
    принять) — зовём сотрудника, чтобы отменил руками.
    """
    if not o or not o.get("cdek_uuid") or not cdek.enabled():
        return
    try:
        await cdek.delete_shipment(o["cdek_uuid"])
    except cdek.CdekError as e:
        log.warning("Накладная по заказу №%s не погасилась: %s", o["id"], e)
        await alert_staff(
            f"⚠️ Заказ №{o['id']} отменён, а накладную СДЭК погасить не вышло:\n{e}\n"
            f"Отмени её руками в ЛК СДЭК: "
            f"{o.get('cdek_number') or o['cdek_uuid']}")
        return
    db.clear_cdek(o["id"])
    log.info("Накладная по отменённому заказу №%s погашена", o["id"])
    await alert_staff(f"Заказ №{o['id']} отменён, накладная СДЭК погашена.")


async def after_paid(o: dict):
    """Общий хвост оплаты: и для вебхука, и для возврата человека в бота.

    Накладную заводим здесь же, если CDEK_CREATE_ON_PAID: трек-номер нужен
    покупателю сразу, а не через день, когда футболку допечатают.
    """
    await notify_customer_status(o, "paid")
    await refresh_or_send_staff_card(o)
    if (config.CDEK_CREATE_ON_PAID and o["delivery_method"] != "pickup"
            and cdek.enabled()):
        asyncio.create_task(_shipment_task(o))


async def sync_shipment(oid: int):
    """Спрашивает у СДЭК настоящее состояние накладной и подтягивает его
    в заказ: трек-номер клиенту, статус в карточку, приёмку посылки —
    в 'shipped', вручение — в 'done'."""
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
    o = db.get_order(oid)

    # СДЭК принял пакет — переводим заказ в «Передан в СДЭК» сами. Кнопку
    # сотрудник нажимает уже в ПВЗ, с пакетом в одной руке и телефоном
    # в другой, и забывает об этом чаще, чем хотелось бы. Слово СДЭК тут
    # достовернее нажатой кнопки: посылка уже физически не у нас.
    became_shipped = (
        config.CDEK_AUTO_SHIPPED
        and cdek.looks_shipped(info)
        and o["status"] in ("paid", "in_progress", "ready")
    )
    if became_shipped:
        db.set_status(oid, "shipped")
        o = db.get_order(oid)
        log.info("Заказ №%s: СДЭК принял посылку (%s) — статус «передан в доставку»",
                 oid, info["status"] or "—")

    if not (changed or became_shipped):
        return

    if changed and info["invalid"]:
        await alert_staff(
            f"⚠️ СДЭК отклонил накладную по заказу №{oid}: {info['error']}\n"
            "Проверь адрес и телефон получателя, потом жми «Создать накладную СДЭК ↻».")

    if info["number"] and not had_number:
        # До передачи в СДЭК это ещё не «уехал», а просто присвоенный номер.
        await notify_customer_status(
            o, "shipped" if o["status"] == "shipped" else "tracked")
    elif became_shipped:
        await notify_customer_status(o, "shipped")

    if became_shipped:
        await alert_staff(
            f"🚚 Заказ №{oid}: СДЭК принял посылку — «{info['text'] or 'в пути'}». "
            "Статус сменился сам, «Сдал в СДЭК» нажимать не нужно.")

    if info["status"] in cdek.DONE_STATUSES and o["status"] not in ("done", "cancelled"):
        db.set_status(oid, "done")
        o = db.get_order(oid)
        await notify_customer_status(o, "done")
    elif changed and info["status"] in cdek.ALERT_STATUSES:
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

    # Заказ стал оплаченным (в ручном режиме это делает сотрудник кнопкой) —
    # заводим накладную, чтобы трек-номер ушёл покупателю сразу.
    if (new_status == "paid" and config.CDEK_CREATE_ON_PAID
            and o["delivery_method"] != "pickup"):
        asyncio.create_task(_shipment_task(o))
    # Готовый заказ с доставкой сразу заводим в СДЭК: сотруднику останется
    # распечатать наклейку и отнести пакет. Если накладную уже завели
    # при оплате, ensure_shipment просто ничего не сделает.
    if new_status == "ready" and o["delivery_method"] != "pickup":
        asyncio.create_task(_shipment_task(o))
    # Отменённый заказ не поедет — накладную гасим, чтобы не висела
    # в договоре.
    if new_status == "cancelled":
        asyncio.create_task(_cancel_shipment_task(o))


async def _cancel_shipment_task(o: dict):
    try:
        await cancel_shipment(o)
        await refresh_or_send_staff_card(db.get_order(o["id"]))
    except Exception as e:
        log.warning("Гашение накладной по заказу №%s упало: %s", o["id"], e)


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
