"""Оплата через ЮKassa (API v3).

Два режима, переключаются переменной PAYMENT_MODE:

  manual   — сотрудник скидывает реквизиты руками и жмёт «Оплачен ✓» в карточке.
             Рабочая схема, оставлена как запасной путь.
  yookassa — создаём платёж по API, отдаём человеку ссылку, факт оплаты ловим
             вебхуком.

Про безопасность вебхука: тело уведомления мы НЕ считаем доказательством оплаты.
Из него берём только id платежа, после чего сами спрашиваем у ЮKassa
GET /v3/payments/{id} и сверяем статус и сумму. Подделать такое нельзя.

Про чек (54-ФЗ): состав чека собирается, только если YOOKASSA_SEND_RECEIPT=true.
Включать в тот же день, когда онлайн-касса будет подключена к ЮKassa: раньше —
чек уйдёт в никуда, позже — магазин начнёт отклонять платежи без чека.
"""
import logging
import re

import httpx

from . import config

log = logging.getLogger("payments")

API_URL = "https://api.yookassa.ru/v3"
TIMEOUT = httpx.Timeout(20.0)


class PaymentError(Exception):
    pass


def enabled() -> bool:
    return config.PAYMENT_MODE == "yookassa"


def _auth() -> tuple[str, str]:
    if not config.YOOKASSA_SHOP_ID or not config.YOOKASSA_SECRET_KEY:
        raise PaymentError("Не заданы YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY")
    return (config.YOOKASSA_SHOP_ID, config.YOOKASSA_SECRET_KEY)


def _rub(value: int | float) -> str:
    """ЮKassa ждёт сумму строкой с двумя знаками: 3000 → '3000.00'."""
    return f"{float(value):.2f}"


def normalize_phone(raw: str | None) -> str | None:
    """+7 (900) 123-45-67 → 79001234567. ЮKassa хочет только цифры."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return digits if 11 <= len(digits) <= 15 else None


def return_url(oid: int) -> str:
    """Куда человек попадёт после оплаты.

    Возвращаем в бота, а не в мини-апп: страница оплаты открывается во внешнем
    браузере, и обычная ссылка на мини-апп там не развернётся.
    """
    if config.BOT_USERNAME:
        return f"https://t.me/{config.BOT_USERNAME}?start=paid_{oid}"
    return f"{config.WEBAPP_URL}/webapp/"


def build_receipt(order: dict) -> dict | None:
    """Состав чека. Позиции обязаны в сумме давать цену заказа."""
    phone = normalize_phone(order.get("phone"))
    if not phone:
        raise PaymentError("Для чека нужен телефон покупателя")

    items = [{
        "description": f"Футболка {config.BRAND} с печатью, размер {order['size']}"[:128],
        "quantity": "1.00",
        "amount": {"value": _rub(config.BASE_PRICE), "currency": "RUB"},
        "vat_code": config.YOOKASSA_VAT_CODE,
        "payment_mode": "full_payment",
        "payment_subject": "commodity",
    }]
    extra = max(0, len(order["items"]) - config.INCLUDED_PRINTS)
    if extra:
        items.append({
            "description": "Дополнительный принт",
            "quantity": f"{extra}.00",
            "amount": {"value": _rub(config.EXTRA_PRINT_PRICE), "currency": "RUB"},
            "vat_code": config.YOOKASSA_VAT_CODE,
            "payment_mode": "full_payment",
            "payment_subject": "commodity",
        })
    total = config.BASE_PRICE + extra * config.EXTRA_PRINT_PRICE
    if total != order["price"]:
        raise PaymentError(
            f"Чек не сходится с заказом: {total} ≠ {order['price']} ₽")
    return {"customer": {"phone": phone}, "items": items}


async def create_payment(order: dict, attempt: int = 1) -> tuple[str, str] | None:
    """Создаёт платёж. Возвращает (ссылка на оплату, id платежа) или None
    в ручном режиме."""
    if config.PAYMENT_MODE == "manual":
        return None
    if not enabled():
        raise PaymentError(f"Неизвестный PAYMENT_MODE: {config.PAYMENT_MODE}")

    oid = order["id"]
    payload = {
        "amount": {"value": _rub(order["price"]), "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url(oid)},
        "description": f"Заказ №{oid}, {config.BRAND}"[:128],
        "metadata": {"order_id": str(oid)},
    }
    if config.YOOKASSA_SEND_RECEIPT:
        payload["receipt"] = build_receipt(order)

    headers = {
        # Один и тот же ключ на повторный запрос вернёт тот же платёж,
        # а не создаст второй. attempt меняем, если делаем новую ссылку.
        "Idempotence-Key": f"omanko-order-{oid}-{attempt}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(f"{API_URL}/payments", json=payload,
                                  headers=headers, auth=_auth())
    except httpx.HTTPError as e:
        raise PaymentError(f"ЮKassa недоступна: {e}") from e

    if r.status_code >= 400:
        raise PaymentError(f"ЮKassa вернула {r.status_code}: {r.text[:400]}")

    data = r.json()
    url = (data.get("confirmation") or {}).get("confirmation_url")
    if not url:
        raise PaymentError(f"В ответе нет ссылки на оплату: {str(data)[:400]}")
    return url, data["id"]


async def fetch_payment(payment_id: str) -> dict:
    """Спрашиваем у ЮKassa настоящее состояние платежа."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(f"{API_URL}/payments/{payment_id}", auth=_auth())
    except httpx.HTTPError as e:
        raise PaymentError(f"ЮKassa недоступна: {e}") from e
    if r.status_code >= 400:
        raise PaymentError(f"ЮKassa вернула {r.status_code}: {r.text[:400]}")
    return r.json()


def _amount_matches(payment: dict, order: dict) -> bool:
    try:
        return abs(float(payment["amount"]["value"]) - float(order["price"])) < 0.01
    except (KeyError, TypeError, ValueError):
        return False


async def confirm_payment(payment_id: str, order: dict) -> bool:
    """True, если платёж действительно прошёл и сумма совпадает с заказом."""
    payment = await fetch_payment(payment_id)
    if payment.get("status") != "succeeded":
        return False
    if not _amount_matches(payment, order):
        log.error("Сумма платежа %s не совпадает с заказом №%s", payment_id, order["id"])
        return False
    return True


def order_id_from_webhook(body: dict) -> tuple[int | None, str | None]:
    """Достаёт (order_id, payment_id) из уведомления. Ничему в теле не верим —
    это только указатель, по которому дальше пойдём в API."""
    obj = body.get("object") or {}
    payment_id = obj.get("id")
    raw = (obj.get("metadata") or {}).get("order_id")
    try:
        oid = int(raw)
    except (TypeError, ValueError):
        oid = None
    return oid, payment_id
