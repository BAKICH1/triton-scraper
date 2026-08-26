# -*- coding: utf-8 -*-
"""Бета: телеграм-бот «выгрузить объявление».

Живёт внутри цикла сканера (GitHub Actions, ~2,5 мин): забирает накопившиеся
команды getUpdates и отвечает карточкой объявления — фото, просмотры,
продавец, саморег, TOP, категория, ссылка. Команда приходит с сайта:
кнопка «TG» на карточке ведёт на https://t.me/<бот>?start=<id>.
Оффсет обновлений хранится в kv базы — цепочка раннеров работает как один бот.
"""
import html
import json
import os
import re
import sqlite3
import urllib.request

import db
import publish

API = "https://api.telegram.org/bot{token}/{method}"
UA_HDR = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

ID_RE = re.compile(r"(?:/start\s+|/ad\s+)?(?:https?://www\.kleinanzeigen\.de/s-anzeige/[^\s]*?/)?(\d{6,12})(?:[^\d].*)?$")


def _call(token, method, _http_timeout=25, _quiet=False, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data,
                                 method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_http_timeout) as r:
            return json.load(r)
    except Exception as e:
        if not _quiet:
            print(f"  tg:{method} → {type(e).__name__} {e}")
        return {}


IMG_RE = re.compile(r'https://img\.kleinanzeigen\.de/api/v1/prod-ads/images/[0-9a-f]{2}/([0-9a-f-]{36})')


def _fetch(url):
    """Страница объявления: напрямую, при неудаче — через публичный прокси."""
    import urllib.error, urllib.parse
    for target in (url, "https://api.allorigins.win/raw?url=" + urllib.parse.quote(url, safe="")):
        try:
            req = urllib.request.Request(target, headers={"User-Agent": UA_HDR})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            continue
    return ""


def _gallery(page_html, fallback_img):
    """Все фото объявления в порядке галереи, правило $_59 — максимум качества."""
    seen, out = set(), []
    for uuid in IMG_RE.findall(page_html):
        if uuid not in seen:
            seen.add(uuid)
            out.append(f"https://img.kleinanzeigen.de/api/v1/prod-ads/images/{uuid[:2]}/{uuid}?rule=$_59.JPG")
    if not out and fallback_img:
        out = [fallback_img]
    return out[:10]


def _ad_row(ad_id):
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    conn.close()
    return dict(r) if r else None


def _cat_path(cat_id):
    if not cat_id:
        return ""
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    parts, cur = [], cat_id
    for _ in range(3):
        r = conn.execute("SELECT name,parent FROM categories WHERE id=?", (cur,)).fetchone()
        if not r:
            break
        parts.append(r["name"] or "")
        cur = r["parent"]
        if cur is None:
            break
    conn.close()
    return " › ".join(p for p in reversed(parts) if p)


def _fmt_date(a):
    t = a.get("posted_at") or ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2})", t)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)} {m.group(4)}"
    return a.get("date_text") or ""


def _caption(a):
    """Карточка объявления для Telegram (HTML, ≤1024 символов)."""
    e = html.escape
    lines = []
    if a.get("is_top"):
        lines.append("⚠️ <b>TOP-объявление</b>")
    lines.append(f"<b>{e(a['title'])}</b>")
    price = a.get("price_text") or ""
    if a.get("price_eur") == 0:
        price = "Бесплатно (Verschenken)"
    if price:
        price = f"💶 {e(price)}" + (" · торг" if a.get("negotiable") else "")
        lines.append(price)
    loc = a.get("location") or ""
    dt = _fmt_date(a)
    if loc or dt:
        lines.append(("📍 " + e(loc) if loc else "") + (" · " if loc and dt else "") + (e(dt) if dt else ""))
    views = a.get("views")
    if views is not None:
        vr = publish._view_rate(a)
        extra = f" · +{vr:g}/мин 🔥" if vr else ""
        lines.append(f"👁 {views} просмотров{extra}")
    who = "🏪 Магазин (PRO)" if a.get("is_pro") else "👤 Частное лицо"
    ms = a.get("member_since")
    if ms:
        try:
            from datetime import date
            d = date.fromisoformat(ms)
            age = (date.today() - d).days
            tag = " · <b>САМОРЕГ</b> ⚠️" if age < 2 else ""
            who += f" · акк с {d.strftime('%d.%m.%Y')}{tag}"
        except Exception:
            pass
    lines.append(who)
    cp = _cat_path(a.get("category_id"))
    if cp:
        lines.append("🏷 " + e(cp))
    if a.get("img_count"):
        lines.append(f"📸 {a['img_count']} фото")
    descr = (a.get("descr_full") or a.get("descr") or "").strip()
    if descr:
        lines.append("—\n<i>" + e(descr[:420].strip()) + ("…" if len(descr) > 420 else "") + "</i>")
    lines.append("\n📡 Triton Scraper")
    return "\n".join(lines)[:1024]


