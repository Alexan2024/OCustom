"""Запуск: сайт мини-аппа + бот в одном процессе."""
import asyncio
import logging

import uvicorn

from app import cdek, config, db, jobs
from app.api import app
from app.bot import bot, dp

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("start")


async def main():
    db.init_db()

    # Username нужен, чтобы человек вернулся в бота со страницы оплаты.
    try:
        me = await bot.get_me()
        config.BOT_USERNAME = me.username or ""
        log.info("Бот: @%s", config.BOT_USERNAME)
    except Exception as e:
        log.warning("Не удалось узнать username бота: %s", e)

    log.info("Адрес мини-аппа: %s/webapp/", config.WEBAPP_URL)
    log.info("Режим оплаты: %s", config.PAYMENT_MODE)
    if config.PAYMENT_MODE == "yookassa":
        if not (config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY):
            log.error("PAYMENT_MODE=yookassa, но не заданы YOOKASSA_SHOP_ID / "
                      "YOOKASSA_SECRET_KEY — ссылки на оплату создаваться не будут")
        log.info("Адрес для HTTP-уведомлений ЮKassa: %s/api/payments/yookassa/webhook",
                 config.WEBAPP_URL)
        log.info("Чек по 54-ФЗ: %s",
                 "передаётся" if config.YOOKASSA_SEND_RECEIPT else "выключен")

    if cdek.enabled():
        log.info("Доставка СДЭК включена (%s)",
                 "ПЕСОЧНИЦА, настоящих посылок не будет" if config.CDEK_TEST
                 else "боевой контур")
        if not config.CDEK_TEST and not (config.CDEK_ACCOUNT and config.CDEK_PASSWORD):
            log.error("DELIVERY_CDEK=true и CDEK_TEST=false, но не заданы "
                      "CDEK_ACCOUNT / CDEK_PASSWORD — доставка считаться не будет")
        log.info("Отправляем из ПВЗ %s (город %s)",
                 config.CDEK_SHIPMENT_POINT or "— не задан!", config.CDEK_FROM_CITY_CODE)
        if not config.CDEK_SHIPMENT_POINT:
            log.error("Не задан CDEK_SHIPMENT_POINT — накладные создаваться не будут")
        log.info("Адрес для уведомлений СДЭК: %s", cdek.webhook_url())
        try:
            await cdek.ensure_webhook()
        except Exception as e:
            log.warning("Подписка на статусы СДЭК не оформилась: %s", e)
    else:
        log.info("Доставка СДЭК выключена — только самовывоз")

    if not config.STAFF_CHAT_ID:
        log.warning("STAFF_CHAT_ID не задан — заказы не будут приходить сотрудникам")

    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=config.PORT, log_level="info")
    )
    await asyncio.gather(
        server.serve(),
        dp.start_polling(bot),
        jobs.expire_unpaid_orders_loop(),
        jobs.sync_shipments_loop(),
    )


if __name__ == "__main__":
    if not config.BOT_TOKEN:
        raise SystemExit("Не задана переменная BOT_TOKEN — добавь её в настройках Railway")
    asyncio.run(main())
