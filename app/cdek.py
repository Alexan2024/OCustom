"""Доставка СДЭК (API v2).

Включается флагом DELIVERY_CDEK. Пока он выключен, в мини-аппе остаётся
один способ получения — самовывоз, и ни одного запроса в СДЭК не уходит.

Схема отправки одна: сотрудник относит пакет в свой ПВЗ
(CDEK_SHIPMENT_POINT), дальше СДЭК везёт его либо в ПВЗ получателя
(тариф 136), либо курьером до двери (тариф 137). Забор курьером от поп-апа
не используем: его надо заказывать заранее, а поп-ап работает не по
складскому расписанию.

Про доверие к вебхуку — та же логика, что в оплате: тело уведомления
доказательством не считается. Из него берём только uuid накладной, после чего
сами спрашиваем GET /v2/orders/{uuid}. Плюс фоновая доопрашивалка в jobs.py
на случай, если уведомление не дошло.
"""
import asyncio
import logging
import time

import httpx

from . import config

log = logging.getLogger("cdek")

PROD_URL = "https://api.cdek.ru/v2"
TEST_URL = "https://api.edu.cdek.ru/v2"

# Публичные реквизиты песочницы СДЭК — они одни и те же для всех,
# заводить ничего не нужно. В бой попасть с ними нельзя.
TEST_ACCOUNT = "wqGwiQx0gg8mLtiEKsUinjVSICCjtTEP"
TEST_PASSWORD = "RmAmgvSgSl1yirlz9QupbzOJVqhCxcP5"

TIMEOUT = httpx.Timeout(25.0)

# Человеческие названия статусов. СДЭК присылает и своё name, но оно
# бывает канцелярским — где знаем перевод получше, показываем свой.
STATUS_TEXT = {
    "ACCEPTED": "Накладная создана",
    "CREATED": "Накладная создана",
    "RECEIVED_AT_SHIPMENT_WAREHOUSE": "Принят на склад отправителя",
    "READY_FOR_SHIPMENT_IN_SENDER_CITY": "Готов к отправке из города-отправителя",
    "TAKEN_BY_TRANSPORTER_FROM_SENDER_CITY": "Уехал из города-отправителя",
    "SENT_TO_TRANSIT_CITY": "В пути",
    "ACCEPTED_IN_TRANSIT_CITY": "В пути",
    "SENT_TO_RECIPIENT_CITY": "В пути в город получателя",
    "ACCEPTED_IN_RECIPIENT_CITY": "Прибыл в город получателя",
    "ACCEPTED_AT_PICK_UP_POINT": "Готов к выдаче в пункте",
    "READY_FOR_SHIPMENT_IN_RECIPIENT_CITY": "Готов к выдаче",
    "TAKEN_BY_COURIER": "Передан курьеру",
    "DELIVERED": "Вручён получателю",
    "NOT_DELIVERED": "Не вручён",
    "RETURNED_TO_SENDER": "Возвращён отправителю",
    "INVALID": "Накладная отклонена",
}

# Статусы, после которых заказ можно закрывать
DONE_STATUSES = ("DELIVERED",)
# Статусы, о которых надо позвать живого человека
ALERT_STATUSES = ("NOT_DELIVERED", "RETURNED_TO_SENDER", "INVALID")


class CdekError(Exception):
    pass


def enabled() -> bool:
    return config.DELIVERY_CDEK


def _base() -> str:
    return TEST_URL if config.CDEK_TEST else PROD_URL


def _creds() -> tuple[str, str]:
    if config.CDEK_TEST:
        return (config.CDEK_ACCOUNT or TEST_ACCOUNT,
                config.CDEK_PASSWORD or TEST_PASSWORD)
    if not (config.CDEK_ACCOUNT and config.CDEK_PASSWORD):
        raise CdekError("Не заданы CDEK_ACCOUNT / CDEK_PASSWORD")
    return (config.CDEK_ACCOUNT, config.CDEK_PASSWORD)


_token = {"value": "", "exp": 0.0}
_token_lock = asyncio.Lock()


async def _access_token() -> str:
    """Токен живёт час. Держим его в памяти и обновляем за минуту до конца."""
    async with _token_lock:
        if _token["value"] and time.time() < _token["exp"]:
            return _token["value"]
        acc, pwd = _creds()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.post(
                    f"{_base()}/oauth/token?parameters",
                    data={"grant_type": "client_credentials",
                          "client_id": acc, "client_secret": pwd},
                )
        except httpx.HTTPError as e:
            raise CdekError(f"СДЭК недоступен: {e}") from e
        if r.status_code >= 400:
            raise CdekError(f"СДЭК не выдал токен ({r.status_code}): {r.text[:300]}")
        data = r.json()
        _token["value"] = data.get("access_token", "")
        _token["exp"] = time.time() + int(data.get("expires_in", 3600)) - 60
        if not _token["value"]:
            raise CdekError("В ответе СДЭК нет access_token")
        return _token["value"]


