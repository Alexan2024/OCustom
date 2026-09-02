"""Фоновые задачи.

Пока одна: неоплаченные заказы не должны вечно держать принты в резерве.
DTF-переносы — физические остатки, каждый брошенный заказ прячет от других
покупателей реальную наклейку, которая лежит на складе.
"""
import asyncio
import logging

from . import bot as tgbot, config, db, payments

log = logging.getLogger("jobs")

CHECK_EVERY_SEC = 120


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
