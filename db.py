# -*- coding: utf-8 -*-
"""Хранилище: SQLite (stdlib). Потокобезопасно через lock + WAL."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "ads.db")

_lock = threading.RLock()
_conn = None


def _iso(dt=None):
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def init():
    global _conn
    os.makedirs(DATA_DIR, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.executescript("""
    CREATE TABLE IF NOT EXISTS ads(
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        descr TEXT DEFAULT '',
        price_text TEXT DEFAULT '',
        price_eur REAL,
        negotiable INTEGER DEFAULT 0,
        location TEXT DEFAULT '',
        date_text TEXT DEFAULT '',
        posted_at TEXT,
        category_id INTEGER,
        is_pro INTEGER DEFAULT 0,
        img_count INTEGER DEFAULT 0,
        url TEXT DEFAULT '',
        img TEXT,
        first_seen TEXT NOT NULL,
        source TEXT DEFAULT 'global'
    );
    CREATE INDEX IF NOT EXISTS idx_ads_first_seen ON ads(first_seen DESC);
    CREATE INDEX IF NOT EXISTS idx_ads_posted ON ads(posted_at DESC);
    CREATE INDEX IF NOT EXISTS idx_ads_price ON ads(price_eur);
    CREATE TABLE IF NOT EXISTS sources(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,           -- global | category | keyword
        value TEXT DEFAULT '',        -- slug:catid для категории, слово для keyword
        label TEXT DEFAULT '',
        pages INTEGER DEFAULT 1,
        active INTEGER DEFAULT 1,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS scans(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT, finished_at TEXT,
        found INTEGER DEFAULT 0, new_ads INTEGER DEFAULT 0, status TEXT DEFAULT 'ok', error TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, slug TEXT, name TEXT, parent INTEGER);
    CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS views_hist(
        ad_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        views INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_vh_ad ON views_hist(ad_id, ts);
    CREATE TABLE IF NOT EXISTS staged_ads(
        id TEXT PRIMARY KEY,
        payload TEXT NOT NULL,          -- JSON объявления целиком
        posted_at TEXT,                 -- ждём, пока станет 20+ минут
        staged_at TEXT
    );
    """)
    _conn.commit()
    # миграции (идемпотентно)
    for ddl in ("ALTER TABLE ads ADD COLUMN views INTEGER",
                "ALTER TABLE ads ADD COLUMN views_at TEXT",
                "ALTER TABLE ads ADD COLUMN views_prev INTEGER",
                "ALTER TABLE ads ADD COLUMN views_prev_at TEXT",
                "ALTER TABLE ads ADD COLUMN is_top INTEGER DEFAULT 0",
                "ALTER TABLE ads ADD COLUMN member_since TEXT",
                "ALTER TABLE ads ADD COLUMN descr_full TEXT",
                "ALTER TABLE ads ADD COLUMN imgs TEXT",
                "ALTER TABLE ads ADD COLUMN ms_tries INTEGER DEFAULT 0"):
        try:
            _conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    _conn.commit()
    with _lock:
        if not _conn.execute("SELECT 1 FROM sources WHERE type='global'").fetchone():
            _conn.execute("INSERT INTO sources(type,value,label,pages,active,created_at) VALUES('global','','Вся Германия (все категории)',2,1,?)", (_iso(),))
            _conn.commit()
    return _conn


# ------------------------------- Стейджинг молодых -------------------------------
def stage_ads(ads_list):
    """Отложить объявления моложе 20 минут — введём их в базу, когда созреют."""
    import json as _json
    now = _iso()
    with _lock:
        for a in ads_list:
            try:
                _conn.execute("INSERT OR IGNORE INTO staged_ads(id,payload,posted_at,staged_at) VALUES(?,?,?,?)",
                              (a["id"], _json.dumps(a, ensure_ascii=False), a.get("posted_at"), now))
            except Exception:
                pass
        # не копим балласт старше 2 суток
        _conn.execute("DELETE FROM staged_ads WHERE staged_at < datetime('now','-2 days')")
        _conn.commit()


def promote_due(min_age_min=20.0):
    """Ввести в основную базу отложенные объявления, достигшие min_age_min."""
    import json as _json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    with _lock:
        rows = _conn.execute("SELECT id,payload,posted_at FROM staged_ads").fetchall()
        due, ids = [], []
        for r in rows:
            try:
                age = (now - datetime.fromisoformat(r["posted_at"])).total_seconds() / 60
            except Exception:
                age = None
            if age is None or age >= min_age_min:
                due.append(_json.loads(r["payload"]))
                ids.append(r["id"])
    if not due:
        return 0
    n, _ = upsert_ads(due, source="staged")
    with _lock:
        _conn.executemany("DELETE FROM staged_ads WHERE id=?", [(i,) for i in ids])
        _conn.commit()
    return n


# ------------------------------- Объявления -------------------------------

def upsert_ads(ads, source="global"):
    """Вставляет новые, обновляет существующие. Возвращает (кол-во новых, список новых id)."""
    new_ids = []
    now = _iso()
    with _lock:
        for a in ads:
            try:
                _conn.execute("""INSERT INTO ads(id,title,descr,price_text,price_eur,negotiable,location,
                    date_text,posted_at,category_id,is_pro,img_count,url,img,first_seen,source,is_top)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (a["id"], a["title"], a["descr"], a["price_text"], a["price_eur"], a["negotiable"],
                     a["location"], a["date_text"], a["posted_at"], a["category_id"], a["is_pro"],
                     a["img_count"], a["url"], a["img"], now, source, a.get("is_top", 0)))
                new_ids.append(a["id"])
            except sqlite3.IntegrityError:
                # уже видели: обновляем только цену/заголовок (могли измениться)
                _conn.execute("UPDATE ads SET title=?, price_text=?, price_eur=?, img=?, is_top=? WHERE id=?",
                              (a["title"], a["price_text"], a["price_eur"], a["img"],
                               a.get("is_top", 0), a["id"]))
        _conn.commit()
    return len(new_ids), new_ids


def get_ads(q="", category_id=None, price_min=None, price_max=None,
            hide_pro=False, only_pro=False, only_free=False, watch_keywords=None,
            limit=200, offset=0, order="first_seen"):
    sql = "SELECT a.* FROM ads a WHERE 1=1"
    args = []
    if q:
        sql += " AND (a.title LIKE ? OR a.descr LIKE ? OR a.location LIKE ?)"
        like = f"%{q}%"
        args += [like, like, like]
    if category_id:
        if category_id in TOP_PARENT_IDS():
            kids = [category_id] + [c["id"] for c in get_categories() if c["parent"] == category_id]
            sql += f" AND a.category_id IN ({','.join('?'*len(kids))})"
            args += kids
        else:
            sql += " AND a.category_id = ?"
            args.append(category_id)
    if price_min is not None:
        sql += " AND a.price_eur >= ?"
        args.append(price_min)
    if price_max is not None:
        sql += " AND a.price_eur <= ?"
        args.append(price_max)
    if only_free:
        sql += " AND a.price_eur = 0"
    if hide_pro:
        sql += " AND a.is_pro = 0"
    if only_pro:
        sql += " AND a.is_pro = 1"
    if watch_keywords:
        likes = " OR ".join(["a.title LIKE ? ESCAPE '\\'"] * len(watch_keywords))
        sql += f" AND ({likes})"
        args += [f"%{k.replace('%', r'\%').replace('_', r'\_')}%" for k in watch_keywords]
    col = "a.first_seen" if order == "first_seen" else "a.posted_at"
    sql += f" ORDER BY {col} DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    with _lock:
        rows = _conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


_TOP_CACHE = []

def TOP_PARENT_IDS():
    if not _TOP_CACHE:
        _TOP_CACHE[:] = [c["id"] for c in get_categories() if c["parent"] is None]
    return set(_TOP_CACHE)


# ------------------------------- Просмотры -------------------------------

def update_views(ad_id, views):
    """Обновляет счётчик, сохраняя предыдущий замер — по паре считаем скорость роста.

    Каждое изменение пишется в views_hist — из неё считаем прирост
    за 20 минут / 1 / 2 / 4 / 8 часов."""
    now = _iso()
    with _lock:
        row = _conn.execute("SELECT views, views_at FROM ads WHERE id=?", (ad_id,)).fetchone()
        prev, prev_at = (row["views"], row["views_at"]) if row else (None, None)
        _conn.execute(
            "UPDATE ads SET views_prev=?, views_prev_at=?, views=?, views_at=? WHERE id=?",
            (prev, prev_at, views, now, ad_id))
        if views != prev:   # точка истории — только когда счётчик реально двинулся
            _conn.execute("INSERT INTO views_hist(ad_id,ts,views) VALUES(?,?,?)", (ad_id, now, views))
        _conn.commit()


def prune_views_hist(hours=24):
    """История нужна для окон прироста до 8 часов — сутки с запасом."""
    with _lock:
        _conn.execute("DELETE FROM views_hist WHERE ts < datetime('now', ?)", (f"-{hours} hours",))
        _conn.commit()


def views_plan(board_cats=(161, 80, 153, 192), hot_minutes=22, board_n=48,
               first_n=14, retop_n=12, reterm_n=10, refresh_after_hours=4, published_limit=800,
               exclude_titles=("iphone", "айфон", "Айфон", "АЙФОН")):
    """План замеров просмотров — бюджет запросов ограничен, поэтому приоритеты:
      1) категории доски в публикуемом окне (чтобы в столбцах всегда были глазки);
      2) «горячие» (свежее hot_minutes мин) — первый замер; исключённые заголовки
         (iPhone и пр.) не измеряем вообще;
      3) «горячие» с замером — повторный замер топа по скорости и самых несвежих;
      4) возраст 15–45 мин — освежаем замер (фильтр «Горячие» считает просмотры
         у объявлений старше 20 минут, цифра должна быть свежей)."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    cutoff = _iso(now - timedelta(hours=refresh_after_hours))
    hot_cut = _iso(now - timedelta(minutes=hot_minutes))
    # первый замер: окно 45 мин, не hot_minutes — объявления входят в базу через
    # стейджинг (~20 мин), при 22-мин окне успевали бы лишь 2 минуты
    first_cut = _iso(now - timedelta(minutes=45))
    recent = _iso(now - timedelta(seconds=100))
    stale_measure = _iso(now - timedelta(minutes=15))
    excl = " AND ".join(["title NOT LIKE ?"] * len(exclude_titles))
    excl_args = ["%" + t + "%" for t in exclude_titles]
    cats_ph = ",".join("?" * len(board_cats))
    out, seen = [], set()

    def _add(rows):
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(r["id"])

    with _lock:
        # доска: поровну на каждую категорию столбца (с дочерними), иначе быстрый
        # приток Elektronik съедает весь приоритет, а Haus&Garten/Mode не меряются
        per_cat = max(2, board_n // max(1, len(board_cats)))
        for cid in board_cats:
            _add(_conn.execute(
                """SELECT id FROM ads
                   WHERE id IN (SELECT id FROM ads ORDER BY first_seen DESC LIMIT ?)
                     AND category_id IN (SELECT id FROM categories WHERE id=? OR parent=?)
                     AND (views IS NULL OR views_at IS NULL OR views_at < ?)
                   ORDER BY (views IS NULL) DESC, first_seen DESC LIMIT ?""",
                (published_limit, cid, cid, cutoff, per_cat)).fetchall())
        _add(_conn.execute(
            """SELECT id FROM ads
               WHERE first_seen >= ? AND views IS NULL AND %s
               ORDER BY first_seen DESC LIMIT ?""" % excl,
            (first_cut, *excl_args, first_n)).fetchall())
        _add(_conn.execute(
            """SELECT id FROM ads
               WHERE first_seen >= ? AND views IS NOT NULL AND views_at < ? AND %s
               ORDER BY CAST(views AS REAL) / MAX(2.0, (julianday('now') - julianday(COALESCE(NULLIF(posted_at,''), first_seen))) * 1440.0) DESC
               LIMIT ?""" % excl,
            (hot_cut, recent, *excl_args, (retop_n + 1) // 2)).fetchall())
        _add(_conn.execute(
            """SELECT id FROM ads
               WHERE first_seen >= ? AND views IS NOT NULL AND views_at < ? AND %s
               ORDER BY views_at ASC
               LIMIT ?""" % excl,
            (hot_cut, recent, *excl_args, retop_n // 2)).fetchall())
        # 4) объявления 15–45 мин от публикации: освежаем просмотры —
        #    фильтр «Горячие» (20+ мин, от 10 просмотров) считает по свежей цифре
        _add(_conn.execute(
            """SELECT id FROM ads
               WHERE views IS NOT NULL AND views_at < ? AND %s
                 AND julianday(COALESCE(NULLIF(posted_at,''), first_seen))
                     BETWEEN julianday('now','-45 minutes') AND julianday('now','-15 minutes')
               ORDER BY views_at ASC LIMIT ?""" % excl,
            (stale_measure, *excl_args, reterm_n)).fetchall())
    return out


# ------------------------------- Источники -------------------------------

def list_sources(only_active=False):
    sql = "SELECT * FROM sources" + (" WHERE active=1" if only_active else "") + " ORDER BY id"
    with _lock:
        return [dict(r) for r in _conn.execute(sql).fetchall()]


def add_source(stype, value, label, pages=1):
    with _lock:
        cur = _conn.execute("INSERT INTO sources(type,value,label,pages,active,created_at) VALUES(?,?,?,?,1,?)",
                            (stype, value, label, int(pages), _iso()))
        _conn.commit()
        return cur.lastrowid


def delete_source(sid):
    with _lock:
        _conn.execute("DELETE FROM sources WHERE id=? AND type!='global'", (sid,))
        _conn.commit()


# ------------------------------- Сканирования -------------------------------

def log_scan(status, found, new_ads, error="", finished_at=None, started_at=None):
    with _lock:
        cur = _conn.execute("INSERT INTO scans(started_at,finished_at,found,new_ads,status,error) VALUES(?,?,?,?,?,?)",
                            (started_at or _iso(), finished_at or _iso(), found, new_ads, status, error))
        _conn.commit()
        return cur.lastrowid


def last_scans(n=12):
    with _lock:
        return [dict(r) for r in _conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (n,)).fetchall()]


# ------------------------------- Категории -------------------------------

def save_categories(cats):
    with _lock:
        _conn.execute("DELETE FROM categories")
        _conn.executemany("INSERT OR REPLACE INTO categories(id,slug,name,parent) VALUES(?,?,?,?)",
                          [(c["id"], c["slug"], c["name"], c["parent"]) for c in cats])
        _conn.commit()
    _TOP_CACHE.clear()


def get_categories():
    with _lock:
        return [dict(r) for r in _conn.execute("SELECT * FROM categories ORDER BY parent NULLS FIRST, id").fetchall()]


# ------------------------------- KV / уборка -------------------------------

def kv_set(key, value):
    with _lock:
        _conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value, ensure_ascii=False)))
        _conn.commit()


def kv_get(key, default=None):
    with _lock:
        row = _conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def stats():
    with _lock:
        total = _conn.execute("SELECT COUNT(*) c FROM ads").fetchone()["c"]
        day_ago = (_iso(datetime.now(timezone.utc) - timedelta(hours=24)))
        last24 = _conn.execute("SELECT COUNT(*) c FROM ads WHERE first_seen >= ?", (day_ago,)).fetchone()["c"]
        by_cat = _conn.execute("""SELECT COALESCE(c.name,'—/Прочее') name, COUNT(*) n FROM ads a
            LEFT JOIN categories c ON c.id=a.category_id GROUP BY a.category_id ORDER BY n DESC LIMIT 24""").fetchall()
    return {"total": total, "last24": last24, "by_category": [dict(r) for r in by_cat]}


def cleanup_ads(days=21):
    cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=days))
    with _lock:
        _conn.execute("DELETE FROM ads WHERE first_seen < ?", (cutoff,))
        _conn.commit()