async def _call(method: str, path: str, *, params=None, json=None):
    tok = await _access_token()
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.request(method, f"{_base()}{path}", params=params,
                                json=json, headers=headers)
    except httpx.HTTPError as e:
        raise CdekError(f"СДЭК недоступен: {e}") from e
    if r.status_code == 401:
        # токен могли отозвать — сбрасываем и пробуем ещё раз, один раз
        _token["exp"] = 0
        tok = await _access_token()
        headers["Authorization"] = f"Bearer {tok}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.request(method, f"{_base()}{path}", params=params,
                                    json=json, headers=headers)
        except httpx.HTTPError as e:
            raise CdekError(f"СДЭК недоступен: {e}") from e
    if r.status_code >= 400:
        raise CdekError(f"СДЭК вернул {r.status_code}: {r.text[:400]}")
    try:
        return r.json()
    except ValueError as e:
        raise CdekError("СДЭК ответил не-JSON") from e


def _errors(payload) -> str:
    """Собирает текст ошибок из ответа СДЭК (он их прячет в разных местах)."""
    out = []
    if isinstance(payload, dict):
        for e in payload.get("errors") or []:
            out.append(e.get("message") or e.get("code") or str(e))
        for req in payload.get("requests") or []:
            for e in req.get("errors") or []:
                out.append(e.get("message") or e.get("code") or str(e))
    return "; ".join(out)


# ---------- Справочники ----------

