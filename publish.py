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
ADMIN_TPL = os.path.join(HERE, "static", "admin.html")
OUT_ADMIN = os.path.join(HERE, "snapshot_admin.html")
PANEL_CLAIM = os.path.join(HERE, "data", "panel_claim.txt")
SUPA_CFG = os.path.join(HERE, "static", "supa.json")
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
                  views,views_prev,views_at,views_prev_at,descr_full,imgs"""
    rows = conn.execute(
        f"""SELECT {cols} FROM ads WHERE price_eur IS NOT NULL
            ORDER BY first_seen DESC LIMIT ?""", (limit,)).fetchall()
    # backlog досочных категорий (с дочерними) за последние board_hours,
    # ниже самого старого объявления основного окна — дедуп в Python на всякий случай
    ph = ",".join("?" * len(board_cats))
    try:
        extra = conn.execute(
            f"""SELECT {cols} FROM ads
                WHERE category_id IN (SELECT id FROM categories WHERE id IN ({ph}) OR parent IN ({ph}))
                  AND price_eur IS NOT NULL
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
            return None
        ids.add(r["id"])
        a = dict(r) | {"cat_name": cats.get(r["category_id"], ""), "vr": _view_rate(r)}
        a["descr"] = (a["descr"] or "")[:140] if is_feed else ""
        # полное описание и галерея — карточка открывается мгновенно, без прокси
        a["descr_full"] = (a.get("descr_full") or None) and a["descr_full"][:300]   # компакт: карточке хватает
        try:
            gal = json.loads(a.get("imgs") or "[]")
        except Exception:
            gal = []
        a["imgs"] = [u for u in gal][:5]
        for k in ("views_prev_at", "views_at"):  # служебное наружу не отдаём
            a.pop(k, None)   # views_prev нужен клиенту: прирост за цикл
        ads.append(a)
        return a

    # прирост просмотров за окна: 20м / 1ч / 2ч / 4ч / 8ч (по истории замеров)
    WINDOWS = ((20, "20"), (60, "60"), (120, "120"), (240, "240"), (480, "480"))
    now_dt = datetime.now(timezone.utc)
    hist = {}
    try:
        for h in conn.execute(
                "SELECT ad_id, ts, views FROM views_hist WHERE ts >= ?",
                ((now_dt - timedelta(hours=9)).isoformat(timespec="seconds"),)):
            hist.setdefault(h["ad_id"], []).append((h["ts"], h["views"]))
    except Exception:
        hist = {}
    # собственные пары замеров объявлений — тоже точки истории
    # (мгновенно даёт каждому измеряемому объявлению окно между замерами)
    for r0 in list(rows) + list(extra):
        pts0 = hist.setdefault(r0["id"], [])
        if r0["views_prev_at"] and r0["views_prev"] is not None:
            pts0.append((r0["views_prev_at"], r0["views_prev"]))
        if r0["views_at"] and r0["views"] is not None:
            pts0.append((r0["views_at"], r0["views"]))
        pts0.sort(key=lambda x: x[0])

    def growth(ad_id, views_now):
        pts = hist.get(ad_id)
        if not pts or views_now is None:
            return None
        out = {}
        for mins, key in WINDOWS:
            cutoff = (now_dt - timedelta(minutes=mins)).isoformat(timespec="seconds")
            base = None
            for ts, v in pts:            # последняя точка ДО окна
                if ts <= cutoff:
                    base = v
                else:
                    break
            out[key] = max(0, views_now - base) if base is not None else None
        return out or None

    for r in rows:
        a = add(r, True)
        if a is not None:
            a["gr"] = growth(r["id"], r["views"])
    for r in extra:
        a = add(r, False)
        if a is not None:
            a["gr"] = growth(r["id"], r["views"])
    ads.sort(key=lambda a: a["first_seen"], reverse=True)
    total = conn.execute("SELECT COUNT(*) c FROM ads").fetchone()["c"]
    conn.close()
    return ads, total, cat_rows


def data_filename():
    """Имя файла данных: негадаемое (md5 от секретного slug) — случайный посетитель
    here.now не вытащит объявления прямой ссылкой /ads.json. Slug в репо НЕ хранится."""
    slug = os.environ.get("HERENOW_SLUG", "").strip()
    if not slug:
        slug, _ = load_claim()
        slug = slug or os.environ.get("HERENOW_SLUG", "local").strip() or "local"
    return "d_" + hashlib.md5(("kl19::" + slug).encode()).hexdigest()[:12] + ".json"


