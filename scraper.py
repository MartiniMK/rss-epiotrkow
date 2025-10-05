# scraper.py
"""
Generator statycznego RSS dla epiotrkow.pl hostowany na GitHub Pages.
Uruchamiany co godzinę przez GitHub Actions.

Wersja z obrazkami + datą publikacji + wstępniakiem (lead) + miniaturą w <description>:
- zbieramy linki ze wszystkich selektorów (pełna lista artykułów),
- wyciągamy URL obrazka (data-src/src) i dodajemy:
  * <enclosure> (RSS 2.0),
  * <media:content> oraz <media:thumbnail> (MRSS),
  * <img> w treści <description> (HTML).
- dla pierwszych DETAIL_LIMIT artykułów pobieramy stronę artykułu i
  wydobywamy datę publikacji oraz pierwszy akapit (lead).
"""

import re
import sys
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime

SITE = "https://epiotrkow.pl"

# Strony: p1 = /news/, p2..p20 = /news/wydarzenia-pX
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

# ILE pozycji zwraca feed
MAX_ITEMS = 500

# ILE artykułów wzbogacać o datę/lead (żeby workflow nie przekraczał limitów czasu)
DETAIL_LIMIT = 150

def guess_mime(url: str) -> str:
    if not url:
        return "image/*"
    u = url.lower()
    if u.endswith(".webp"):  return "image/webp"
    if u.endswith(".png"):   return "image/png"
    if u.endswith(".jpg") or u.endswith(".jpeg"): return "image/jpeg"
    return "image/*"

def find_image_url(a: BeautifulSoup, site_base: str) -> str | None:
    """Znajdź obrazek powiązany z kafelkiem."""
    # 1) w tym samym <a>
    img = a.find("img")
    if img:
        src = img.get("data-src") or img.get("src")
        if src and not src.startswith("data:"):
            return urljoin(site_base, src)

    # 2) do góry maks. 4 poziomy i szukaj <img> wewnątrz kontenera
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

    # 3) fallback: najbliższy <img> po tym węźle
    sib_img = a.find_next("img")
    if sib_img:
        src = sib_img.get("data-src") or sib_img.get("src")
        if src and not src.startswith("data:"):
            return urljoin(site_base, src)
    return None

# Miesiące PL (do parsowania daty tekstowej)
PL_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
    "pazdziernika": 10, "listopada": 11, "grudnia": 12
}

def to_rfc2822(dt: datetime) -> str:
    # Format RFC 2822 w UTC (dla RSS OK)
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

def parse_polish_date(text: str) -> str | None:
    """np. 'niedz., 28 września 2025' → RFC 2822."""
    if not text:
        return None
    m = re.search(r"(\d{1,2})\s+([A-Za-ząćęłńóśźżĄĆĘŁŃÓŚŹŻ]+)\s+(\d{4})", text, re.IGNORECASE)
    if not m:
        return None
    day = int(m.group(1))
    month_name = m.group(2).lower()
    year = int(m.group(3))
    month = PL_MONTHS.get(month_name)
    if not month:
        return None
    try:
        dt = datetime(year, month, day, 12, 0, 0)  # godzina domyślna
        return to_rfc2822(dt)
    except Exception:
        return None

