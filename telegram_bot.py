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

ID_RE = re.compile(r"(?:/start\s+|/ad\s+)?(?:https?://www\.kleinanzeigen\.de/s-anzeige/[^\s]*?/)?(\d{6,12})(?:[^\d].*)?$")


def _call(token, method, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data,
                                 method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except Exception as e:
        print(f"  tg:{method} → {type(e).__name__} {e}")
        return {}


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
    descr = (a.get("descr") or "").strip()
    if descr:
        lines.append("—\n<i>" + e(descr[:420].strip()) + ("…" if len(descr) > 420 else "") + "</i>")
    lines.append("\n📡 Triton Scraper")
    return "\n".join(lines)[:1024]


def _send_card(token, chat_id, ad_id):
    a = _ad_row(str(ad_id))
    if not a:
        _call(token, "sendMessage", chat_id=chat_id,
              text="Не нашёл объявление " + html.escape(str(ad_id)) +
                   " в базе. Пришли id или ссылку объявления заново —(monitored window 2 дня).")
        return
    text = _caption(a)
    kb = {"inline_keyboard": [[{"text": "Открыть на Kleinanzeigen", "url": a["url"]}]]}
    if a.get("img"):
        r = _call(token, "sendPhoto", chat_id=chat_id, photo=a["img"],
                  caption=text, parse_mode="HTML", reply_markup=kb)
        if r.get("ok"):
            return
    _call(token, "sendMessage", chat_id=chat_id, text=text,
          parse_mode="HTML", reply_markup=kb, disable_web_page_preview=False)


HELP = ("Пришли <b>id</b> или <b>ссылку</b> объявления — вышлю карточку с фото, "
        "просмотрами и всем, что знаю.\n"
        "Кнопка «TG» на карточках сайта ведёт сюда автоматически.\n\n📡 Triton Scraper · бета")


def process_updates():
    """Забрать и отработать накопившиеся команды. Вызывается каждый цикл сканера."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    # имя бота для кнопок на сайте (однократно)
    if not db.kv_get("tg_bot_name"):
        me = _call(token, "getMe")
        if me.get("ok") and me.get("result", {}).get("username"):
            db.kv_set("tg_bot_name", me["result"]["username"])
            print(f"  tg: бот @{me['result']['username']} на связи")
            db.kv_set("tg_need_rebuild", "1")
    off = int(db.kv_get("tg_offset", 0) or 0)
    r = _call(token, "getUpdates", offset=off, timeout=0, limit=30)
    if not r.get("ok"):
        return
    for u in r.get("result", []):
        off = max(off, u["update_id"] + 1)
        msg = u.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        if not chat:
            continue
        text = (msg.get("text") or "").strip()
        m = ID_RE.match(text)
        if m:
            _send_card(token, chat, m.group(1))
        else:
            _call(token, "sendMessage", chat_id=chat, text=HELP, parse_mode="HTML")
    db.kv_set("tg_offset", off)
    if r["result"]:
        print(f"  tg: обработано {len(r['result'])} команд")
