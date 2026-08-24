# -*- coding: utf-8 -*-
"""Парсер Kleinanzeigen (kleinanzeigen.de): глобальная лента + категории + поиск по ключевым словам."""
import gzip
import html as H
import io
import json
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
BASE = "https://www.kleinanzeigen.de"
GLOBAL_FEED = BASE + "/s-suchen.html"          # все категории, сортировка «Neueste»
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class BlockedError(Exception):
    """IP временно заблокирован (403/429) — нужно отступить."""


class Fetcher:
    """HTTP-клиент с вежливой задержкой между запросами к сайту."""

    def __init__(self, ua=DEFAULT_UA, min_delay=6.0, timeout=25):
        self.ua = ua
        self.min_delay = min_delay
        self.timeout = timeout
        self._last = 0.0

    def get(self, url: str) -> str:
        wait = self.min_delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.7,ru;q=0.5",
            "Accept-Encoding": "gzip",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise BlockedError(f"HTTP {e.code} от kleinanzeigen.de")
            raise
        finally:
            self._last = time.monotonic()

    def get_bytes(self, url: str, timeout: int = 12) -> bytes:
        """Для прокси картинок — без общей очереди задержек."""
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Referer": BASE + "/"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def get_views(self, ad_id: str) -> int | None:
        """Счётчик просмотров объявления (публичный эндпоинт; каждый запрос +1 к счётчику!)."""
        url = f"{BASE}/s-vac-inc-get.json?adId={ad_id}"
        wait = self.min_delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua, "Accept": "application/json", "Referer": BASE + "/",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return int(data.get("numVisits"))
        except Exception:
            return None
        finally:
            self._last = time.monotonic()


# ----------------------------- Разбор страниц -----------------------------

ARTICLE_RE = re.compile(r'<article[^>]*data-adid="(\d+)"[^>]*data-href="([^"]+)"(.*?)</article>', re.S)
TITLE_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*>(.*?)</a>', re.S)
DESC_RE = re.compile(r'class="aditem-main--middle--description">(.*?)</p>', re.S)
PRICE_RE = re.compile(r'class="aditem-main--middle--price-shipping--price">\s*(.*?)\s*(?:</p>|<span)', re.S)
LOC_RE = re.compile(r'icon-pin-gray"[^>]*></i>\s*([^<]+)', re.S)
DATE_RE = re.compile(r'aditem-main--top--right">\s*<i[^>]*></i>\s*([^<]+?)\s*</div>', re.S)
IMG_RE = re.compile(r'<img\s+src="(https://img\.kleinanzeigen\.de[^"]+)"', re.S)
PRO_RE = re.compile(r'badge-hint-pro-small-srp')
CAT_RE = re.compile(r"/(\d+)-(\d+)-\d+$")
GALLERY_RE = re.compile(r'galleryimage--counter">\s*(\d+)')


def _txt(s):
    return H.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def parse_price(price_text):
    """'4.650 € VB' -> (4650.0, True); 'Zu verschenken' -> (0.0, False); '' -> (None, False)"""
    if not price_text:
        return None, False
    neg = False
    if "VB" in price_text:
        neg = True
    m = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*€", price_text)
    if m:
        num = m.group(1).replace(".", "").replace(",", ".")
        return float(num), neg
    if "verschenk" in price_text.lower():
        return 0.0, False
    return None, neg


def parse_date(date_text, now=None):
    """'Heute, 11:17' / 'Gestern, 08:01' / '17.08.26' -> ISO datetime (Europe/Berlin) или None."""
    if not date_text:
        return None
    now = now or datetime.now(BERLIN)
    t = date_text.strip()
    m = re.match(r"^Heute,\s*(\d{1,2}):(\d{2})$", t)
    if m:
        d = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if d > now:
            d -= timedelta(days=1)
        return d.isoformat()
    m = re.match(r"^Gestern,\s*(\d{1,2}):(\d{2})$", t)
    if m:
        d = (now - timedelta(days=1)).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return d.isoformat()
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{2})$", t)
    if m:
        dd, mm, yy = int(m.group(1)), int(m.group(2)), 2000 + int(m.group(3))
        try:
            return datetime(yy, mm, dd, 12, 0, tzinfo=BERLIN).isoformat()
        except ValueError:
            return None
    return None


