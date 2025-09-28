# scraper.py
"""
Generator statycznego RSS dla epiotrkow.pl hostowany na GitHub Pages.
Uruchamiany co godzinę przez GitHub Actions.

Dopasowany do realnego HTML (wrzesień 2025):
- P1 = /news/ (nie /wydarzenia-p1)
- P2..P9 = /news/wydarzenia-pX
- Linki do artykułów mają postać /news/<slug>,<ID>
- Tytuły w elementach z klasą .tn-title (czasem <span>, czasem <h5>)
"""

import re
import sys
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

SITE = "https://epiotrkow.pl"

# p1 to /news/, p2..p9 to /news/wydarzenia-pX
SOURCE_URLS = [f"{SITE}/news/"] + [f"{SITE}/news/wydarzenia-p{i}" for i in range(2, 10)]

FEED_TITLE = "epiotrkow.pl – Wydarzenia (p1–p9)"
FEED_LINK  = f"{SITE}/news/"
FEED_DESC  = "Automatyczny RSS z list newsów epiotrkow.pl (wydarzenia p1–p9)."

# Preferowane selektory w kolejności prób
ARTICLE_LINK_SELECTORS = [
    ".tn-img a[href^='/news/']",      # duży kafel z góry listy
    ".bg-white a[href^='/news/']",    # kafelki z h5.tn-title
    "a[href^='/news/']",              # fallback (odfiltrujemy kategorie regexem)
]

# tylko rzeczywiste artykuły, które mają ID po przecinku, np. /news/slug,59675
ID_LINK = re.compile(r"^/news/.+,\d+$")

HEADERS = {"User-Agent": "Mozilla/5.0 (+https://github.com/) RSS static builder"}
MAX_ITEMS = 255

def fetch_items():
    items = []
    for url in SOURCE_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
        except Exception as e:
            print(f"[WARN] Nie udało się pobrać: {url} -> {e}", file=sys.stderr)
            continue

        soup = BeautifulSoup(r.text, "lxml")

        anchors = []
        for sel in ARTICLE_LINK_SELECTORS:
            anchors = soup.select(sel)
            if anchors:
                break

        if not anchors:
            print(f"[WARN] Brak dopasowań selektorów na: {url}", file=sys.stderr)
            continue

        for a in anchors:
            href = a.get("href")
            if not href:
                continue

            # filtr: tylko artykuły z ID po przecinku
            if not ID_LINK.match(href):
                continue

            link = urljoin(SITE, href)

            # tytuł – najpierw .tn-title w obrębie <a>
            title_el = a.select_one(".tn-title")
            if title_el:
                title = title_el.get_text(" ", strip=True)
            else:
                # czasem tytuł w <h5 class="tn-title"> będącym dzieckiem <a>
                h5 = a.select_one("h5.tn-title")
                title = h5.get_text(" ", strip=True) if h5 else ""

            if not title:
                # awaryjnie: cokolwiek tekstowego w <a>
                title = a.get_text(" ", strip=True)

            if not title:
                # jeszcze jedna próba: najbliższy element z klasą tn-title
                sibling_title = a.find_next(class_="tn-title")
                if sibling_title:
                    title = sibling_title.get_text(" ", strip=True)

            if not title:
                # ostatecznie: alt obrazka
                img = a.find("img")
                if img and img.get("alt"):
                    title = img.get("alt").strip()

            if not title:
                title = "Bez tytułu"

            guid = hashlib.sha1(link.encode("utf-8")).hexdigest()
            items.append({"title": title, "link": link, "guid": guid})

    # deduplikacja po linku z zachowaniem kolejności
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
<rss version="2.0">
<channel>
<title>{FEED_TITLE}</title>
<link>{FEED_LINK}</link>
<description>{FEED_DESC}</description>
<lastBuildDate>{pubdate}</lastBuildDate>
<ttl>60</ttl>
"""
    body = []
    for it in items:
        body.append(f"""
<item>
  <title><![CDATA[{it['title']}]]></title>
  <link>{it['link']}</link>
  <guid isPermaLink="false">{it['guid']}</guid>
  <pubDate>{pubdate}</pubDate>
  <description><![CDATA[{it['title']}]]></description>
</item>""")
    tail = "\n</channel>\n</rss>\n"
    return head + "".join(body) + tail

if __name__ == "__main__":
    items = fetch_items()
    rss = build_rss(items)
    with open("feed.xml", "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"Generated feed.xml with {len(items)} items")
