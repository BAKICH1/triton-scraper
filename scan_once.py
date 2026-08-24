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


def enrich_member_since(limit: int = 150, delay: float = 0.85, batch: int = 25,
                        window_h: int = 36, retry_h: int = 10, stop=None) -> int:
    """Добирает дату регистрации аккаунта продавца со страниц объявлений.

    NULL — ещё не пробовали, '' — страница была, но маркера нет
    (до 3 попыток, пока объявление свежее retry_h часов), ISO-дата — успешно.
    Приоритет: непроверенные и самые свежие (окно window_h).
    Маршрут: напрямую, а при блокировке IP — через публичный прокси.
    Работает батчами и может останавливаться между запросами (stop-событие) —
    поток гоняется параллельно с публикацией, лимит — предохранитель.
    """
    import sqlite3
    import urllib.error
    from datetime import datetime, timezone, timedelta

    conn = sqlite3.connect(db.DB_PATH)
    direct_ok = {"flag": True}
    f = scraper.Fetcher(min_delay=delay, timeout=15)
    fp = scraper.Fetcher(min_delay=1.0, timeout=25)   # темп для прокси-маршрута
    done = got = 0

    def stopped():
        return stop is not None and stop.is_set()

    while done < limit and not stopped():
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=window_h)).isoformat(timespec="seconds")
        retry_since = (now - timedelta(hours=retry_h)).isoformat(timespec="seconds")
        rows = conn.execute(
            """SELECT id,url,ms_tries FROM ads
               WHERE url!='' AND ms_tries<3 AND first_seen>=?
                 AND (member_since IS NULL OR (member_since='' AND first_seen>=?))
               ORDER BY ms_tries ASC, first_seen DESC LIMIT ?""",
            (since, retry_since, min(batch, limit - done))).fetchall()
        if not rows:
            break
        results = {}
        for ad_id, url, tries in rows:
            if stopped():
                break
            html = None
            if direct_ok["flag"]:
                try:
                    html = f.get(url)
                except scraper.BlockedError:
                    direct_ok["flag"] = False   # IP прикрыли — остальное через прокси
                except urllib.error.HTTPError as e:
                    if e.code in (404, 410):
                        results[ad_id] = ("", None)   # объявление исчезло — ретраи не нужны
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
                        results[ad_id] = ("", None)   # объявление исчезло
                        continue
                    continue                          # прочие ошибки прокси — ретрай позже
                except Exception:
                    continue                     # сети нет — попытку не считаем
            if not html:                          # страницы так и нет — без попытки
                continue
            # страница получена: попытка засчитывается (без даты — ретраи до 3 раз)
            results[ad_id] = (scraper.parse_member_since(html) or "", (tries or 0) + 1)

        with conn:
            for ad_id, (ms, new_tries) in results.items():
                if ms:
                    conn.execute("UPDATE ads SET member_since=?, ms_tries=3 WHERE id=?",
                                 (ms, ad_id))
                    got += 1
                else:
                    # даты нет: None = хватит, иначе попытка+1 (ретрай, пока свежее)
                    conn.execute("UPDATE ads SET member_since='', ms_tries=? WHERE id=?",
                                 (3 if new_tries is None else new_tries, ad_id))
                done += 1
        if len(results) < len(rows):   # остановились посреди батча
            break

    conn.close()
    if done:
        route = "прямо+прокси" if not direct_ok["flag"] else "напрямую"
        print(f"самореги: обогащено {got} из {done} попыток ({route})")
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
