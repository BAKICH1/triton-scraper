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


def enrich_member_since(limit: int = 150, delay: float = 1.0, batch: int = 25,
                        window_h: int = 36, retry_h: int = 10, stop=None,
                        budget_s: int = 85) -> int:
    """Добирает дату регистрации аккаунта продавца со страниц объявлений.

    NULL — ещё не пробовали, '' — страница была, но маркера нет
    (до 3 попыток, пока объявление свежее retry_h часов), ISO-дата — успешно.
    Приоритет: непроверенные и самые свежие (окно window_h).
    Маршрут: напрямую, а при блокировке IP — через публичный прокси.

    Результат коммитится ПОСЛЕ КАЖДОГО объявления (не батчем) и есть бюджет
    времени budget_s — поток живёт параллельно циклу скана и останавливается
    в любой момент без потери наработанного. Если прокси мёртв — не жжём
    цикл таймаутами, выходим и попробуем в следующем.
    """
    import json as _json
    import sqlite3
    import urllib.error
    import time as _time
    from datetime import datetime, timezone, timedelta
    json = _json

    conn = sqlite3.connect(db.DB_PATH)
    t0 = _time.monotonic()
    direct_ok = True
    proxy_ok = True
    f = scraper.Fetcher(min_delay=delay, timeout=15)
    fp = scraper.Fetcher(min_delay=1.0, timeout=12)   # мёртвый прокси не ждём 25с
    done = got = got_descr = got_gal = 0
    batch_i = 0

    def stopped():
        return ((stop is not None and stop.is_set())
                or (_time.monotonic() - t0 > budget_s))

    while done < limit and not stopped() and (direct_ok or proxy_ok):
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=window_h)).isoformat(timespec="seconds")
        retry_since = (now - timedelta(hours=retry_h)).isoformat(timespec="seconds")
        # первые батчи — накрыть новейшие, дальше — дренаж бэклога от старых
        order = "DESC" if batch_i < 3 else "ASC"
        rows = conn.execute(
            f"""SELECT id,url,ms_tries FROM ads
               WHERE url!='' AND first_seen>=?
                 AND (ms_tries<3 OR descr_full IS NULL)
                 AND (member_since IS NULL OR (member_since='' AND first_seen>=?)
                      OR descr_full IS NULL)
               ORDER BY ms_tries ASC, descr_full IS NULL DESC, first_seen {order} LIMIT ?""",
            (since, retry_since, min(batch, limit - done))).fetchall()
        if not rows:
            break
        for ad_id, url, tries in rows:
            if stopped():
                break
            html = None
            if direct_ok:
                try:
                    html = f.get(url)
                except scraper.BlockedError:
                    direct_ok = False          # IP прикрыли — дальше через прокси
                except urllib.error.HTTPError as e:
                    if e.code in (404, 410):   # объявление исчезло — ретраи не нужны
                        with conn:
                            conn.execute("UPDATE ads SET member_since='', ms_tries=3 WHERE id=?",
                                         (ad_id,))
                        done += 1
                        continue
                except Exception:
                    pass
            if html is None and not direct_ok and proxy_ok:
                try:
                    import urllib.parse
                    proxied = ("https://api.allorigins.win/raw?url="
                               + urllib.parse.quote(url, safe=""))
                    html = fp.get(proxied)
                except urllib.error.HTTPError as e:
                    if e.code in (404, 410):
                        with conn:
                            conn.execute("UPDATE ads SET member_since='', ms_tries=3 WHERE id=?",
                                         (ad_id,))
                        done += 1
                        continue
                    proxy_ok = False            # прокси отвечает ошибкой — выходим
                    break
                except Exception:
                    proxy_ok = False            # таймаут/сеть — прокси мёртв, выходим
                    break
            if not html:
                continue
            ms = scraper.parse_member_since(html)
            descr = scraper.parse_descr_full(html)
            gal = scraper.parse_gallery(html)
            with conn:                          # коммит сразу — наработанное не теряем
                if ms:
                    conn.execute("UPDATE ads SET member_since=?, ms_tries=3 WHERE id=?",
                                 (ms, ad_id))
                    got += 1
                elif (tries or 0) < 3:
                    conn.execute("UPDATE ads SET member_since='', ms_tries=? WHERE id=?",
                                 ((tries or 0) + 1, ad_id))
                if descr:
                    conn.execute("UPDATE ads SET descr_full=? WHERE id=?", (descr, ad_id))
                    got_descr += 1
                if gal:
                    conn.execute("UPDATE ads SET imgs=? WHERE id=?",
                                 (json.dumps(gal, ensure_ascii=False), ad_id))
                    got_gal += 1
            done += 1
        batch_i += 1

    conn.close()
    route = ("напрямую" if direct_ok else
             ("прямо+прокси" if proxy_ok else "напрямую (прокси недоступен)"))
    if done or not direct_ok:
        print(f"самореги: попыток {done}, дат {got}, описаний {got_descr}, "
              f"галерей {got_gal}, маршрут: {route} "
              f"({_time.monotonic() - t0:.0f} c)", flush=True)
    return got


