"""Конфигурация. Всё, что бизнес будет крутить руками — здесь или в .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str) -> str:
    v = os.getenv(name, "")
    return v if v.strip() else default


def _flag(name: str, default: str = "false") -> bool:
    return _env(name, default).strip().lower() in ("1", "true", "yes", "on", "да")


# --- Бренд ---
BRAND = _env("BRAND", "ÖMANKÖ")

# --- Telegram ---
BOT_TOKEN = _env("BOT_TOKEN", "")
# Username бота без @. Заполняется автоматически при старте (run.py) —
# нужен, чтобы человек вернулся в бота после оплаты.
BOT_USERNAME = ""
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

# Часовой пояс заведения. Нужен, чтобы дневная квота сбрасывалась в полночь
# по Москве, а не по UTC. 3 = Москва.
TZ_OFFSET_HOURS = int(_env("TZ_OFFSET_HOURS", "3"))

# --- Оплата ---
# manual   — сотрудник подтверждает оплату кнопкой (СБП-перевод, ссылка руками)
# yookassa — автоматика через ЮKassa (нужны YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY)
PAYMENT_MODE = _env("PAYMENT_MODE", "manual")

YOOKASSA_SHOP_ID = _env("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = _env("YOOKASSA_SECRET_KEY", "")

# Передавать ли состав чека в платеже (54-ФЗ).
# false — пока онлайн-касса (Атол) не подключена к ЮKassa.
# true  — включить в тот же день, когда Паша подключит кассу. Раньше нельзя
#         (чек уйдёт в никуда), позже нельзя (платежи начнут отваливаться).
YOOKASSA_SEND_RECEIPT = _flag("YOOKASSA_SEND_RECEIPT", "false")
# Ставка НДС в чеке. Коды ЮKassa, число от 1 до 12:
#   1 — без НДС           5 — расчётная 10/110    9  — расчётная 5/105
#   2 — 0%                6 — расчётная 20/120    10 — расчётная 7/107
#   3 — 10%               7 — 5%  (УСН)           11 — 22%
#   4 — 20%               8 — 7%  (УСН)           12 — расчётная 22/122
# У ÖMANKÖ — УСН со ставкой 5%, то есть код 7 (со слов Паши, сентябрь 2026).
# Меняется только вместе с системой налогообложения.
YOOKASSA_VAT_CODE = int(_env("YOOKASSA_VAT_CODE", "7"))
# Признак способа расчёта в чеке:
#   full_payment     — полный расчёт (деньги и товар встречаются сразу)
#   full_prepayment  — полная предоплата (заплатили сейчас, вещь получат позже)
# У нас предоплата 100%: человек платит, футболку печатают после. Так велел
# Паша со стороны кассы — иначе чек не сойдётся с тем, что ждёт Атол.
YOOKASSA_PAYMENT_MODE = _env("YOOKASSA_PAYMENT_MODE", "full_prepayment")

# Сколько минут держим неоплаченный заказ, прежде чем вернуть принты в остатки.
# 0 — не отменять автоматически.
ORDER_HOLD_MINUTES = int(_env("ORDER_HOLD_MINUTES", "30"))

# --- Цены, ₽ ---
BASE_PRICE = int(_env("BASE_PRICE", "3000"))        # футболка, включает INCLUDED_PRINTS принтов
INCLUDED_PRINTS = int(_env("INCLUDED_PRINTS", "3"))
EXTRA_PRINT_PRICE = int(_env("EXTRA_PRINT_PRICE", "300"))
MAX_PRINTS = int(_env("MAX_PRINTS", "5"))           # потолок по времени запечатки
# Принт на рукаве считается обычным принтом: входит в INCLUDED_PRINTS,
# в MAX_PRINTS и в дневную квоту наравне с грудью и спиной.

# --- Квота онлайн-заказов в день ---
ONLINE_QUOTA_PER_DAY = int(_env("ONLINE_QUOTA_PER_DAY", "8"))

# --- Срок хранения готового заказа (показывается пользователю) ---
PICKUP_HOLD_DAYS = int(_env("PICKUP_HOLD_DAYS", "5"))

# --- Точка выдачи (текст в боте) ---
PICKUP_TEXT = os.getenv(
    "PICKUP_TEXT",
    f"Поп-ап {BRAND}, зеркальный павильон Музеона (напротив Парка Горького). "
    "Ежедневно 11:00–21:00.",
)

# =====================================================================
#  Доставка
# =====================================================================
# Самовывоз есть всегда. СДЭК — отдельный флаг: пока он выключен, мини-апп
# показывает один способ получения и ни одного запроса в СДЭК не уходит.
DELIVERY_METHODS = ("pickup", "cdek_pvz", "cdek_door")
DELIVERY_CDEK = _flag("DELIVERY_CDEK", "false")

# Боевой контур или песочница. По умолчанию боевой: реквизиты живут
# в переменных Railway, и молча уехать в песочницу с ними нельзя.
# CDEK_TEST=true — контур api.edu.cdek.ru, реквизиты подставляются сами,
# договор не нужен, заказы настоящими посылками не становятся.
CDEK_TEST = _flag("CDEK_TEST", "false")
CDEK_ACCOUNT = _env("CDEK_ACCOUNT", "")       # Account из ЛК СДЭК → Интеграция
CDEK_PASSWORD = _env("CDEK_PASSWORD", "")     # Secure password оттуда же
# Ключи в код не кладём: они дают право создавать отправления от лица магазина.
# Договор «Интернет-магазин» — ИМ-РФ-ММР-50. В API он не передаётся: тарифы
# 136/137 открыты на самом аккаунте. Номер тут только чтобы не потерялся.

# Откуда уезжает посылка. Схема одна: сотрудник относит пакет в свой ПВЗ,
# дальше СДЭК везёт его получателю. Забор курьером от поп-апа не используется —
# его надо заказывать заранее, а поп-ап работает не по складскому расписанию.
# MSK537 — Москва, 2-й Хвостов переулок, 12. Пн-Пт 10:00-21:00, Сб-Вс 10:00-20:00.
CDEK_SHIPMENT_POINT = _env("CDEK_SHIPMENT_POINT", "MSK537")
CDEK_FROM_CITY_CODE = int(_env("CDEK_FROM_CITY_CODE", "44"))  # 44 = Москва

# Тарифы для договора «Интернет-магазин»:
#   136 — посылка склад→склад (получатель забирает в ПВЗ)
#   137 — посылка склад→дверь (курьер до адреса)
CDEK_TARIFF_PVZ = int(_env("CDEK_TARIFF_PVZ", "136"))
CDEK_TARIFF_DOOR = int(_env("CDEK_TARIFF_DOOR", "137"))

CDEK_SENDER_NAME = _env("CDEK_SENDER_NAME", BRAND)
CDEK_SENDER_PHONE = _env("CDEK_SENDER_PHONE", "")
# Префикс номера заказа на стороне СДЭК: номера должны быть уникальны в рамках
# договора, а «42» слишком легко пересечётся с чем-нибудь ещё.
CDEK_ORDER_PREFIX = _env("CDEK_ORDER_PREFIX", "omanko")

# Габариты посылки: замер Паши, футболка в пакете 40×35×3 см, 500 г.
# Считаем один размер на все заказы — разброс между S и XL тариф не двигает.
# Проверено на боевых тарифах: цена от габаритов в этом диапазоне не зависит
# вообще (договор считает по направлению), так что ужимать пакет смысла нет.
PACKAGE_WEIGHT_G = int(_env("PACKAGE_WEIGHT_G", "500"))
PACKAGE_LENGTH_CM = int(_env("PACKAGE_LENGTH_CM", "40"))
PACKAGE_WIDTH_CM = int(_env("PACKAGE_WIDTH_CM", "35"))
PACKAGE_HEIGHT_CM = int(_env("PACKAGE_HEIGHT_CM", "3"))

# Наценка на тариф СДЭК, ₽ (упаковка, время сотрудника). 0 — продаём в ноль.
DELIVERY_MARKUP_RUB = int(_env("DELIVERY_MARKUP_RUB", "0"))
# Бесплатная доставка от суммы товаров, ₽. 0 — выключено.
FREE_DELIVERY_FROM = int(_env("FREE_DELIVERY_FROM", "0"))

# --- Геометрия изделия ---
# Числа из размерной сетки ÖMANKÖ и замеров бланка, см → мм:
#   A (length_mm)      — длина изделия от высшей точки плеча до низа
#   B (chest_mm)       — ширина по груди под проймой, от края до края
#   C (sleeve_mm)      — длина рукава по внешнему шву
#   sleeve_w_mm        — ширина расправленного рукава по низу, от сгиба до сгиба
SIZES = {
    "S":  {"chest_mm": 570, "length_mm": 710, "sleeve_mm": 210, "sleeve_w_mm": 210},
    "M":  {"chest_mm": 600, "length_mm": 740, "sleeve_mm": 220, "sleeve_w_mm": 210},
    "L":  {"chest_mm": 625, "length_mm": 755, "sleeve_mm": 230, "sleeve_w_mm": 230},
    "XL": {"chest_mm": 650, "length_mm": 785, "sleeve_mm": 245, "sleeve_w_mm": 240},
}

SIDES = ("front", "back", "sleeve_l", "sleeve_r")
SLEEVE_SIDES = ("sleeve_l", "sleeve_r")

# --- Отступы, из которых считается печатная зона ---
# Термопресс зону НЕ ограничивает: каждая наклейка прижимается отдельно,
# футболку под платеном двигают. Ограничение — только сама вещь: швы, низ,
# горловина. Эти четыре числа и есть вся настройка зоны.
SIDE_SEAM_MM = int(_env("SIDE_SEAM_MM", "70"))       # от бокового шва, с каждой стороны
HEM_MM = int(_env("HEM_MM", "100"))                  # от низа изделия
COLLAR_FRONT_MM = int(_env("COLLAR_FRONT_MM", "150"))  # от плечевой линии, перед
COLLAR_BACK_MM = int(_env("COLLAR_BACK_MM", "90"))     # от плечевой линии, спина

# Рукав. Обе стороны зоны считаются из замеров: вдоль руки — из длины C,
# поперёк — из ширины расправленного рукава. Отступ от сгиба нужен потому,
# что по самому сгибу ткань не ложится на платен плоско.
SLEEVE_SIDE_MM = int(_env("SLEEVE_SIDE_MM", "40"))   # от каждого сгиба
SLEEVE_TOP_MM = int(_env("SLEEVE_TOP_MM", "40"))     # отступ от проймы
SLEEVE_CUFF_MM = int(_env("SLEEVE_CUFF_MM", "40"))   # отступ от манжета

# Минимальный просвет между принтами. Не про наложение: пресс прижимает
# наклейки по одной, и край платена не должен лечь на уже запечатанный
# соседний принт.
MIN_GAP_MM = int(_env("MIN_GAP_MM", "15"))


def zone(size: str, side: str) -> dict:
    """Печатная зона: ширина, высота и отступ верхнего края.

    top_mm для груди и спины отсчитывается от плечевой линии,
    для рукава — от проймы.
    """
    g = SIZES[size]
    if side in SLEEVE_SIDES:
        return {
            "w_mm": g["sleeve_w_mm"] - 2 * SLEEVE_SIDE_MM,
            "h_mm": g["sleeve_mm"] - SLEEVE_TOP_MM - SLEEVE_CUFF_MM,
            "top_mm": SLEEVE_TOP_MM,
        }
    top = COLLAR_FRONT_MM if side == "front" else COLLAR_BACK_MM
    return {
        "w_mm": g["chest_mm"] - 2 * SIDE_SEAM_MM,
        "h_mm": g["length_mm"] - top - HEM_MM,
        "top_mm": top,
    }


def zones(size: str) -> dict:
    return {s: zone(size, s) for s in SIDES}


# --- Привязка фотографии бланка к миллиметрам ---
# Ориентиры, снятые с webapp/shirt_front.webp и shirt_back.webp (оба 1171×902).
# Фото сжато по вертикали до пропорций реального бланка, поэтому масштаб
# по ширине и по высоте одинаковый. Меняешь снимок — пересними и эти числа.
SHIRT_PHOTO = {
    "w_px": 1171,
    "h_px": 902,
    "shoulder_y": 24,    # высшая точка плеча, отсюда отсчитывается длина A
    "hem_y": 896,        # низ изделия
    "chest_x0": 231,     # левый край корпуса под проймой
    "chest_x1": 938,     # правый край корпуса под проймой
}

# Вертикальная растяжка снимка — параметр чисто внешний.
# Исходное фото было сток-мокапом: он примерно на 11% длиннее реального бланка,
# и мы сжали картинку до настоящих пропорций. Из-за этого футболка на экране
# выглядит приплюснутой. Здесь её можно вернуть к исходному виду:
#   1.0  — как есть, пропорции совпадают с вещью
#   1.11 — вид мокапа, футболка выглядит естественно
# На миллиметры не влияет: масштаб берётся от ширины по груди, а верх печатной
# зоны привязан к линии плеча, которая едет вместе с картинкой. Плата за
# растяжку — принт визуально окажется чуть выше относительно низа футболки,
# чем будет на вещи. Спека для сотрудника при этом остаётся точной.
PHOTO_STRETCH_Y = float(_env("PHOTO_STRETCH_Y", "1.0"))

# Остатки бланков по размерам. 0 = размер скрыт. Обновляется в .env или прямо тут.
SHIRT_STOCK = {
    s.split(":")[0]: int(s.split(":")[1])
    for s in _env("SHIRT_STOCK", "S:3,M:23,L:95,XL:50").split(",")
}

# База лежит на постоянном диске (на Railway — том, смонтированный в /data),
# чтобы заказы не пропадали при обновлении. Локально — папка data/ в проекте.
DATA_DIR = Path(_env("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "omanko.db"
# Старое имя файла базы — если оно есть на диске, переименуем при старте.
LEGACY_DB_PATH = DATA_DIR / "amang.db"

# Картинки принтов лежат прямо в репозитории — их загружают через GitHub
STICKERS_DIR = BASE_DIR / "stickers"
STICKERS_CSV = STICKERS_DIR / "stickers.csv"
