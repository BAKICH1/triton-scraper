# -*- coding: utf-8 -*-
"""Публикация/обновление снапшота дашборда на here.now.

- Создание:  python3 publish.py            (если нет сохранённого slug+claimToken)
- Обновление того же URL:  python3 publish.py --update
- data/herenow_claim.txt хранит slug и claimToken (для анонимных сайтов)
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import db

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "static", "snapshot_template.html")
OUT = os.path.join(HERE, "snapshot_index.html")
OUT_VER = os.path.join(HERE, "snapshot_version.txt")
OUT_DATA = os.path.join(HERE, "snapshot_ads.json")
STATE_FILE = os.path.join(HERE, "data", "publish_state.json")
CLAIM_FILE = os.path.join(HERE, "data", "herenow_claim.txt")
API = "https://here.now/api/v1/publish"
HEADERS = {"content-type": "application/json", "X-HereNow-Client": "arena/agent-mode"}


def _view_rate(r):
    """Скорость набора просмотров, шт/мин. Честная — по паре замеров (Δ);
    иначе средняя от момента публикации. Без posted_at fallback не даём:
    старые топ-объявления с тысячами просмотров иначе дают дикие цифры."""
    from datetime import datetime, timezone

    def parse(s):
        try:
            return datetime.fromisoformat(str(s)) if s else None
        except Exception:
            return None

    def mins(a, b):
        if not a or not b:
            return None
        try:
            if a.tzinfo is None:
                a = a.replace(tzinfo=timezone.utc)
            if b.tzinfo is None:
                b = b.replace(tzinfo=timezone.utc)
            return (b - a).total_seconds() / 60.0
        except Exception:
            return None

    v, vp = r["views"], r["views_prev"]
    if v is None:
        return None
    now = datetime.now(timezone.utc)
    measured = parse(r["views_at"]) or now
    if vp is not None:
        m = mins(parse(r["views_prev_at"]), measured)
        if m and m >= 0.5 and v >= vp:
            return round((v - vp) / m, 1)
    m2 = mins(parse(r["posted_at"]), measured)   # от публикации, не от обнаружения
    if m2 and m2 >= 2.0:
        return round(v / m2, 1)
    return None


def collect_ads(limit=500, board_cats=(161, 80, 153, 192), board_hours=6, board_limit=1200):
    """Новейшие `limit` объявлений (лента, с описанием) + backlog досочных категорий
    (столбцы; без описания — легче). Ленте хватит 140 символов описания для поиска."""
    from datetime import datetime, timezone, timedelta
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    cats = {c["id"]: c["name"] for c in conn.execute("SELECT id,name FROM categories")}
    cat_rows = [dict(r) for r in conn.execute(
        "SELECT id,slug,name,parent FROM categories ORDER BY parent NULLS FIRST, id")]
    cols = """id,title,descr,price_text,price_eur,negotiable,location,date_text,
                  posted_at,category_id,is_pro,is_top,member_since,img_count,url,img,first_seen,
                  views,views_prev,views_at,views_prev_at"""
    rows = conn.execute(
        f"""SELECT {cols} FROM ads ORDER BY first_seen DESC LIMIT ?""", (limit,)).fetchall()
    # backlog досочных категорий (с дочерними) за последние board_hours,
    # ниже самого старого объявления основного окна — дедуп в Python на всякий случай
    ph = ",".join("?" * len(board_cats))
    try:
        extra = conn.execute(
            f"""SELECT {cols} FROM ads
                WHERE category_id IN (SELECT id FROM categories WHERE id IN ({ph}) OR parent IN ({ph}))
                  AND first_seen >= ?
                  AND first_seen < (SELECT MIN(first_seen) FROM
                                    (SELECT first_seen FROM ads ORDER BY first_seen DESC LIMIT ?))
                ORDER BY first_seen DESC LIMIT ?""",
            (*board_cats, *board_cats,
             (datetime.now(timezone.utc) - timedelta(hours=board_hours)).isoformat(timespec="seconds"),
             limit, board_limit)).fetchall()
    except Exception:
        extra = []
    ads, ids = [], set()

    def add(r, is_feed):
        if r["id"] in ids:
            return
        ids.add(r["id"])
        a = dict(r) | {"cat_name": cats.get(r["category_id"], ""), "vr": _view_rate(r)}
        a["descr"] = (a["descr"] or "")[:140] if is_feed else ""
        for k in ("views_prev", "views_prev_at", "views_at"):  # служебное наружу не отдаём
            a.pop(k, None)
        ads.append(a)

    for r in rows:
        add(r, True)
    for r in extra:
        add(r, False)
    ads.sort(key=lambda a: a["first_seen"], reverse=True)
    total = conn.execute("SELECT COUNT(*) c FROM ads").fetchone()["c"]
    conn.close()
    return ads, total, cat_rows


def build():
    ads, total, cat_rows = collect_ads(limit=2600)
    html_tpl = open(TEMPLATE, encoding="utf-8").read()
    tpl_hash = hashlib.md5(html_tpl.encode("utf-8")).hexdigest()[:8]
    stamp = datetime.now(timezone.utc)
    version = f"v{int(stamp.timestamp())}.{total}"
    ts_iso = stamp.isoformat(timespec="seconds")

    # ads.json — живые данные: страница подтягивает их сама, без перезагрузки
    data = {"tpl": tpl_hash, "ver": version, "ts": ts_iso, "total": total, "ads": ads}
    data_str = json.dumps(data, ensure_ascii=False)
    with open(OUT_DATA, "w", encoding="utf-8") as f:
        f.write(data_str)

    # index.html перезаливаем при смене шаблона ИЛИ числа категорий
    # (категории зашиты в index) — данные живут в ads.json
    sig = f"{tpl_hash}:{len(cat_rows)}"
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        state = {}
    if state.get("sig") != sig or not os.path.exists(OUT):
        html = (html_tpl
                .replace("/*__ADS__*/[]", json.dumps(ads[:150], ensure_ascii=False))
                .replace("/*__CATS__*/[]", json.dumps(cat_rows, ensure_ascii=False))
                .replace("__TOTAL__", str(total))
                .replace("__STAMP__", stamp.strftime("%d.%m.%Y %H:%M UTC"))
                .replace("__TSISO__", ts_iso)
                .replace("__TPLHASH__", tpl_hash)
                .replace("__VERSION__", version))
        _syntax_check(html)
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(html)
        json.dump({"sig": sig}, open(STATE_FILE, "w", encoding="utf-8"))
        print(f"✔ index.html пересобран (шаблон {tpl_hash})")
    with open(OUT_VER, "w", encoding="utf-8") as f:
        f.write(version)
    print(f"✔ сборка: {len(ads)} объявлений, ads.json {len(data_str) // 1024} КБ, версия {version}")
    return {OUT_DATA: ("ads.json", "application/json; charset=utf-8"),
            OUT_VER: ("version.txt", "text/plain; charset=utf-8"),
            OUT: ("index.html", "text/html; charset=utf-8")}


def _syntax_check(html):
    """Проверка JS собранной страницы — защита от публикации битой версии."""
    import re as _re
    import subprocess
    import tempfile
    for i, block in enumerate(_re.findall(r"<script>(.*?)</script>", html, _re.S), 1):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as tf:
            tf.write(block)
            path = tf.name
        p = subprocess.run(["node", "--check", path], capture_output=True)
        os.unlink(path)
        if p.returncode != 0:
            raise RuntimeError(f"JS блок {i} битый, публикация отменена:\n{p.stderr.decode()[:300]}")


def _files_meta(files):
    out = []
    for path, (rpath, ctype) in files.items():
        data = open(path, "rb").read()
        out.append({"path": rpath, "size": len(data), "contentType": ctype,
                    "hash": hashlib.sha256(data).hexdigest()})
    return out


def _upload(files, j, api_key=""):
    ups = {(u.get("path")): u for u in j.get("upload", {}).get("uploads", [])}
    for path, (rpath, ctype) in files.items():
        if rpath not in ups:
            continue  # hash совпал, сервер скопирует сам
        data = open(path, "rb").read()
        put = urllib.request.Request(ups[rpath]["url"], data=data, method="PUT",
                                     headers={"Content-Type": ctype})
        with urllib.request.urlopen(put, timeout=90) as r:
            r.read()
    u = j["upload"]
    fin_headers = {"content-type": "application/json"}
    if api_key:
        fin_headers["Authorization"] = f"Bearer {api_key}"
    fin = urllib.request.Request(u["finalizeUrl"], method="POST",
        data=json.dumps({"versionId": u["versionId"]}).encode(),
        headers=fin_headers)
    with urllib.request.urlopen(fin, timeout=30) as r:
        r.read()


def create(files, name="Монитор Kleinanzeigen", desc="Новые объявления Kleinanzeigen во всех категориях, живое обновление"):
    body = json.dumps({
        "files": _files_meta(files),
        "displayName": name,
        "displayDescription": desc,
        "viewer": {"title": name, "description": desc},
    }).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.load(r)
    _upload(files, j)
    _save_claim(j)
    print("SITE_URL:", j.get("siteUrl"))
    return j


def update(slug, claim_token, files):
    hdrs = dict(HEADERS)
    api_key = os.environ.get("HERENOW_API_KEY", "").strip()
    if api_key:
        hdrs["Authorization"] = f"Bearer {api_key}"
        claim_token = ""          # закреплённый сайт: только Bearer, без claimToken
    payload = {"files": _files_meta(files)}
    if claim_token:
        payload["claimToken"] = claim_token
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{API}/{slug}", data=body, method="PUT", headers=hdrs)
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.load(r)
    _upload(files, j, api_key)
    return j


def _save_claim(j):
    with open(CLAIM_FILE, "w", encoding="utf-8") as f:
        f.write(f"site: {j.get('siteUrl')}\nslug: {j.get('slug')}\n"
                f"claim: {j.get('claimUrl')}\nclaimToken: {j.get('claimToken')}\n")
    if j.get("claimUrl"):
        print("CLAIM_URL:", j["claimUrl"])


def load_claim():
    try:
        vals = {}
        for line in open(CLAIM_FILE, encoding="utf-8"):
            if ":" in line:
                k, v = line.split(":", 1)
                vals[k.strip()] = v.strip()
        slug = vals.get("slug")
        if not slug and vals.get("site"):
            slug = vals["site"].rstrip("/").split("//")[-1].split(".")[0]
        if slug and vals.get("claimToken"):
            return slug, vals["claimToken"]
    except Exception:
        pass
    return None, None


def republish():
    """Собрать снапшот и залить на here.now.

    Если адрес закреплён через HERENOW_SLUG (секрет CI) — обновляем только его:
    при ошибке ПАДАЕМ громко, никаких тихих клонов со сменой адреса.
    Случайный новый сайт создаём только когда адрес вообще не задан.
    """
    files = build()
    slug, token = load_claim()
    pinned = os.environ.get("HERENOW_SLUG", "").strip()
    if pinned:                       # адрес зафиксирован секретом — только он
        token = token or os.environ.get("HERENOW_CLAIM", "").strip()
        update(pinned, token, files)
        print(f"↑ here.now обновлён: https://{pinned}.here.now/ ({time.strftime('%H:%M:%S')})")
        return pinned
    if slug and token:
        try:
            update(slug, token, files)
            print(f"↑ here.now обновлён: https://{slug}.here.now/ ({time.strftime('%H:%M:%S')})")
            return slug
        except urllib.error.HTTPError as e:
            print(f"⚠ обновление here.now не удалось (HTTP {e.code}), пробую создать новый сайт")
        except Exception as e:
            print(f"⚠ обновление here.now не удалось: {e}")
    j = create(files)
    return j.get("slug")


if __name__ == "__main__":
    republish()
