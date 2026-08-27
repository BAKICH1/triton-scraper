# -*- coding: utf-8 -*-
"""Фоновый монитор: опрашивает источники, складывает объявления в БД."""
import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone

import db
import scraper

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_cfg():
    cfg = {
        "scan_interval_sec": 90,
        "global_pages": 2,
        "request_delay_sec": 6,
        "backoff_max_sec": 600,
        "retention_days": 21,
        "views_per_round": 6,
        "views_refresh_hours": 4,
        "user_agent": scraper.DEFAULT_UA,
    }
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
    except Exception:
        pass
    return cfg


class Republisher(threading.Thread):
    """Периодически перезаливает снапшот на here.now (тот же URL, по claimToken)."""

    def __init__(self, interval=180):
        super().__init__(daemon=True)
        self.interval = interval
        self.status = "не запускался"
        self.enabled = False
        self._wake = threading.Event()

    def run(self):
        import publish
        self.enabled = publish.load_claim() != (None, None)
        if not self.enabled:
            self.status = "нет claimToken — публикация вручную: python3 publish.py"
            return
        while True:
            try:
                slug = publish.republish()
                self.status = f"обновлён {time.strftime('%H:%M:%S')} · https://{slug}.here.now"
            except Exception as e:
                self.status = f"ошибка: {e}"
            self._wake.wait(self.interval)
            self._wake.clear()

    def info(self):
        return {"enabled": self.enabled, "status": self.status, "interval": self.interval}


REPUBLISH = Republisher(interval=int(load_cfg().get("republish_sec", 90)))


class Monitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.cfg = load_cfg()
        self.fetcher = scraper.Fetcher(ua=self.cfg["user_agent"], min_delay=self.cfg["request_delay_sec"])
        # отдельный лёгкий клиент для счётчика просмотров: крошечные JSON GET,
        # не тормозят и не тормозятся общей 4-секундной очередью сканера
        self.vfetcher = scraper.Fetcher(ua=self.cfg["user_agent"], min_delay=1.0)
        self.paused = False
        self.status = "starting"          # ok | blocked | error | paused | starting
        self.last_scan_at = None
        self.next_scan_at = None
        self.last_error = ""
        self.new_in_last_scan = 0
        self._wake = threading.Event()
        self._backoff = 0
        self.last_block_ts = 0.0

    # ---------- управление ----------
    def wake(self):
        self._wake.set()

    def toggle_pause(self, paused=None):
        self.paused = (not self.paused) if paused is None else paused
        self.status = "paused" if self.paused else "ok"
        self.wake()
        return self.paused

    def update_cfg(self, **kw):
        for k in ("scan_interval_sec", "global_pages", "request_delay_sec", "retention_days"):
            if k in kw and kw[k] is not None:
                self.cfg[k] = int(kw[k])
        self.fetcher.min_delay = float(self.cfg["request_delay_sec"])
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        self.wake()

    def info(self):
        return {
            "status": "paused" if self.paused else self.status,
            "last_scan_at": self.last_scan_at,
            "next_scan_at": self.next_scan_at,
            "new_in_last_scan": self.new_in_last_scan,
            "last_error": self.last_error,
            "interval": self.cfg["scan_interval_sec"],
            "backoff": self._backoff,
            "cfg": self.cfg,
            "herenow": REPUBLISH.info() if REPUBLISH.is_alive() or REPUBLISH.enabled else None,
        }

    # ---------- построение URL источника ----------
    def _source_urls(self, src):
        pages = int(src.get("pages") or 1)
        if src["type"] == "global":
            urls = scraper.global_feed_urls(max(self.cfg["global_pages"], 1))
            # свежесть гарантируют первые 4 страницы (каждый цикл); глубокие —
            # по 3 за раз по кругу: экономим ~90 с на просмотры каждую итерацию
            if len(urls) > 4:
                try:
                    off = int(db.kv_get("glob_rot", 0) or 0)
                except Exception:
                    off = 0
                tail = urls[4:]
                i = off % len(tail)
                rot = (tail[i:] + tail[:i])[:3]
                try:
                    db.kv_set("glob_rot", (off + 3) % len(tail))
                except Exception:
                    pass
                urls = urls[:4] + rot
            return urls
        if src["type"] == "category":
            try:
                slug, cid = src["value"].split(":")
                return scraper.category_urls(cid, slug, pages)
            except Exception:
                return []
        if src["type"] == "keyword":
            return scraper.keyword_urls(src["value"], pages)
        return []

    # ---------- цикл ----------
    def run(self):
        # категории: пробуем обновить с сайта, иначе файл
        try:
            cats = scraper.parse_categories_from_home(self.fetcher.get(scraper.BASE + "/"))
            if len(cats) > 50:
                db.save_categories(cats)
        except Exception:
            pass
        if not db.get_categories():
            fb = scraper.load_fallback_categories(os.path.join(db.DATA_DIR, "categories_fallback.json"))
            if fb:
                db.save_categories(fb)
        while True:
            if self.paused:
                self.status = "paused"
                self._wake.wait(5)
                self._wake.clear()
                continue
            started = time.time()
            self._scan_round()
            elapsed = time.time() - started
            interval = max(15, int(self.cfg["scan_interval_sec"])) + self._backoff
            self.next_scan_at = (datetime.now(timezone.utc).timestamp() + max(5, interval - elapsed))
            self._wake.wait(max(5, interval - elapsed))
            self._wake.clear()

    def _scan_round(self):
        t0 = datetime.now(timezone.utc).isoformat(timespec="seconds")
        total_new, total_found, errors = 0, 0, 0
        try:   # созревшие 20+ минут — из стейджинга в базу
            total_new += db.promote_due(20)
        except Exception:
            pass
        try:
            sources = db.list_sources(only_active=True)
        except Exception:
            sources = [{"type": "global", "pages": 2}]
        # категории — по k за раунд по кругу: полный круг ~4 цикла (~25 мин),
        # новые объявления всё равно ловит глобальная лента первых страниц
        cats = [s for s in sources if s["type"] == "category"]
        k = max(1, int(self.cfg.get("cats_per_round", 4)))
        if len(cats) > k:
            try:
                off = int(db.kv_get("cat_rot", 0) or 0)
            except Exception:
                off = 0
            i = off % len(cats)
            pick = (cats[i:] + cats[:i])[:k]
            try:
                db.kv_set("cat_rot", (off + k) % len(cats))
            except Exception:
                pass
            sources = [s for s in sources if s["type"] != "category"] + pick
        for src in sources:
            urls = self._source_urls(src)
            for url in urls:
                if self.paused:
                    return
                for attempt in range(2):
                    try:
                        html = self.fetcher.get(url)
                        ads = scraper.parse_listing(html)
                        # моложе 20 минут не берём сразу: откладываем и введём,
                        # когда исполнится 20 минут от публикации (иначе потеряем)
                        keep, young = [], []
                        for ad in ads:
                            pt = ad.get("posted_at")
                            try:
                                age = (datetime.now(timezone.utc) - datetime.fromisoformat(pt)).total_seconds() / 60 if pt else None
                            except Exception:
                                age = None
                            if age is not None and age < 20:
                                young.append(ad)
                            else:
                                keep.append(ad)
                        if young:
                            db.stage_ads(young)
                        ads = keep
                        total_found += len(ads)
                        n_new, _ = db.upsert_ads(ads, source=src["type"])
                        total_new += n_new
                        self.status = "ok"
                        self._backoff = 0
                        break
                    except scraper.BlockedError as e:
                        self.status = "blocked"
                        self.last_error = str(e)
                        self.last_block_ts = time.time()
                        self._backoff = min(self._backoff * 2 if self._backoff else 90, self.cfg["backoff_max_sec"])
                        time.sleep(min(self._backoff, 120) if attempt else 20)
                    except Exception as e:
                        errors += 1
                        self.last_error = f"{type(e).__name__}: {e}"
                        traceback.print_exc()
                        time.sleep(5)
        self.new_in_last_scan = total_new
        try:
            db.prune_views_hist(24)   # история приростов: сутки с запасом
        except Exception:
            pass
        self.last_scan_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            db.log_scan("ok" if not self.last_error else "warn", total_found, total_new, started_at=t0)
            if total_found:
                db.cleanup_ads(self.cfg["retention_days"])
        except Exception:
            pass
        # просмотры: понемногу, т.к. каждый запрос +1 к счётчику объявления.
        # Блок скана не отменяет замеры (другой клиент, другой темп) — только
        # свежий блок (<90 с): значит IP в бане и для просмотров.
        if self.status != "blocked" or (time.time() - self.last_block_ts > 90):
            try:
                ids = db.views_plan(
                    board_cats=tuple(self.cfg.get("views_priority_cats", [161, 80, 153, 192])),
                    hot_minutes=int(self.cfg.get("hot_window_min", 22)),
                    board_n=int(self.cfg.get("views_board_n", 8)),
                    first_n=int(self.cfg.get("views_hot_first", 18)),
                    retop_n=int(self.cfg.get("views_hot_retop", 12)),
                    reterm_n=int(self.cfg.get("views_reterm", 10)),
                    refresh_after_hours=int(self.cfg.get("views_refresh_hours", 4)),
                    published_limit=int(self.cfg.get("snapshot_ads", 800)),
                    exclude_titles=tuple(self.cfg.get(
                        "views_exclude_titles", ["iphone", "айфон", "Айфон", "АЙФОН"])),
                )
                for ad_id in ids:
                    if self.paused:
                        break
                    v = self.vfetcher.get_views(ad_id)
                    if v is not None:
                        db.update_views(ad_id, v)
                    time.sleep(0.2)  # бережно: не дёргаем счётчик очередью
            except Exception:
                pass
