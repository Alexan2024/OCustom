# AMANG custom station

Telegram-бот и мини-апп: покупатель собирает футболку из готовых DTF-принтов
(в реальном масштабе), сотрудник получает спеку в миллиметрах и запечатывает.

**Запуск описан в файле `ИНСТРУКЦИЯ.md` — открывай его, а не этот файл.**

Техническая справка:

```
run.py            запуск (веб + бот одним процессом)
app/config.py     цены, размеры, печатные зоны — читаются из переменных окружения
app/db.py         SQLite, резерв остатков, автоимпорт stickers/stickers.csv
app/api.py        API мини-аппа + серверная валидация геометрии
app/bot.py        /start, /chatid, карточки заказов, статусы
app/payments.py   ручной режим сейчас, заготовка под ypmn
app/auth.py       проверка подписи Telegram initData
webapp/           мини-апп, без сборки (HTML + CSS + JS)
stickers/         картинки принтов + stickers.csv (каталог)
```

Локально: `pip install -r requirements.txt`, задать `BOT_TOKEN` и `WEBAPP_URL`
(HTTPS-туннель, например `cloudflared tunnel --url http://localhost:8080`),
затем `python run.py`.
