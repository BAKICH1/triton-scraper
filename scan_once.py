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


def main() -> int:
    slug = os.environ.get("HERENOW_SLUG", "").strip()
    claim = os.environ.get("HERENOW_CLAIM", "").strip()
    if slug and claim:
        with open(os.path.join(db.DATA_DIR, "herenow_claim.txt"), "w", encoding="utf-8") as f:
            f.write(f"site: https://{slug}.here.now\nslug: {slug}\nclaimToken: {claim}\n")
    db.init()
    mon = monitor.Monitor()          # поток не запускаем — нужен только один раунд
    mon._scan_round()
    print("скан:", mon.last_scan_at, "| найдено/новых за раунд:", mon.new_in_last_scan,
          "| статус:", mon.status,
          ("| ошибка: " + mon.last_error) if mon.last_error else "")
    ok = publish.republish()
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