def fetch_article_details(url: str) -> tuple[str | None, str | None]:
    """Zwraca (pubDate_rfc2822, lead_txt) z podstrony artykułu."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
    except Exception as e:
        print(f"[WARN] Nie udało się pobrać artykułu: {url} -> {e}", file=sys.stderr)
        return None, None

    soup = BeautifulSoup(r.text, "lxml")

    # 1) DATA: meta/og
    meta = soup.find("meta", attrs={"property": "article:published_time"}) \
        or soup.find("meta", attrs={"name": "article:published_time"}) \
        or soup.find("meta", attrs={"itemprop": "datePublished"}) \
        or soup.find("meta", attrs={"name": "date"})
    pub_rfc = None
    if meta and meta.get("content"):
        iso = meta["content"].strip()
        try:
            if iso.endswith("Z"):
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                pub_rfc = to_rfc2822(dt.astimezone(tz=None).replace(tzinfo=None))
            else:
                dt = datetime.fromisoformat(iso)
                pub_rfc = to_rfc2822(dt.replace(tzinfo=None))
        except Exception:
            pub_rfc = None

    # 2) DATA: fallback z .news-date / <time>
    if not pub_rfc:
        date_el = soup.select_one(".news-date") or soup.find("time")
        if date_el:
            pub_rfc = parse_polish_date(date_el.get_text(" ", strip=True))

    # 3) LEAD: pierwszy sensowny akapit
    lead = None
    for sel in [".news-content p", ".article-content p", "article p", ".content p", ".post-content p", ".entry-content p"]:
        p = soup.select_one(sel)
        if p:
            txt = p.get_text(" ", strip=True)
            if txt and len(txt) > 40:
                lead = txt
                break
    if not lead:
        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            lead = md["content"].strip()

    return pub_rfc, lead

def fetch_items():
    items = []
    for url in SOURCE_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
        except Exception as e:
            print(f"[WARN] Nie udało się pobrać listy: {url} -> {e}", file=sys.stderr)
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
            items.append({
                "title": title, "link": link, "guid": guid,
                "image": img_url, "mime": mime
            })

    # deduplikacja po linku (zachowaj kolejność)
    seen, unique = set(), []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        unique.append(it)

    # dograj szczegóły (data/lead) dla pierwszych DETAIL_LIMIT
    for idx, it in enumerate(unique):
        if idx >= DETAIL_LIMIT:
            break
        pub, lead = fetch_article_details(it["link"])
        if pub:
            it["pubDate"] = pub
        if lead:
            it["lead"] = lead

    return unique[:MAX_ITEMS]

def rfc2822_now():
    return time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())

def build_rss(items):
    build_date = rfc2822_now()
    head = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:media="http://search.yahoo.com/mrss/">
<channel>
<title>{FEED_TITLE}</title>
<link>{FEED_LINK}</link>
<description>{FEED_DESC}</description>
<lastBuildDate>{build_date}</lastBuildDate>
<ttl>60</ttl>
"""
    body = []
    for it in items:
        # pubDate: z artykułu, a jeśli brak – data builda
        pubdate = it.get("pubDate", build_date)

        # description: miniatura + lead/tytuł
        lead_text = it.get("lead") or it["title"]
        img_html = ""
        if it.get("image"):
            # prosta miniatura w opisie (bez stylów – czytniki same dopasują)
            img_html = f'<p><img src="{it["image"]}" alt="miniatura"/></p>'
        desc_html = f"{img_html}<p>{lead_text}</p>"

        enclosure = ""
        media = ""
        media_thumb = ""
        if it.get("image"):
            enclosure   = f'\n  <enclosure url="{it["image"]}" type="{it.get("mime","image/*")}" />'
            media       = f'\n  <media:content url="{it["image"]}" medium="image" />'
            media_thumb = f'\n  <media:thumbnail url="{it["image"]}" />'

        body.append(f"""
<item>
  <title><![CDATA[{it['title']}]]></title>
  <link>{it['link']}</link>
  <guid isPermaLink="false">{it['guid']}</guid>
  <pubDate>{pubdate}</pubDate>
  <description><![CDATA[{desc_html}]]></description>{enclosure}{media}{media_thumb}
</item>""")
    tail = "\n</channel>\n</rss>\n"
    return head + "".join(body) + tail

if __name__ == "__main__":
    items = fetch_items()
    rss = build_rss(items)
    with open("feed.xml", "w", encoding="utf-8") as f:
        f.write(rss)
    print(f"Generated feed.xml with {len(items)} items (images + miniatures + dates/leads for first {min(len(items), DETAIL_LIMIT)} items)")
