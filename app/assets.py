"""Версия статики мини-аппа.

Telegram кэширует страницу мини-аппа агрессивно и без спроса. Пока номер
версии проставлялся руками (`app.js?v=8` в index.html), обновление доезжало
не до всех: сам index.html отдавался без заголовков кэша, WebView держал его
у себя часами — и новый номер версии лежал внутри файла, который к человеку
не приходил.

Поэтому номер версии здесь считается от содержимого файлов, а не пишется
руками, и подставляется в HTML на лету. Забыть его нельзя: поменялся байт
в app.js или styles.css — поменялся адрес, по которому браузер их просит.

Тот же номер уходит в адрес мини-аппа (`?b=…`, см. `webapp_url` в bot.py):
после деплоя адрес другой, значит и страницу Telegram запросит заново,
а не достанет из своего кэша.
"""
import hashlib
import logging
from pathlib import Path

from . import config

log = logging.getLogger("assets")

WEBAPP_DIR: Path = config.BASE_DIR / "webapp"
INDEX_FILE: Path = WEBAPP_DIR / "index.html"

# Файлы, изменение которых обязано доехать до всех сразу.
WATCHED = ("index.html", "app.js", "styles.css")

# Ссылки, которым проставляется версия. Картинки бланка сюда не входят:
# они меняются раз в год, им хватает обычного max-age.
VERSIONED = ("app.js", "styles.css")

# Кэш разбора: пересчитываем, только когда файлы на диске изменились.
# На Railway это происходит ровно один раз за деплой, локально — на каждую
# правку, без перезапуска сервера.
_state: dict = {"key": None, "build": "dev", "html": ""}


def _disk_key() -> tuple:
    """Отпечаток файлов на диске: имя, время правки, размер."""
    out = []
    for name in WATCHED:
        try:
            st = (WEBAPP_DIR / name).stat()
            out.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((name, 0, 0))
    return tuple(out)


def _hash() -> str:
    h = hashlib.sha256()
    for name in WATCHED:
        try:
            h.update((WEBAPP_DIR / name).read_bytes())
        except OSError as e:
            # Файла нет — это поломка сборки, но ронять сервер из-за неё
            # нельзя: мини-апп покажет ошибку загрузки сам.
            log.warning("Не читается %s: %s", name, e)
            h.update(name.encode())
    return h.hexdigest()[:10]


def _render(html: str, build: str) -> str:
    """Проставить версию в ссылках на app.js и styles.css.

    Трогаем только относительные ссылки, то есть те, что начинаются сразу
    после кавычки. Иначе под замену попадёт и telegram-web-app.js с CDN
    Telegram — он тоже заканчивается на «app.js».
    """
    for name in VERSIONED:
        out, i = [], 0
        while True:
            j = html.find(name, i)
            if j < 0:
                out.append(html[i:])
                break
            if j == 0 or html[j - 1] not in "\"'":
                out.append(html[i:j + len(name)])
                i = j + len(name)
                continue
            out.append(html[i:j])
            k = j + len(name)
            # Съедаем старый query, если он был: до кавычки или пробела.
            if k < len(html) and html[k] == "?":
                while k < len(html) and html[k] not in "\"'> ":
                    k += 1
            out.append(f"{name}?v={build}")
            i = k
        html = "".join(out)
    return html


def _refresh() -> None:
    key = _disk_key()
    if key == _state["key"]:
        return
    build = _hash()
    try:
        raw = INDEX_FILE.read_text(encoding="utf-8")
    except OSError as e:
        log.error("Не читается index.html: %s", e)
        raw = "<!doctype html><meta charset=utf-8><p>Мини-апп не собран."
    _state.update(key=key, build=build, html=_render(raw, build))
    log.info("Статика мини-аппа: сборка %s", build)


def build() -> str:
    """Короткий хэш текущей сборки мини-аппа."""
    _refresh()
    return _state["build"]


def index_html() -> str:
    """index.html с проставленными версиями app.js и styles.css."""
    _refresh()
    return _state["html"]
