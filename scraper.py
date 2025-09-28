# scraper.py
"""
Generator statycznego RSS dla epiotrkow.pl hostowany na GitHub Pages.
Uruchamiany co godzinę przez GitHub Actions.

Wersja z obrazkami:
- zbieramy linki ze wszystkich selektorów (pełna lista artykułów),
- wyciągamy URL obrazka (data-src/src) z kafelka i dodajemy do RSS
  jako <enclosure> i <media:content> (MRSS).
"""

import re
import sys
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

SITE = "https://epiotrkow.pl"

# Strony: p1 = /news/, p2..p20 = /news/wydarzenia-pX (zwiększ zakres jeśli chcesz)
SOURCE_URLS = [f"{SITE}/news/"] + [f"{SITE}/news/wydarzenia-p{i}" for i in range(2, 21)]

FEED_TITLE = "epiotrkow.pl – Wydarzenia (p1–p20)"
FEED_LINK  = f"{SITE}/news/"
FEED_DESC  = "Automatyczny RSS z list newsów epiotrkow.pl (wydarzenia p1–p20)."

# Selektory, z których zbieramy linki (agregujemy ze wszystkich)
ARTICLE_LINK_SELECTORS = [
    ".tn-img a[href^='/news/']",      # duży kafel
    ".bg-white a[href^='/news/']",    # kafelki z h5.tn-title
    "a[href^='/news/']"               # fallback
]

# tylko prawdziwe artykuły (slug,ID)
ID_LINK = re.compile(r"^/news/.+,\d+$")

HEADERS = {"User-Agent": "Mozilla/5.0 (+https://github.com/) RSS static builder"}

MAX_ITEMS = 500  # ile pozycji w RSS

def guess_mime(url: str) -> str:
    u = url.lower()
    if u.endswith(".webp"):
        return "image/webp"
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".jpg") or u.endswith(".jpeg"):
        return "image/jpeg"
    return "image/*"

def find_image_url(a: BeautifulSoup, site_base: str) -> str | None:
    """Spróbuj znaleźć obrazek związany z danym linkiem do artykułu.
    Priorytet: w tym samym <a> → wyżej w drzewie (do 4 poziomów) → najbliższy <img>.
    Zwraca absolutny URL albo None.
    """
    # 1) w tym samym <a>
    img = a.find("img")
    if img:
        src = img.get("data-src") or img.get("src")
        if src and not src.startswith("data:"):
            return urljoin(site_base, src)

    # 2) przejdź do góry maks. 4 poziomy i szukaj <img> wewnątrz kontenera
    parent = a
    for _ in range(4):
        parent = parent.parent
        if not parent:
            break
        img = parent.find("img")
        if img:
            src = img.get("data-src") or img.get("src")
            if src and not src.startswith("data:"):
                return urljoin(site_base, src)

    # 3) fallback: najbliższy <img> w dokumencie po tym węźle (ograniczone ryzyko reklam)
    sib_img = a.find_next("img")
    if sib_img:
        src = sib_img.get("data-src") or sib_img.get("src")
        if src and not src.startswith("data:"):
            return urljoin(site_base, src)

    return None

def fetch_items():
    items = []
    for url in SOURCE_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"[WARN] Nie udało się pobrać: {url} -> {e}", file=sys.stderr)
            continue

        soup = BeautifulSoup(r.text, "lxml")

        # zbierz ze wszystkich selektorów i deduplikuj po href
        anchors = []
        for sel in ARTICLE_LINK_SELECTORS:
            anchors.extend(soup.select(sel))

        seen_href = set()
        clean = []
        for a in anchors:
            href = a.get("href")
            if not href:
                continue
            if href in seen_href:
                continue
            seen_href.add(href)
            clean.append(a)

        for a in clean:
            href = a.get("href")
            if not ID_LINK.match(href):
                continue

            link = urljoin(SITE, href)

            # Tytuł — próby w kolejności
            title_el = a.select_one(".tn-title")
            if title_el:
                title = title_el.get_text(" ", strip=True)
            else:
                h5 = a.select_one("h5.tn-title")
                title = h5.get_text(" ", strip=True) if h5 else ""

            if not title:
                title = a.get_text(" ", strip=True)

            if not title:
                sibling = a.find_next(class_="tn-title")
                if sibling:
                    title = sibling.get_text(" ", strip=True)

            if not title:
                img_in_a = a.find("img")
                if img_in_a and img_in_a.get("alt"):
                    title = img_in_a["alt"].strip()

            if not title:
                title = "Bez tytułu"

            # Obrazek (opcjonalny)
            img_url = find_image_url(a, SITE)
            mime = guess_mime(img_url) if img_url else None

            guid = hashlib.sha1(link.encode("utf-8")).hexdigest()
            items.append({"title": title, "link": link, "guid": guid, "image": img_url, "mime": mime})

    # deduplikacja po linku
    seen, unique = set(), []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        unique.append(it)

    return unique[:MAX_ITEMS]

def rfc2822_now():
    return time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())

def build_rss(items):
    pubdate = rfc2822_now()
    head = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:media="http://search.yahoo.com/mrss/">
<channel>
<title>{FEED_TITLE}</title>
<link>{FEED_LINK}</link>
<description>{FEED_DESC}</description>
<lastBuildDate>{pubdate}</lastBuildDate>
<ttl>60</ttl>
"""
    body = []
    for it in items:
        enclosure = ""
        media = ""
        if it.get("image"):
            enclosure = f'\n  <enclosure url="{it["image"]}" type="{it.get("mime","image/*")}" />'
            media = f'\n  <media:content url="{it["image"]}" medium="image" />'

        body.append(f"""
<item>
  <title><![CDATA[{it['title']}]]></title>
  <link>{it['link']}</link>
  <guid isPermaLink="false">{it['guid']}</guid>
  <pubDate>{pubdate}</pubDate>
  <description><![CDATA[{it['title']}]]></description>{enclosure}{media}
</item>""")
    tail = "\n</channel>\n</rss>\n"
    return head + "".join(body) + tail

if __name__ == "__main__":
    items = fetch_items()
    rss = build_rss(items)
    with open("feed.xml", "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"Generated feed.xml with {len(items)} items (with images where available)")
