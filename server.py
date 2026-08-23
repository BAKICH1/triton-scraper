# -*- coding: utf-8 -*-
"""Веб-интерфейс и API монитора Kleinanzeigen. Стандартная библиотека, без зависимостей."""
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import monitor as monitor_mod

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
IMG_CACHE = os.path.join(db.DATA_DIR, "imgcache")
IMG_HOST_RE = re.compile(r"^https://img\.kleinanzeigen\.de/")
MON = monitor_mod.Monitor()
_img_lock = threading.Semaphore(6)


def json_resp(handler, obj, code=200, headers=None):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")  # для статического зеркала на here.now
    for k, v in (headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KlMonitor/1.0"

    def log_message(self, fmt, *args):
        pass  # тише в консоли

    # ----------------------------- GET -----------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, q = parsed.path, dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        try:
            if path in ("/", "/index.html"):
                self._file("index.html", "text/html; charset=utf-8")
            elif path == "/api/ads":
                self._api_ads(q)
            elif path == "/api/stats":
                st = db.stats()
                st["monitor"] = MON.info()
                st["scans"] = db.last_scans(10)
                json_resp(self, st)
            elif path == "/api/categories":
                json_resp(self, db.get_categories())
            elif path == "/api/sources":
                json_resp(self, db.list_sources())
            elif path == "/api/watches":
                json_resp(self, [s for s in db.list_sources() if s["type"] == "keyword"])
            elif path == "/img":
                self._img(q.get("u", ""))
            elif path == "/api/export.csv":
                self._export_csv(q)
            else:
                self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                json_resp(self, {"error": str(e)}, 500)
            except Exception:
                pass

    # ----------------------------- POST -----------------------------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        payload = {}
        if length:
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                payload = {}
        try:
            if parsed.path == "/api/scan":
                MON.wake()
                json_resp(self, {"ok": True, "queued": True})
            elif parsed.path == "/api/pause":
                paused = MON.toggle_pause()
                json_resp(self, {"ok": True, "paused": paused})
            elif parsed.path == "/api/settings":
                MON.update_cfg(**payload)
                json_resp(self, {"ok": True, "cfg": MON.cfg})
            elif parsed.path in ("/api/watches", "/api/sources"):
                stype = payload.get("type", "keyword")
                if stype == "category":
                    cat_id = int(payload.get("cat_id", 0) or 0)
                    cat = next((c for c in db.get_categories() if c["id"] == cat_id), None)
                    if not cat:
                        return json_resp(self, {"error": "Категория не найдена"}, 400)
                    value = f"{cat['slug']}:{cat['id']}"
                    sid = db.add_source("category", value, cat["name"], int(payload.get("pages", 2)))
                else:
                    kw = (payload.get("keyword") or "").strip()[:60]
                    if not kw:
                        return json_resp(self, {"error": "Пустое ключевое слово"}, 400)
                    sid = db.add_source("keyword", kw, f'Поиск: "{kw}"', 1)
                json_resp(self, {"ok": True, "id": sid})
            elif parsed.path in ("/api/watches/delete", "/api/sources/delete"):
                db.delete_source(int(payload.get("id", 0)))
                json_resp(self, {"ok": True})
            else:
                self.send_error(404)
        except Exception as e:
            json_resp(self, {"error": str(e)}, 500)

    # ----------------------------- обработчики -----------------------------
    def _api_ads(self, q):
        def num(v):
            try:
                return float(str(v).replace(",", ".")) if str(v).strip() != "" else None
            except Exception:
                return None

        category_id = int(q["category"]) if q.get("category", "").isdigit() else None
        watches = [s["value"] for s in db.list_sources() if s["type"] == "keyword"] if q.get("watches") == "1" else None
        ads = db.get_ads(
            q=q.get("q", "").strip()[:80],
            category_id=category_id,
            price_min=num(q.get("min")),
            price_max=num(q.get("max")),
            hide_pro=q.get("pro") == "0",
            only_pro=q.get("pro") == "1",
            only_free=q.get("free") == "1",
            watch_keywords=watches,
            limit=min(int(q.get("limit", "150") or 150), 400),
            offset=min(int(q.get("offset", "0") or 0), 100000),
            order=q.get("order", "first_seen"),
        )
        for a in ads:
            if a["img"]:
                a["img_proxy"] = "/img?u=" + urllib.parse.quote(a["img"], safe="")
        json_resp(self, {"ads": ads, "count": len(ads)})

    def _export_csv(self, q):
        import csv
        ads = db.get_ads(q=q.get("q", "").strip()[:80], limit=5000, order="first_seen")
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["id", "заголовок", "цена", "текст_цены", "город", "размещено", "категория", "ссылка", "PRO"])
        cats = {c["id"]: c["name"] for c in db.get_categories()}
        for a in ads:
            w.writerow([a["id"], a["title"], a["price_eur"] if a["price_eur"] is not None else "",
                        a["price_text"], a["location"], a["posted_at"] or a["date_text"],
                        cats.get(a["category_id"], ""), a["url"], "да" if a["is_pro"] else ""])
        body = "\ufeff" + buf.getvalue()
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="kleinanzeigen_export.csv"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _img(self, url):
        if not IMG_HOST_RE.match(url):
            self.send_error(403)
            return
        os.makedirs(IMG_CACHE, exist_ok=True)
        key = hashlib.sha1(url.encode()).hexdigest()
        cached = os.path.join(IMG_CACHE, key)
        if not os.path.exists(cached):
            with _img_lock:
                try:
                    data = MON.fetcher.get_bytes(url)
                    with open(cached + ".tmp", "wb") as f:
                        f.write(data)
                    os.replace(cached + ".tmp", cached)
                    if len(os.listdir(IMG_CACHE)) > 6000:  # простая очистка
                        for f in sorted((os.path.getmtime(os.path.join(IMG_CACHE, x)), x)
                                        for x in os.listdir(IMG_CACHE))[:2000]:
                            try:
                                os.remove(os.path.join(IMG_CACHE, f[1]))
                            except OSError:
                                pass
                except Exception:
                    self.send_error(502)
                    return
        try:
            with open(cached, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(502)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, name, ctype):
        fp = os.path.join(STATIC, name)
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    db.init()
    MON.start()
    monitor_mod.REPUBLISH.start()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"✅ Монитор Kleinanzeigen запущен: http://0.0.0.0:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