def parse_listing(html_text: str):
    """Разбирает страницу выдачи -> список объявлений (dict)."""
    ads = []
    for m in ARTICLE_RE.finditer(html_text):
        ad_id, href, body = m.group(1), m.group(2), m.group(3)
        # TOP-объявление: родительский <li> перед статьёй несёт badge-topad
        pre = html_text[max(0, m.start() - 300):m.start()]
        is_top = int("badge-topad" in pre or "is-topad" in pre)
        cat_id = None
        cm = CAT_RE.search(href)
        if cm:
            cat_id = int(cm.group(2))
        price_text = _txt(PRICE_RE.search(body).group(1)) if PRICE_RE.search(body) else ""
        price_eur, negotiable = parse_price(price_text)
        date_text = _txt(DATE_RE.search(body).group(1)) if DATE_RE.search(body) else ""
        img_m = IMG_RE.search(body)
        gal = GALLERY_RE.search(body)
        ads.append({
            "id": ad_id,
            "title": _txt(TITLE_RE.search(body).group(1)) if TITLE_RE.search(body) else "",
            "descr": _txt(DESC_RE.search(body).group(1))[:300] if DESC_RE.search(body) else "",
            "price_text": price_text or "—",
            "price_eur": price_eur,
            "negotiable": int(negotiable),
            "location": _txt(LOC_RE.search(body).group(1)) if LOC_RE.search(body) else "",
            "date_text": date_text,
            "posted_at": parse_date(date_text),
            "category_id": cat_id,
            "is_pro": int(bool(PRO_RE.search(body))),
            "is_top": is_top,
            "img_count": int(gal.group(1)) if gal else 0,
            "url": BASE + href,
            "img": img_m.group(1) if img_m else None,
        })
    return ads


# ----------------------------- URL источников -----------------------------

def global_feed_urls(pages=2):
    urls = [GLOBAL_FEED]
    for p in range(2, max(2, pages) + 1):
        urls.append(f"{BASE}/s-seite:{p}")
    return urls


def category_urls(cat_id, slug, pages=1):
    urls = [f"{BASE}/s-{slug}/c{cat_id}"]
    for p in range(2, max(1, pages) + 1):
        urls.append(f"{BASE}/s-{slug}/seite:{p}/c{cat_id}")
    return urls


def keyword_urls(keyword, pages=1):
    slug = re.sub(r"[^a-z0-9äöüß]+", "-", keyword.lower()).strip("-") or "suche"
    urls = [f"{BASE}/s-{urllib.parse.quote(slug)}/k0"]
    for p in range(2, max(1, pages) + 1):
        urls.append(f"{BASE}/s-{urllib.parse.quote(slug)}/seite:{p}/k0")
    return urls


# ----------------------------- Категории -----------------------------

TOP_IDS = {210, 195, 80, 153, 161, 130, 17, 102, 185, 73, 231, 297, 272, 235, 400}
CAT_LINK_RE = re.compile(r'href="/s-([a-z0-9-]+)/c(\d+)"[^>]*>([^<]+)<')


def parse_categories_from_home(html_text):
    out, seen, cur = [], set(), None
    for m in CAT_LINK_RE.finditer(html_text):
        slug, cid, name = m.group(1), int(m.group(2)), H.unescape(m.group(3)).strip()
        if cid in seen:
            continue
        seen.add(cid)
        if cid in TOP_IDS:
            cur = cid
            out.append({"id": cid, "slug": slug, "name": name, "parent": None})
        else:
            out.append({"id": cid, "slug": slug, "name": name, "parent": cur})
    return out


def load_fallback_categories(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


import urllib.parse  # noqa: E402  (используется в keyword_urls)
