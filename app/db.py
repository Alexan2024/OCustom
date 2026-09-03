"""SQLite. При 8–10 заказах в день этого хватает с запасом."""
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

log = logging.getLogger("db")

# Время заведения (Москва по умолчанию). SQLite считает datetime('now') в UTC,
# поэтому везде добавляем смещение — иначе дневная квота сбрасывалась бы в 03:00.
NOW_SQL = f"datetime('now','+{config.TZ_OFFSET_HOURS} hours')"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS stickers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    file TEXT NOT NULL,          -- имя PNG в stickers/
    width_mm REAL NOT NULL,      -- физический размер DTF-переноса
    height_mm REAL NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    csv_stock INTEGER NOT NULL DEFAULT 0,  -- что было в таблице при прошлой загрузке
    active INTEGER NOT NULL DEFAULT 1,
    sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    first_name TEXT,
    phone TEXT,                  -- телефон покупателя (нужен для чека по 54-ФЗ)
    size TEXT NOT NULL,
    price INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    -- new → paid → in_progress → ready → done | cancelled
    payment_id TEXT,             -- id платежа в ЮKassa
    view_token TEXT NOT NULL,    -- для просмотра раскладки сотрудником по ссылке
    staff_msg_id INTEGER,        -- id карточки в чате сотрудников
    created_at TEXT NOT NULL DEFAULT ({NOW_SQL})
);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    side TEXT NOT NULL,          -- front | back
    sticker_id INTEGER NOT NULL REFERENCES stickers(id),
    x_mm REAL NOT NULL,          -- центр стикера: смещение от вертикальной оси (+ вправо)
    y_mm REAL NOT NULL,          -- центр стикера: от верха печатной зоны
    rotation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS customers (
    user_id INTEGER PRIMARY KEY,
    phone TEXT,
    updated_at TEXT NOT NULL DEFAULT ({NOW_SQL})
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def now_local() -> datetime:
    """Текущее время заведения (наивный datetime, как в базе)."""
    return (datetime.now(timezone.utc)
            + timedelta(hours=config.TZ_OFFSET_HOURS)).replace(tzinfo=None)


def stamp(dt: datetime) -> str:
    """Время в том же формате, в котором его пишет SQLite."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Переезд со старого имени базы (amang.db → omanko.db) без потери заказов.
    if config.LEGACY_DB_PATH.exists() and not config.DB_PATH.exists():
        config.LEGACY_DB_PATH.rename(config.DB_PATH)
        log.info("База переименована: %s → %s",
                 config.LEGACY_DB_PATH.name, config.DB_PATH.name)
    with conn() as c:
        c.executescript(SCHEMA)
    _migrate()
    sync_stickers_from_csv()


def _migrate():
    """Добавляет колонки, которых нет в уже развёрнутой базе."""
    with conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(orders)")}
        for name, ddl in (("phone", "TEXT"), ("payment_id", "TEXT")):
            if name not in cols:
                c.execute(f"ALTER TABLE orders ADD COLUMN {name} {ddl}")
                log.info("Миграция: добавлена колонка orders.%s", name)
        _fix_legacy_created_at(c)


def _fix_legacy_created_at(c):
    """Разовая починка времени заказов.

    В первой версии схемы created_at писался в UTC. Сравнения идут по времени
    заведения (UTC+TZ_OFFSET_HOURS), поэтому свежий заказ выглядел на три часа
    старше, чем есть, и автоотмена срабатывала сразу после создания. Уже
    существующую таблицу CREATE TABLE IF NOT EXISTS не переписывает, так что
    старый дефолт живёт в базе до тех пор, пока его не поправить руками.

    Метку о том, что починка сделана, держим в PRAGMA user_version — второй раз
    время не сдвинется.
    """
    if c.execute("PRAGMA user_version").fetchone()[0] >= 1:
        return
    row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'"
    ).fetchone()
    ddl = (row["sql"] if row else "") or ""
    if "hours" not in ddl and config.TZ_OFFSET_HOURS:
        n = c.execute(
            f"UPDATE orders SET created_at="
            f"datetime(created_at,'+{int(config.TZ_OFFSET_HOURS)} hours')"
        ).rowcount
        log.info("Миграция: время у %d заказов переведено из UTC в местное", n)
    c.execute("PRAGMA user_version = 1")


def sync_stickers_from_csv():
    """Читает stickers/stickers.csv при каждом запуске.

    Новая строка → добавляем принт. Изменил число в колонке stock → ставим новое.
    Не менял → остаток не трогаем (иначе продажи «откатились» бы после обновления).
    Убрал строку из таблицы → принт прячется из каталога.
    """
    import csv

    slog = logging.getLogger("stickers")
    if not config.STICKERS_CSV.exists():
        slog.warning("Нет файла %s — каталог принтов пуст", config.STICKERS_CSV)
        return
    seen = []
    with conn() as c, open(config.STICKERS_CSV, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f)):
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            if not row.get("file"):
                continue
            if not (config.STICKERS_DIR / row["file"]).exists():
                slog.warning("В таблице есть %s, но картинки нет — строка пропущена", row["file"])
                continue
            try:
                name = row["name"]
                w, h = float(row["width_mm"]), float(row["height_mm"])
                stock = int(row["stock"])
            except (KeyError, ValueError):
                slog.warning("Строка %s заполнена неверно — пропущена", row.get("file"))
                continue
            seen.append(row["file"])
            old = c.execute(
                "SELECT id, stock, csv_stock FROM stickers WHERE file=?", (row["file"],)
            ).fetchone()
            if old is None:
                c.execute(
                    "INSERT INTO stickers (name,file,width_mm,height_mm,stock,csv_stock,sort,active)"
                    " VALUES (?,?,?,?,?,?,?,1)",
                    (name, row["file"], w, h, stock, stock, i),
                )
            else:
                # остаток перезаписываем, только если число в таблице изменили руками
                new_stock = stock if stock != old["csv_stock"] else old["stock"]
                c.execute(
                    "UPDATE stickers SET name=?, width_mm=?, height_mm=?, stock=?,"
                    " csv_stock=?, sort=?, active=1 WHERE id=?",
                    (name, w, h, new_stock, stock, i, old["id"]),
                )
        if seen:
            q = ",".join("?" * len(seen))
            c.execute(f"UPDATE stickers SET active=0 WHERE file NOT IN ({q})", seen)
    slog.info("Каталог принтов обновлён: %d шт.", len(seen))


# ---------- Стикеры ----------

def active_stickers():
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM stickers WHERE active=1 AND stock>0 ORDER BY sort, id"
        ).fetchall()
    return [dict(r) for r in rows]


def sticker_map(ids):
    with conn() as c:
        q = ",".join("?" * len(ids))
        rows = c.execute(f"SELECT * FROM stickers WHERE id IN ({q})", list(ids)).fetchall()
    return {r["id"]: dict(r) for r in rows}


# ---------- Покупатели ----------

def set_phone(user_id: int, phone: str):
    with conn() as c:
        c.execute(
            f"INSERT INTO customers (user_id, phone, updated_at) VALUES (?,?,{NOW_SQL}) "
            f"ON CONFLICT(user_id) DO UPDATE SET phone=excluded.phone, updated_at={NOW_SQL}",
            (user_id, phone),
        )


def get_phone(user_id: int) -> str | None:
    with conn() as c:
        row = c.execute("SELECT phone FROM customers WHERE user_id=?", (user_id,)).fetchone()
    return row["phone"] if row else None


# ---------- Квота ----------

def orders_today() -> int:
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) n FROM orders "
            "WHERE date(created_at)=? AND status!='cancelled'",
            (now_local().strftime("%Y-%m-%d"),),
        ).fetchone()
    return row["n"]


def quota_left() -> int:
    return max(0, config.ONLINE_QUOTA_PER_DAY - orders_today())


# ---------- Заказы ----------

class OrderError(Exception):
    pass


def create_order(user, size: str, items: list, price: int, phone: str | None = None) -> dict:
    """items: [{side, sticker_id, x_mm, y_mm, rotation}, ...]
    Резервирует остатки стикеров атомарно."""
    today = now_local().strftime("%Y-%m-%d")
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        # квота
        n = c.execute(
            "SELECT COUNT(*) n FROM orders "
            "WHERE date(created_at)=? AND status!='cancelled'", (today,)
        ).fetchone()["n"]
        if n >= config.ONLINE_QUOTA_PER_DAY:
            raise OrderError("Квота онлайн-заказов на сегодня исчерпана. Попробуй завтра!")
        # остатки
        need: dict[int, int] = {}
        for it in items:
            need[it["sticker_id"]] = need.get(it["sticker_id"], 0) + 1
        for sid, cnt in need.items():
            row = c.execute(
                "SELECT stock, active, name FROM stickers WHERE id=?", (sid,)
            ).fetchone()
            if not row or not row["active"] or row["stock"] < cnt:
                nm = row["name"] if row else f"#{sid}"
                raise OrderError(f"Принт «{nm}» уже разобрали. Убери его или выбери другой.")
        for sid, cnt in need.items():
            c.execute("UPDATE stickers SET stock=stock-? WHERE id=?", (cnt, sid))
        # Время пишем сами, а не дефолтом таблицы: в старых базах дефолт остался
        # в UTC, и заказ рождался «на три часа старше», чем есть.
        cur = c.execute(
            "INSERT INTO orders (user_id, username, first_name, phone, size, price,"
            " view_token, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (user["id"], user.get("username"), user.get("first_name"),
             phone, size, price, secrets.token_urlsafe(8), stamp(now_local())),
        )
        oid = cur.lastrowid
        for it in items:
            c.execute(
                "INSERT INTO order_items (order_id, side, sticker_id, x_mm, y_mm, rotation) "
                "VALUES (?,?,?,?,?,?)",
                (oid, it["side"], it["sticker_id"],
                 round(it["x_mm"], 1), round(it["y_mm"], 1), it["rotation"]),
            )
    return get_order(oid)


def get_order(oid: int) -> dict | None:
    with conn() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return None
        items = c.execute(
            "SELECT oi.*, s.name, s.file, s.width_mm, s.height_mm "
            "FROM order_items oi JOIN stickers s ON s.id=oi.sticker_id "
            "WHERE oi.order_id=?", (oid,)
        ).fetchall()
    d = dict(o)
    d["items"] = [dict(i) for i in items]
    return d


def set_status(oid: int, status: str, restock: bool = False):
    with conn() as c:
        c.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
        if restock:
            _restock(c, oid)


def _restock(c, oid: int):
    for row in c.execute(
        "SELECT sticker_id, COUNT(*) n FROM order_items WHERE order_id=? GROUP BY sticker_id",
        (oid,),
    ).fetchall():
        c.execute("UPDATE stickers SET stock=stock+? WHERE id=?",
                  (row["n"], row["sticker_id"]))


def set_payment_id(oid: int, payment_id: str):
    with conn() as c:
        c.execute("UPDATE orders SET payment_id=? WHERE id=?", (payment_id, oid))


def mark_paid(oid: int, payment_id: str | None = None) -> bool:
    """Переводит заказ в paid. Возвращает False, если он уже не 'new' —
    так повторная доставка вебхука не создаёт второе уведомление клиенту."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            "UPDATE orders SET status='paid', payment_id=COALESCE(?, payment_id) "
            "WHERE id=? AND status='new'",
            (payment_id, oid),
        )
        return cur.rowcount == 1


def cancel_if_new(oid: int) -> bool:
    """Отменяет заказ и возвращает принты в остатки, если он всё ещё не оплачен."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            "UPDATE orders SET status='cancelled' WHERE id=? AND status='new'", (oid,)
        )
        if cur.rowcount != 1:
            return False
        _restock(c, oid)
        return True


def expired_unpaid(minutes: int) -> list[dict]:
    """Заказы, которые висят в 'new' дольше положенного.

    Границу считаем в Python и сравниваем с created_at, который тоже пишется
    из Python: обе стороны заведомо в одной системе отсчёта. Раньше время
    заказа приходило из дефолта таблицы (в старых базах — UTC), а граница
    считалась в местном времени, и разница в три часа отменяла заказ
    через секунду после создания.
    """
    cutoff = stamp(now_local() - timedelta(minutes=int(minutes)))
    with conn() as c:
        rows = c.execute(
            "SELECT id, user_id, price, payment_id, staff_msg_id FROM orders "
            "WHERE status='new' AND created_at < ?", (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


def set_staff_msg(oid: int, msg_id: int):
    with conn() as c:
        c.execute("UPDATE orders SET staff_msg_id=? WHERE id=?", (msg_id, oid))