async def search_cities(q: str, limit: int = 15) -> list[dict]:
    """Подсказка городов по началу названия.

    Именно /location/suggest/cities, а не /location/cities: второй ищет
    по точному совпадению названия и на «Санкт» не отвечает ничем.
    Подсказка возвращает full_name вида «Казань, городской округ Казань,
    Татарстан, Россия» — режем его на название и уточнение, чтобы человек
    не путал двадцать одинаковых Казацких.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return []
    data = await _call("GET", "/location/suggest/cities",
                       params={"name": q, "country_code": "RU"})
    out = []
    for c in data if isinstance(data, list) else []:
        full = (c.get("full_name") or "").split(",")
        name = full[0].strip() if full else ""
        # последний кусок — «Россия», он ничего не уточняет
        region = ", ".join(p.strip() for p in full[1:-1]) if len(full) > 2 else ""
        if c.get("code") and name:
            out.append({"code": c["code"], "city": name, "region": region})
        if len(out) >= limit:
            break
    return out


async def delivery_points(city_code: int, limit: int = 300) -> list[dict]:
    """Пункты выдачи в городе. Постаматы не берём: футболка в пакете туда
    влезет, но габариты ячеек у них разные, и промах виден только на месте."""
    data = await _call("GET", "/deliverypoints", params={
        "city_code": city_code, "type": "PVZ", "country_code": "RU",
        "is_handout": "true",
    })
    need_kg = config.PACKAGE_WEIGHT_G / 1000
    out = []
    for p in data if isinstance(data, list) else []:
        wmax = p.get("weight_max") or 0
        if wmax and wmax < need_kg:
            continue
        loc = p.get("location") or {}
        out.append({
            "code": p.get("code"),
            "name": p.get("name") or "",
            "address": loc.get("address_full") or loc.get("address") or "",
            "city": loc.get("city") or "",
            "work_time": p.get("work_time") or "",
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
        })
        if len(out) >= limit:
            break
    return [p for p in out if p["code"] and p["address"]]


# ---------- Тариф ----------

def tariff_code(method: str) -> int:
    return config.CDEK_TARIFF_DOOR if method == "cdek_door" else config.CDEK_TARIFF_PVZ


def _package_body() -> dict:
    return {
        "weight": config.PACKAGE_WEIGHT_G,
        "length": config.PACKAGE_LENGTH_CM,
        "width": config.PACKAGE_WIDTH_CM,
        "height": config.PACKAGE_HEIGHT_CM,
    }


async def calc(method: str, city_code: int) -> dict:
    """Стоимость и срок доставки. Цену округляем вверх до рубля и добавляем
    наценку — упаковка и время сотрудника тоже чего-то стоят."""
    data = await _call("POST", "/calculator/tariff", json={
        "type": 1,
        "tariff_code": tariff_code(method),
        "from_location": {"code": config.CDEK_FROM_CITY_CODE},
        "to_location": {"code": city_code},
        "packages": [_package_body()],
    })
    err = _errors(data)
    if err:
        raise CdekError(err)
    raw = data.get("total_sum")
    if raw is None:
        raw = data.get("delivery_sum")
    if raw is None:
        raise CdekError("СДЭК не вернул стоимость доставки")
    import math
    price = int(math.ceil(float(raw))) + config.DELIVERY_MARKUP_RUB
    return {
        "price": max(0, price),
        "period_min": data.get("period_min"),
        "period_max": data.get("period_max"),
    }


async def price_for(order_like: dict) -> int:
    """Стоимость доставки для набора полей заказа. pickup — всегда 0."""
    if order_like.get("method") == "pickup":
        return 0
    return (await calc(order_like["method"], order_like["city_code"]))["price"]


# ---------- Накладная ----------

def _phone(raw: str | None) -> str:
    from .payments import normalize_phone
    digits = normalize_phone(raw)
    if not digits:
        raise CdekError("У получателя нет телефона в понятном формате")
    return "+" + digits


async def create_shipment(order: dict) -> str:
    """Заводит заказ в СДЭК. Возвращает uuid накладной.

    Номер трека присваивается не мгновенно — его подхватит fetch_shipment.
    """
    if not enabled():
        raise CdekError("Доставка СДЭК выключена (DELIVERY_CDEK)")
    if order["delivery_method"] == "pickup":
        raise CdekError("Это заказ на самовывоз")
    if not config.CDEK_SHIPMENT_POINT:
        raise CdekError("Не задан CDEK_SHIPMENT_POINT — некуда сдавать посылку")

    oid = order["id"]
    goods = int(order["price"]) - int(order.get("delivery_price") or 0)
    payload = {
        "type": 1,
        "number": f"{config.CDEK_ORDER_PREFIX}-{oid}",
        "tariff_code": tariff_code(order["delivery_method"]),
        "comment": f"Заказ №{oid}, {config.BRAND}",
        "shipment_point": config.CDEK_SHIPMENT_POINT,
        "recipient": {
            "name": order.get("recipient_name") or order.get("first_name") or "Получатель",
            "phones": [{"number": _phone(order.get("phone"))}],
        },
        "packages": [{
            "number": str(oid),
            **_package_body(),
            "items": [{
                "name": f"Футболка {config.BRAND}, размер {order['size']}"[:255],
                "ware_key": f"tshirt-{order['size']}"[:50],
                # Заказ уже оплачен онлайн, наложенного платежа нет.
                "payment": {"value": 0},
                "cost": goods,
                "weight": config.PACKAGE_WEIGHT_G,
                "amount": 1,
            }],
        }],
    }
    if config.CDEK_SENDER_NAME:
        sender = {"name": config.CDEK_SENDER_NAME}
        if config.CDEK_SENDER_PHONE:
            sender["phones"] = [{"number": _phone(config.CDEK_SENDER_PHONE)}]
        payload["sender"] = sender

    if order["delivery_method"] == "cdek_pvz":
        if not order.get("pvz_code"):
            raise CdekError("В заказе не выбран пункт выдачи")
        payload["delivery_point"] = order["pvz_code"]
    else:
        if not order.get("address"):
            raise CdekError("В заказе нет адреса доставки")
        payload["to_location"] = {
            "code": order["city_code"],
            "address": order["address"],
        }

    data = await _call("POST", "/orders", json=payload)
    err = _errors(data)
    if err:
        raise CdekError(err)
    uuid = ((data.get("entity") or {}).get("uuid"))
    if not uuid:
        raise CdekError(f"В ответе СДЭК нет uuid: {str(data)[:300]}")
    return uuid


async def fetch_shipment(uuid: str) -> dict:
    """Состояние накладной: номер трека, код и текст последнего статуса.

    Возвращает {"number", "status", "text", "invalid", "error"}.
    """
    data = await _call("GET", f"/orders/{uuid}")
    entity = data.get("entity") or {}
    requests = data.get("requests") or []

    invalid = any(r.get("state") == "INVALID" for r in requests)
    error = _errors({"requests": requests}) if invalid else ""

    statuses = entity.get("statuses") or []
    code, text = "", ""
    if statuses:
        last = statuses[-1]
        code = last.get("code") or ""
        text = STATUS_TEXT.get(code) or last.get("name") or code
    if invalid and not code:
        code, text = "INVALID", STATUS_TEXT["INVALID"]

    return {
        "number": entity.get("cdek_number") or "",
        "status": code,
        "text": text,
        "invalid": invalid,
        "error": error,
    }


def uuid_from_webhook(body: dict) -> str | None:
    """Из уведомления берём только uuid — дальше идём в API сами."""
    if not isinstance(body, dict):
        return None
    attrs = body.get("attributes") or {}
    return body.get("uuid") or attrs.get("cdek_order_uuid") or None


def tracking_url(number: str) -> str:
    return f"https://www.cdek.ru/ru/tracking?order_id={number}"


def webhook_url() -> str:
    return f"{config.WEBAPP_URL}/api/delivery/cdek/webhook"


async def ensure_webhook():
    """Подписка на смену статусов. Если уже подписаны на этот адрес —
    ничего не делаем; если на другой — перевешиваем."""
    if not enabled():
        return
    url = webhook_url()
    try:
        current = await _call("GET", "/webhooks")
    except CdekError as e:
        log.warning("Не смог прочитать подписки СДЭК: %s", e)
        return
    items = current if isinstance(current, list) else []
    for w in items:
        ent = w.get("entity") if isinstance(w.get("entity"), dict) else w
        if ent.get("type") == "ORDER_STATUS" and ent.get("url") == url:
            log.info("Подписка на статусы СДЭК уже есть: %s", url)
            return
    try:
        await _call("POST", "/webhooks", json={"type": "ORDER_STATUS", "url": url})
        log.info("Подписался на статусы СДЭК: %s", url)
    except CdekError as e:
        log.warning("Не смог подписаться на статусы СДЭК: %s", e)