def build():
    ads, total, cat_rows = collect_ads(limit=3200)
    html_tpl = open(TEMPLATE, encoding="utf-8").read()
    tg_name = ""
    try:
        tg_name = db.kv_get("tg_bot_name", "") or ""
    except Exception:
        pass
    tpl_hash = hashlib.md5(html_tpl.encode("utf-8")).hexdigest()[:8]

    supa_url, supa_key = _supa_cfg()
    stamp = datetime.now(timezone.utc)
    version = f"v{int(stamp.timestamp())}.{total}"
    ts_iso = stamp.isoformat(timespec="seconds")

    dfile = data_filename()
    # живые данные: страница подтягивает их сама, без перезагрузки
    data = {"tpl": tpl_hash, "ver": version, "ts": ts_iso, "total": total, "ads": ads}
    data_str = json.dumps(data, ensure_ascii=False)
    with open(OUT_DATA, "w", encoding="utf-8") as f:
        f.write(data_str)

    # index.html перезаливаем при смене шаблона ИЛИ числа категорий
    # (категории зашиты в index) — данные живут в ads.json
    sig = f"{tpl_hash}:{len(cat_rows)}:{tg_name}:{supa_url}"
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        state = {}
    if state.get("sig") != sig or not os.path.exists(OUT):
        html = (html_tpl
                .replace("__TGBOT__", tg_name)
                .replace("/*__ADS__*/[]", "[]")   # объявления НЕ вшиваем: исходник страницы чистый
                .replace("/*__CATS__*/[]", json.dumps(cat_rows, ensure_ascii=False))
                .replace("__TOTAL__", str(total))
                .replace("__STAMP__", stamp.strftime("%d.%m.%Y %H:%M UTC"))
                .replace("__TSISO__", ts_iso)
                .replace("__TPLHASH__", tpl_hash)
                .replace("__VERSION__", version)
                .replace("__SUPA_URL__", supa_url)
                .replace("__SUPA_KEY__", supa_key)
                .replace("__DATAFILE__", dfile))
        _syntax_check(html)
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(html)
        json.dump({"sig": sig}, open(STATE_FILE, "w", encoding="utf-8"))
        print(f"✔ index.html пересобран (шаблон {tpl_hash})")
    with open(OUT_VER, "w", encoding="utf-8") as f:
        f.write(version)


    print(f"✔ сборка: {len(ads)} объявлений, ads.json {len(data_str) // 1024} КБ, версия {version}")
    return {OUT_DATA: (dfile, "application/json; charset=utf-8"),
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


def create(files, name="Монитор Kleinanzeigen", desc="Новые объявления Kleinanzeigen во всех категориях, живое обновление",
           claim_file=None, api_key=""):
    hdrs = dict(HEADERS)
    if api_key:
        hdrs["Authorization"] = f"Bearer {api_key}"
    body = json.dumps({
        "files": _files_meta(files),
        "displayName": name,
        "displayDescription": desc,
        "viewer": {"title": name, "description": desc},
    }).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers=hdrs)
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.load(r)
    _upload(files, j, api_key)
    _save_claim(j, claim_file or CLAIM_FILE)
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


def _save_claim(j, path=CLAIM_FILE):
    with open(path, "w", encoding="utf-8") as f:
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
        if slug:
            return slug, vals.get("claimToken", "")   # токена может не быть: сайт закреплён API-ключом
    except Exception:
        pass
    return None, None


def _supa_cfg():
    """ключи Supabase (пусто → плейсхолдеры остаются, аккаунты/панель спят)"""
    supa_url, supa_key = "__SUPA_URL__", "__SUPA_KEY__"
    try:
        _cfg = json.load(open(SUPA_CFG, encoding="utf-8"))
        if _cfg.get("url"):
            supa_url, supa_key = _cfg["url"], _cfg.get("key", "")
    except Exception:
        pass
    return supa_url, supa_key


def build_admin():
    """Панель управления — отдельный одностраничный сайт (без БД)."""
    supa_url, supa_key = _supa_cfg()
    adm = open(ADMIN_TPL, encoding="utf-8").read()
    adm = adm.replace("__SUPA_URL__", supa_url).replace("__SUPA_KEY__", supa_key)
    _syntax_check(adm)
    with open(OUT_ADMIN, "w", encoding="utf-8") as f:
        f.write(adm)
    print("✔ панель собрана (admin)")
    return {OUT_ADMIN: ("index.html", "text/html; charset=utf-8")}


def _load_panel_claim():
    try:
        vals = {}
        for line in open(PANEL_CLAIM, encoding="utf-8"):
            if ":" in line:
                k, v = line.split(":", 1)
                vals[k.strip()] = v.strip()
        slug = vals.get("slug")
        if not slug and vals.get("site"):
            slug = vals["site"].rstrip("/").split("//")[-1].split(".")[0]
        if slug:
            return slug, vals.get("claimToken", "")   # токена может не быть: сайт закреплён API-ключом
    except Exception:
        pass
    return None, None


def _local_api_key():
    try:
        return json.load(open(os.path.join(os.path.dirname(HERE), "herenow_key.json"))).get("apiKey", "")
    except Exception:
        return ""


def publish_panel():
    """Панель управления — ОТДЕЛЬНЫЙ сайт here.now (свой URL, свой claim)."""
    files = build_admin()
    api_key = os.environ.get("HERENOW_API_KEY", "").strip() or _local_api_key()
    slug, token = _load_panel_claim()
    if api_key:
        os.environ["HERENOW_API_KEY"] = api_key   # update() берёт ключ из окружения
    if slug:
        try:
            update(slug, token, files)
            print(f"↑ панель обновлена: https://{slug}.here.now/")
            return slug
        except Exception as e:
            print(f"⚠ обновление панели не удалось ({e}), создаю новый сайт")
    j = create(files, name="Triton — панель управления",
               desc="Выдача времени доступа на аккаунты Triton",
               claim_file=PANEL_CLAIM, api_key=api_key)
    return j.get("slug")


def republish():
    """Собрать снапшот и залить на here.now.

    Если адрес закреплён через HERENOW_SLUG (секрет CI) — обновляем только его:
    при ошибке ПАДАЕМ громко, никаких тихих клонов со сменой адреса.
    Случайный новый сайт создаём только когда адрес вообще не задан.
    """
    try:
        publish_panel()
    except Exception as e:
        print(f"⚠ панель не опубликована: {e}")

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
