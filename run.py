"""Запуск: сайт мини-аппа + бот в одном процессе."""
import asyncio
import logging

import uvicorn

from app import config, db
from app.api import app
from app.bot import bot, dp

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("start")


async def main():
    db.init_db()
    log.info("Адрес мини-аппа: %s/webapp/", config.WEBAPP_URL)
    if not config.STAFF_CHAT_ID:
        log.warning("STAFF_CHAT_ID не задан — заказы не будут приходить сотрудникам")
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=config.PORT, log_level="info")
    )
    await asyncio.gather(server.serve(), dp.start_polling(bot))


if __name__ == "__main__":
    if not config.BOT_TOKEN:
        raise SystemExit("Не задана переменная BOT_TOKEN — добавь её в настройках Railway")
    asyncio.run(main())
