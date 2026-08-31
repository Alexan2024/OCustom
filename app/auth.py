"""Проверка initData из Telegram Mini App (HMAC-SHA256, стандартная схема)."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from . import config

MAX_AGE_SEC = 3600 * 6


def validate_init_data(init_data: str) -> dict | None:
    """Возвращает dict пользователя или None, если подпись невалидна."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > MAX_AGE_SEC:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None
