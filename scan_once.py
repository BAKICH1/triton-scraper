# -*- coding: utf-8 -*-
"""Один цикл мониторинга: скан всех источников → замеры просмотров → публикация на here.now.

Для запуска по расписанию без живого сервера (GitHub Actions, cron, любой CI).
Токен here.now берётся из переменных окружения HERENOW_SLUG / HERENOW_CLAIM,
если они заданы — иначе используется data/herenow_claim.txt как обычно.
"""
import os
import sys
import traceback

import db
import monitor
import publish
import scraper


def _fetch_via_proxy(url: str) -> str:
    """Страница объявления через публичный прокси (другой IP — обход лимитов CI)."""
    import urllib.parse
    u = "https://api.allorigins.win/raw?url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(u, headers={"User-Agent": scraper.DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def enrich_member_since(limit: int = 28, delay: float = 1.5, window_h: int = 36) -> int:
    """Добирает дату регистрации аккаунта продавца со страниц объявлений.

    NULL — ещё не пробовали, '' — пробовали, маркера нет (не ретраим),
    ISO-дата — успешно. Приоритет: самые свежие объявления (окно window_h).
    Маршрут: напрямую, а при блокировке IP — через публичный прокси.
    """
    import sqlite3
    import urllib.error
    from datetime import datetime, timezone, timedelta

    conn = sqlite3.connect(db.DB_PATH)
    since = (datetime.now(timezone.utc) - timedelta(hours=window_h)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id,url FROM ads WHERE member_since IS NULL AND url!='' AND first_seen>=? "
        "ORDER BY first_seen DESC LIMIT ?", (since, limit)).fetchall()
    if not rows:
        conn.close()
        return 0

    direct_ok = {"flag": True}
    f = scraper.Fetcher(min_delay=delay, timeout=15)
    fp = scraper.Fetcher(min_delay=1.0, timeout=25)   # темп для прокси-маршрута
    results = {}

    for ad_id, url in rows:
        html = None
        if direct_ok["flag"]:
            try:
                html = f.get(url)
            except scraper.BlockedError:
                direct_ok["flag"] = False   # IP прикрыли — остальное через прокси
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    results[ad_id] = ""
                    continue
            except Exception:
                pass
        if html is None:
            try:
                import urllib.parse
                proxied = ("https://api.allorigins.win/raw?url="
                           + urllib.parse.quote(url, safe=""))
                html = fp.get(proxied)
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    results[ad_id] = ""
                    continue
            except Exception:
                continue                     # ретрай в следующий цикл
        results[ad_id] = scraper.parse_member_since(html) or ""

    got = 0
    with conn:
        for ad_id, ms in results.items():
            conn.execute("UPDATE ads SET member_since=? WHERE id=?", (ms, ad_id))
            if ms:
                got += 1
    conn.close()
    route = "прямо+прокси" if not direct_ok["flag"] else "напрямую"
    print(f"самореги: обогащено {got} из {len(rows)} очереди ({route})")
    return got


def main() -> int:
    slug = os.environ.get("HERENOW_SLUG", "").strip()
    claim = os.environ.get("HERENOW_CLAIM", "").strip()
    if slug and claim:
        with open(os.path.join(db.DATA_DIR, "herenow_claim.txt"), "w", encoding="utf-8") as f:
            f.write(f"site: https://{slug}.here.now\nslug: {slug}\nclaimToken: {claim}\n")
    db.init()
    # свежая база (CI-раннер): заполняем дерево категорий — без него пустые
    # селекторы, мёртвая доска и очередь просмотров доски
    if not db.get_categories():
        fb = scraper.load_fallback_categories(
            os.path.join(db.DATA_DIR, "categories_fallback.json"))
        if fb:
            db.save_categories(fb)
    mon = monitor.Monitor()          # поток не запускаем — нужен только один раунд
    # бета: телеграм-бот опрашивает команды всё время, пока жив раннер
    tg = None
    try:
        import telegram_bot
        tg = telegram_bot.start_polling()
    except Exception as e:
        print("tg: не запустился (не критично):", e)
    mon._scan_round()
    print("скан:", mon.last_scan_at, "| найдено/новых за раунд:", mon.new_in_last_scan,
          "| статус:", mon.status,
          ("| ошибка: " + mon.last_error) if mon.last_error else "")
    # публикуем СРАЗУ после скана — свежие объявления на сайте на минуту раньше;
    # даты саморегов догонят следующим циклом (окно фильтра — 2 дня)
    ok = publish.republish()
    enrich_member_since()
    # хвост: бот продолжает отвечать, пока стартует следующий раннер
    try:
        import telegram_bot
        telegram_bot.tail(tg, int(os.environ.get("TG_TAIL", "40")))
    except Exception:
        pass
    # сжать WAL в основной файл — чтобы кэш/копия базы были полными
    try:
        import sqlite3
        c = sqlite3.connect(db.DB_PATH)
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
