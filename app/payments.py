"""Оплата.

Режим manual (сейчас): сотрудник подтверждает оплату кнопкой в карточке заказа.
Режим ypmn (после ответа Дениса): заполнить create_payment_url() и verify_webhook().

Что нужно от ypmn:
  1) hosted payment page: создать платёж по API → получить URL, отдать его мини-аппу;
     в платеже передать return_url = f"{WEBAPP_URL}/paid?order={oid}"
  2) webhook об успехе → роут /api/payments/ypmn/webhook в api.py
  3) состав чека (позиции) в платеже, чтобы Атол Онлайн пробил корректно:
     - "Футболка кастомная, размер X" — BASE_PRICE
     - "Доп. принт" × N — EXTRA_PRINT_PRICE
"""
from . import config


class PaymentError(Exception):
    pass


def create_payment_url(order: dict) -> str | None:
    """Вернуть URL оплаты или None (режим manual — платёж подтверждают руками)."""
    if config.PAYMENT_MODE == "manual":
        return None
    if config.PAYMENT_MODE == "ypmn":
        # TODO после ответа Дениса:
        # resp = requests.post("https://api.ypmn.ru/...", json={
        #     "merchant": config.YPMN_MERCHANT_ID,
        #     "amount": order["price"] * 100,
        #     "order_id": order["id"],
        #     "return_url": f"{config.WEBAPP_URL}/webapp/?paid={order['id']}",
        #     "receipt": build_receipt(order),
        # }, ...)
        # return resp.json()["payment_url"]
        raise PaymentError("ypmn ещё не подключён — заполни app/payments.py")
    raise PaymentError(f"Неизвестный PAYMENT_MODE: {config.PAYMENT_MODE}")


def verify_webhook(headers: dict, body: bytes) -> int | None:
    """Проверить подпись вебхука ypmn, вернуть order_id оплаченного заказа.
    TODO: реализовать по документации ypmn (подпись через YPMN_SECRET)."""
    return None
