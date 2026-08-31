"""Конфигурация. Всё, что бизнес будет крутить руками — здесь или в .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# --- Telegram ---
def _env(name: str, default: str) -> str:
    v = os.getenv(name, "")
    return v if v.strip() else default


BOT_TOKEN = _env("BOT_TOKEN", "")
# Чат сотрудников (группа), куда падают карточки заказов. ID вида -100xxxxxxxxxx
STAFF_CHAT_ID = int(_env("STAFF_CHAT_ID", "0"))
# Публичный HTTPS-адрес, где крутится этот сервер (нужен для кнопки мини-аппа)
_railway_domain = _env("RAILWAY_PUBLIC_DOMAIN", "")
WEBAPP_URL = _env(
    "WEBAPP_URL",
    f"https://{_railway_domain}" if _railway_domain else "https://example.com",
).rstrip("/")

# Порт задаёт хостинг (Railway подставляет PORT сам)
PORT = int(_env("PORT", "8080"))

# --- Оплата ---
# manual  — сотрудник подтверждает оплату кнопкой (СБП-перевод, оплата по ссылке руками)
# ypmn    — автоматика через ypmn (заполнить app/payments.py после ответа Дениса)
PAYMENT_MODE = _env("PAYMENT_MODE", "manual")
YPMN_MERCHANT_ID = os.getenv("YPMN_MERCHANT_ID", "")
YPMN_SECRET = os.getenv("YPMN_SECRET", "")

# --- Цены, ₽ ---
BASE_PRICE = int(_env("BASE_PRICE", "3000"))        # футболка, включает INCLUDED_PRINTS принтов
INCLUDED_PRINTS = int(_env("INCLUDED_PRINTS", "3"))
EXTRA_PRINT_PRICE = int(_env("EXTRA_PRINT_PRICE", "300"))
MAX_PRINTS = int(_env("MAX_PRINTS", "5"))           # потолок по времени запечатки

# --- Квота онлайн-заказов в день ---
ONLINE_QUOTA_PER_DAY = int(_env("ONLINE_QUOTA_PER_DAY", "8"))

# --- Срок хранения готового заказа (показывается пользователю) ---
PICKUP_HOLD_DAYS = int(_env("PICKUP_HOLD_DAYS", "5"))

# --- Точка выдачи (текст в боте) ---
PICKUP_TEXT = os.getenv(
    "PICKUP_TEXT",
    "Поп-ап AMANG, зеркальный павильон Музеона (напротив Парка Горького). Ежедневно 11:00–21:00.",
)

# --- Размеры и печатные зоны ---
# Печатная зона: прямоугольник на груди/спине, в который можно ставить принты.
# Ширина/высота в мм. Начало координат — верх-центр зоны.
SIZES = {
    "S":   {"print_w_mm": 280, "print_h_mm": 400},
    "M":   {"print_w_mm": 300, "print_h_mm": 420},
    "L":   {"print_w_mm": 320, "print_h_mm": 440},
    "XL":  {"print_w_mm": 340, "print_h_mm": 460},
    "XXL": {"print_w_mm": 360, "print_h_mm": 480},
}

# Остатки бланков по размерам. 0 = размер скрыт. Обновляется в .env или прямо тут.
SHIRT_STOCK = {
    s.split(":")[0]: int(s.split(":")[1])
    for s in _env("SHIRT_STOCK", "S:5,M:10,L:10,XL:5,XXL:3").split(",")
}

# База лежит на постоянном диске (на Railway — том, смонтированный в /data),
# чтобы заказы не пропадали при обновлении. Локально — папка data/ в проекте.
DATA_DIR = Path(_env("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "amang.db"

# Картинки принтов лежат прямо в репозитории — их загружают через GitHub
STICKERS_DIR = BASE_DIR / "stickers"
STICKERS_CSV = STICKERS_DIR / "stickers.csv"
