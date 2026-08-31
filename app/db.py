"""SQLite. При 8–10 заказах в день этого хватает с запасом."""
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS stickers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    file TEXT NOT NULL,          -- имя PNG в data/stickers/
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
    size TEXT NOT NULL,
    price INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    -- new → paid → in_progress → ready → done | cancelled
    view_token TEXT NOT NULL,    -- для просмотра раскладки сотрудником по ссылке
    staff_msg_id INTEGER,        -- id карточки в чате сотрудников
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
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


def init_db():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
    sync_stickers_from_csv()


def sync_stickers_from_csv():
    """Читает stickers/stickers.csv при каждом запуске.

    Новая строка → добавляем принт. Изменил число в колонке stock → ставим новое.
    Не менял → остаток не трогаем (иначе продажи «откатились» бы после обновления).
    Убрал строку из таблицы → принт прячется из каталога.
    """
    import csv
    import logging

    log = logging.getLogger("stickers")
    if not config.STICKERS_CSV.exists():
        log.warning("Нет файла %s — каталог принтов пуст", config.STICKERS_CSV)
        return
    seen = []
    with conn() as c, open(config.STICKERS_CSV, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f)):
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            if not row.get("file"):
                continue
            if not (config.STICKERS_DIR / row["file"]).exists():
                log.warning("В таблице есть %s, но картинки нет — строка пропущена", row["file"])
                continue
            try:
                name = row["name"]
                w, h = float(row["width_mm"]), float(row["height_mm"])
                stock = int(row["stock"])
            except (KeyError, ValueError):
                log.warning("Строка %s заполнена неверно — пропущена", row.get("file"))
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
    log.info("Каталог принтов обновлён: %d шт.", len(seen))


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


# ---------- Квота ----------

def orders_today() -> int:
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) n FROM orders WHERE date(created_at)=? AND status!='cancelled'",
            (date.today().isoformat(),),
        ).fetchone()
    return row["n"]


def quota_left() -> int:
    return max(0, config.ONLINE_QUOTA_PER_DAY - orders_today())


# ---------- Заказы ----------

class OrderError(Exception):
    pass


def create_order(user, size: str, items: list, price: int) -> dict:
    """items: [{side, sticker_id, x_mm, y_mm, rotation}, ...]
    Резервирует остатки стикеров атомарно."""
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        # квота
        n = c.execute(
            "SELECT COUNT(*) n FROM orders WHERE date(created_at)=? AND status!='cancelled'",
            (date.today().isoformat(),),
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
        cur = c.execute(
            "INSERT INTO orders (user_id, username, first_name, size, price, view_token) "
            "VALUES (?,?,?,?,?,?)",
            (user["id"], user.get("username"), user.get("first_name"),
             size, price, secrets.token_urlsafe(8)),
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
            for row in c.execute(
                "SELECT sticker_id, COUNT(*) n FROM order_items WHERE order_id=? GROUP BY sticker_id",
                (oid,),
            ).fetchall():
                c.execute("UPDATE stickers SET stock=stock+? WHERE id=?",
                          (row["n"], row["sticker_id"]))


def set_staff_msg(oid: int, msg_id: int):
    with conn() as c:
        c.execute("UPDATE orders SET staff_msg_id=? WHERE id=?", (msg_id, oid))