def ensure_sources():
    """Сетка источников ×10 охвата: глобальная лента + ВСЕ корневые категории.

    Идемпотентно: чистим только pages/active, не ломая ручные источники.
    Глубина: горячие ниши (Elektronik/Haus&Garten/Mode) — 3 страницы, остальные — 2.
    """
    import sqlite3
    prior = {161: 3, 80: 3, 153: 3}
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    have = {(r["type"], r["value"]): r for r in conn.execute("SELECT * FROM sources")}
    if ("global", "") not in have:
        conn.execute("INSERT INTO sources(type,value,label,pages,active) "
                     "VALUES('global','','Вся Германия (все категории)',2,1)")
    for r in conn.execute("SELECT id,slug,name FROM categories WHERE parent IS NULL").fetchall():
        key = ("category", f"{r['slug']}:{r['id']}")
        pages = prior.get(r["id"], 2)
        if key in have:
            conn.execute("UPDATE sources SET pages=?, active=1 WHERE id=?",
                         (pages, have[key]["id"]))
        else:
            conn.execute("INSERT INTO sources(type,value,label,pages,active) "
                         "VALUES(?,?,?,?,1)", ("category", key[1], r["name"], pages))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM sources WHERE active=1").fetchone()["c"]
    conn.close()
    print(f"источники: {n} активных (сетка категорий гарантирует ~10x охват)")


def main() -> int:
    slug = os.environ.get("HERENOW_SLUG", "").strip()
    claim = os.environ.get("HERENOW_CLAIM", "").strip()
    if slug and claim:
        with open(os.path.join(db.DATA_DIR, "herenow_claim.txt"), "w", encoding="utf-8") as f:
            f.write(f"site: https://{slug}.here.now\nslug: {slug}\nclaimToken: {claim}\n")
    db.init()
    ensure_sources()
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
    # обогащение саморегов — потоком на всё время цикла (скан+публикация+хвост)
    import threading
    stop_enr = threading.Event()
    def _enrich_worker():
        try:
            enrich_member_since(stop=stop_enr)
        except Exception as e:
            print("самореги: поток остановился:", type(e).__name__, e)
    enr = threading.Thread(target=_enrich_worker, daemon=True, name="enrich")
    enr.start()
    mon._scan_round()
    print("скан:", mon.last_scan_at, "| найдено/новых за раунд:", mon.new_in_last_scan,
          "| статус:", mon.status,
          ("| ошибка: " + mon.last_error) if mon.last_error else "")
    # публикуем СРАЗУ после скана — свежие объявления на сайте на минуту раньше;
    # даты саморегов догонят следующим циклом (окно фильтра — 2 дня)
    ok = publish.republish()
    # хвост: бот продолжает отвечать, пока стартует следующий раннер
    try:
        import telegram_bot
        telegram_bot.tail(tg, int(os.environ.get("TG_TAIL", "40")))
    except Exception:
        pass
    stop_enr.set(); enr.join(timeout=8)
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
