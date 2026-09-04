"""Фоновые задачи.

Первая: активный опрос платежей. Вебхук ЮKassa — самый быстрый путь, но он
держится на одной настройке в чужом личном кабинете: не настроили, ошиблись
в адресе, отключили после серии таймаутов — и факт оплаты к нам не приезжает
вообще. Раньше единственной страховкой была проверка перед автоотменой, то
есть через полчаса: человек оплатил и полчаса сидит в тишине, а сотрудники
видят «Ожидает оплаты». Теперь каждые двадцать секунд сами спрашиваем ЮKassa
про все заказы, по которым выпущена ссылка. Вебхук остаётся — он просто
успевает первым, когда работает.

Вторая: неоплаченные заказы не должны вечно держать принты в резерве.
DTF-переносы — физические остатки, каждый брошенный заказ прячет от других
покупателей реальную наклейку, которая лежит на складе.

Третья: доопрос накладных СДЭК. Логика та же, что с оплатой: вебхук может
потеряться, и человек останется без трек-номера. Раз в десять минут проходим
по активным отправлениям и спрашиваем СДЭК сами.
"""
import asyncio
import logging

from . import bot as tgbot, cdek, config, db, payments

log = logging.getLogger("jobs")

CHECK_EVERY_SEC = 20        # круг опроса платежей и проверки таймаутов
SHIPMENTS_EVERY_SEC = 600   # доопрос накладных СДЭК
WATCH_LIMIT = 40            # сколько неоплаченных заказов опрашиваем за круг

# Один раз за запуск сообщаем в рабочий чат, что оплату нашёл опрос, а не
# уведомление: почти всегда это значит, что в ЮKassa не прописан адрес
# HTTP-уведомлений. Дальше молчим, чтобы не превращать это в спам.
_polling_reported = False


# ---------- Платежи ----------

def _pending_payment_orders() -> list[int]:
    """Заказы, которые ждут оплату и по которым уже выпущена ссылка.

    Запрос живёт здесь, а не в db.py, потому что нужен только фоновой
    задаче: это не часть модели заказа, а деталь страховочного механизма.
    """
    with db.conn() as c:
        rows = c.execute(
            "SELECT id FROM orders WHERE status='new' "
            "AND payment_id IS NOT NULL AND payment_id != '' "
            "ORDER BY id DESC LIMIT ?",
            (WATCH_LIMIT,),
        ).fetchall()
    return [r["id"] for r in rows]


async def check_payment(oid: int, source: str = "опрос") -> bool:
    """Спрашивает ЮKassa про один заказ и, если деньги пришли, переводит его
    в «оплачен»: пишет покупателю и обновляет карточку в чате сотрудников.

    True — заказ именно сейчас стал оплаченным. False — денег ещё нет либо
    заказ уже отметил кто-то другой (вебхук, возврат в бота, сотрудник
    кнопкой). Второе уведомление при этом не уходит: решает db.mark_paid.
    """
    global _polling_reported

    o = db.get_order(oid)
    if not o or o["status"] != "new" or not o.get("payment_id"):
        return False
    if not payments.enabled():
        return False

    if not await payments.confirm_payment(o["payment_id"], o):
        return False
    if not db.mark_paid(oid, o["payment_id"]):
        return False

    o = db.get_order(oid)
    log.info("Заказ №%s оплачен (%s)", oid, source)
    await tgbot.notify_customer_status(o, "paid")
    await tgbot.refresh_or_send_staff_card(o)

    if not _polling_reported:
        _polling_reported = True
        await tgbot.alert_staff(
            f"ℹ️ Оплату заказа №{oid} нашли опросом, а не уведомлением от ЮKassa.\n"
            "Всё работает, но с задержкой до полуминуты. Если так каждый раз — "
            "в личном кабинете ЮKassa не прописан адрес HTTP-уведомлений "
            "(часть 6.4 инструкции)."
        )
    return True


async def _watch_payments():
    """Круг опроса. Если ЮKassa не отвечает — прекращаем круг, а не долбим
    её сорока запросами подряд: следующая попытка через двадцать секунд."""
    if not payments.enabled():
        return
    for oid in _pending_payment_orders():
        try:
            await check_payment(oid)
        except payments.PaymentError as e:
            log.warning("Опрос платежа по заказу №%s не удался: %s", oid, e)
            return
        await asyncio.sleep(0.2)   # не выстреливаем очередью в ЮKassa


# ---------- Автоотмена ----------

async def _expire():
    if config.ORDER_HOLD_MINUTES <= 0:
        return
    for row in db.expired_unpaid(config.ORDER_HOLD_MINUTES):
        oid = row["id"]
        order = db.get_order(oid)
        if not order:
            continue

        # Последняя проверка перед отменой: опрос мог не успеть на этом круге,
        # а отменить оплаченный заказ было бы больно.
        if order["payment_id"] and payments.enabled():
            try:
                if await check_payment(oid, source="проверка перед отменой"):
                    continue
            except payments.PaymentError as e:
                # ЮKassa недоступна — лучше подождать, чем отменить вслепую.
                log.warning("Не смог проверить платёж по заказу №%s: %s", oid, e)
                continue

        if db.cancel_if_new(oid):
            order = db.get_order(oid)
            log.info("Заказ №%s отменён по таймауту, принты вернулись в остатки", oid)
            await tgbot.notify_customer_status(order, "expired")
            await tgbot.refresh_or_send_staff_card(order)


async def expire_unpaid_orders_loop():
    """Один цикл на две задачи: сначала спрашиваем ЮKassa про все заказы,
    ждущие оплату, потом отменяем те, у кого вышло время."""
    while True:
        try:
            await _watch_payments()
        except Exception as e:
            log.warning("Опрос платежей упал: %s", e)
        try:
            await _expire()
        except Exception as e:
            log.warning("Проверка просроченных заказов упала: %s", e)
        await asyncio.sleep(CHECK_EVERY_SEC)


# ---------- Накладные ----------

async def sync_shipments_loop():
    """Страховка на случай потерянного уведомления от СДЭК."""
    while True:
        await asyncio.sleep(SHIPMENTS_EVERY_SEC)
        if not cdek.enabled():
            continue
        try:
            for oid in db.active_shipments():
                try:
                    await tgbot.sync_shipment(oid)
                except Exception as e:
                    log.warning("Не смог обновить накладную по заказу №%s: %s", oid, e)
                await asyncio.sleep(1)   # не долбим СДЭК очередью
        except Exception as e:
            log.warning("Проверка накладных упала: %s", e)