def _stored_gallery(a):
    """Галерея из базы (обогащение 3-в-1): мгновенно, без похода на живой сайт."""
    try:
        raw = json.loads(a.get("imgs") or "[]")
    except Exception:
        return []
    out = []
    for u in raw:
        u = (u or "").strip()
        if not u:
            continue
        if re.fullmatch(r"[0-9a-f-]{36}", u):   # uuid → полный URL
            u = f"https://img.kleinanzeigen.de/api/v1/prod-ads/images/{u[:2]}/{u}?rule=$_59.JPG"
        out.append(u)
    return out[:10]


def _send_card(token, chat_id, ad_id):
    a = _ad_row(str(ad_id))
    if not a:
        _call(token, "sendMessage", chat_id=chat_id,
              text="Не нашёл объявление " + html.escape(str(ad_id)) +
                   " в базе (окно мониторинга — 2 дня). Пришли id или ссылку заново.")
        return
    text = _caption(a)
    kb = {"inline_keyboard": [[{"text": "Открыть на Kleinanzeigen", "url": a["url"]}]]}
    imgs = _stored_gallery(a)
    if not imgs:
        imgs = _gallery(_fetch(a["url"]), a.get("img"))   # запасной путь: живая страница
    if len(imgs) > 1:
        media = [{"type": "photo", "media": imgs[0], "caption": text, "parse_mode": "HTML"}]
        media += [{"type": "photo", "media": u} for u in imgs[1:]]
        r = _call(token, "sendMediaGroup", chat_id=chat_id, media=media)
        if r.get("ok"):
            _call(token, "sendMessage", chat_id=chat_id,
                  text="⬆️ " + str(len(imgs)) + " фото · объявление целиком 👇",
                  reply_markup=kb)
            return
    if imgs:
        r = _call(token, "sendPhoto", chat_id=chat_id, photo=imgs[0],
                  caption=text, parse_mode="HTML", reply_markup=kb)
        if r.get("ok"):
            return
    _call(token, "sendMessage", chat_id=chat_id, text=text,
          parse_mode="HTML", reply_markup=kb, disable_web_page_preview=False)


HELP = ("Пришли <b>id</b> или <b>ссылку</b> объявления — вышлю карточку с фото, "
        "просмотрами и всем, что знаю.\n"
        "Кнопка «TG» на карточках сайта ведёт сюда автоматически.\n\n📡 Triton Scraper · бета")


def _handle(token, u):
    msg = u.get("message") or {}
    chat = msg.get("chat", {}).get("id")
    if not chat:
        return
    text = (msg.get("text") or "").strip()
    m = ID_RE.match(text)
    if m:
        _send_card(token, chat, m.group(1))
    else:
        _call(token, "sendMessage", chat_id=chat, text=HELP, parse_mode="HTML")


def process_updates():
    """Разовая обработка накопившегося (для локального запуска/тестов)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    _register(token)
    off = int(db.kv_get("tg_offset", 0) or 0)
    r = _call(token, "getUpdates", offset=off, timeout=0, limit=30)
    if not r.get("ok"):
        return
    for u in r.get("result", []):
        off = max(off, u["update_id"] + 1)
        _handle(token, u)
        db.kv_set("tg_offset", off)


def _register(token):
    """Имя бота для кнопок сайта (однократно)."""
    if not db.kv_get("tg_bot_name"):
        me = _call(token, "getMe")
        if me.get("ok") and me.get("result", {}).get("username"):
            db.kv_set("tg_bot_name", me["result"]["username"])
            print(f"  tg: бот @{me['result']['username']} на связи")


def start_polling():
    """Живой long-poll поток на всё время работы раннера: ответ за секунды.

    Telegram держит один getUpdates-консьюмер, поэтому бот опрашивает
    только этим потоком; оффсет сохраняется после каждой команды.
    """
    import threading, time
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    _register(token)
    st = {"stop": False, "n": 0}

    def loop():
        off = int(db.kv_get("tg_offset", 0) or 0)
        while not st["stop"]:
            r = _call(token, "getUpdates", offset=off, timeout=25, limit=10,
                      _http_timeout=40, _quiet=True)
            if not r.get("ok"):
                time.sleep(3)
                continue
            for u in r.get("result", []):
                off = max(off, u["update_id"] + 1)
                try:
                    _handle(token, u)
                    st["n"] += 1
                except Exception as e:
                    print("  tg: ошибка обработки:", e)
                db.kv_set("tg_offset", off)

    t = threading.Thread(target=loop, daemon=True, name="tg-poll")
    t.start()
    return st


def tail(st, seconds=40):
    """Держим процесс живым — бот продолжает отвечать, пока раннер не сменится."""
    import time
    if not st:
        return
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds and not st["stop"]:
        time.sleep(1)
    if st["n"]:
        print(f"  tg: за цикл отвечено {st['n']} раз")
