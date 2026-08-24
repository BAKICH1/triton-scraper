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
