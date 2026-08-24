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


def enrich_member_since(limit: int = 45, delay: float = 1.6, window_h: int = 36,
                        workers: int = 3) -> int:
    """Добирает дату регистрации аккаунта продавца со страниц объявлений.

    NULL — ещё не пробовали, '' — пробовали, маркера нет (не ретраим),
    ISO-дата — успешно. Приоритет: самые свежие объявления (окно window_h),
    чтобы фильтр «самореги» работал для всего нового потока.
    Потоки с личным темпом запросов; при блокировке 403/429 — мягкий стоп.
    """
    import sqlite3
    import urllib.error
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone, timedelta

    conn = sqlite3.connect(db.DB_PATH)
    since = (datetime.now(timezone.utc) - timedelta(hours=window_h)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id,url FROM ads WHERE member_since IS NULL AND url!='' AND first_seen>=? "
        "ORDER BY first_seen DESC LIMIT ?", (since, limit)).fetchall()
    if not rows:
        conn.close()
        return 0

    stop = {"flag": False}
    results = {}

    def work(slice_):
        f = scraper.Fetcher(min_delay=delay, timeout=15)
        for ad_id, url in slice_:
            if stop["flag"]:
                return
            try:
                html = f.get(url)
            except scraper.BlockedError:      # 403/429 — притормозим до следующего цикла
                stop["flag"] = True
                return
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):      # объявление исчезло — больше не трогаем
                    results[ad_id] = ""
                continue                      # прочие коды — ретрай в следующий цикл
            except Exception:
                continue
            results[ad_id] = scraper.parse_member_since(html) or ""

    slices = [rows[i::workers] for i in range(workers)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, slices))

    got = 0
    with conn:
        for ad_id, ms in results.items():
            conn.execute("UPDATE ads SET member_since=? WHERE id=?", (ms, ad_id))
            if ms:
                got += 1
    conn.close()
    print(f"самореги: обогащено {got} из {len(rows)} очереди")
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
    mon._scan_round()
    print("скан:", mon.last_scan_at, "| найдено/новых за раунд:", mon.new_in_last_scan,
          "| статус:", mon.status,
          ("| ошибка: " + mon.last_error) if mon.last_error else "")
    enrich_member_since()
    ok = publish.republish()
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
