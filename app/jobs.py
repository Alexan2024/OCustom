"""Фоновые задачи.

Первая: неоплаченные заказы не должны вечно держать принты в резерве.
DTF-переносы — физические остатки, каждый брошенный заказ прячет от других
покупателей реальную наклейку, которая лежит на складе.

Вторая: доопрос накладных СДЭК. Вебхук — основной путь, но он может
потеряться, и человек останется без трек-номера. Раз в десять минут
проходим по активным отправлениям и спрашиваем СДЭК сами.
"""
import asyncio
import logging

from . import bot as tgbot, cdek, config, db, payments

log = logging.getLogger("jobs")

CHECK_EVERY_SEC = 120
SHIPMENTS_EVERY_SEC = 600


async def expire_unpaid_orders_loop():
    while True:
        try:
            await _tick()
        except Exception as e:
            log.warning("Проверка просроченных заказов упала: %s", e)
        await asyncio.sleep(CHECK_EVERY_SEC)


async def _tick():
    if config.ORDER_HOLD_MINUTES <= 0:
        return
    for row in db.expired_unpaid(config.ORDER_HOLD_MINUTES):
        oid = row["id"]
        order = db.get_order(oid)
        if not order:
            continue

        # Перед отменой убеждаемся, что деньги правда не пришли:
        # вебхук мог потеряться, и отменить оплаченный заказ было бы больно.
        if order["payment_id"] and payments.enabled():
            try:
                if await payments.confirm_payment(order["payment_id"], order):
                    if db.mark_paid(oid, order["payment_id"]):
                        order = db.get_order(oid)
                        await tgbot.notify_customer_status(order, "paid")
                        await tgbot.refresh_or_send_staff_card(order)
                        log.info("Заказ №%s оплачен, вебхук не дошёл", oid)
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
