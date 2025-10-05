# scraper.py
"""
Generator statycznego RSS dla epiotrkow.pl hostowany na GitHub Pages.
Uruchamiany co godzinę przez GitHub Actions.

Funkcje:
- Zbiera linki ze wszystkich selektorów (pełna lista artykułów).
- Dodaje obrazek: <enclosure>, <media:content>, <media:thumbnail>.
- Dociąga z podstrony artykułu datę publikacji i LEAD (z kilku akapitów),
  a lead wstrzykuje do <description> razem z miniaturą <img>.
- Fallbacki na teasery ładowane JS-em: JSON-LD (Article/NewsArticle) i AMP.
"""

import re
import sys
import time
import json
import hashlib
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

SITE = "https://epiotrkow.pl"

# Strony: p1 = /news/, p2..p20 = /news/wydarzenia-pX (zwiększ zakres, jeśli chcesz)
SOURCE_URLS = [f"{SITE}/news/"] + [f"{SITE}/news/wydarzenia-p{i}" for i in range(2, 21)]

FEED_TITLE = "epiotrkow.pl"
FEED_LINK  = f"{SITE}/news/"
FEED_DESC  = "Automatyczny RSS z list newsów epiotrkow.pl."

# Selektory, z których zbieramy linki (agregujemy ze wszystkich)
ARTICLE_LINK_SELECTORS = [
    ".tn-img a[href^='/news/']",      # duży kafel
    ".bg-white a[href^='/news/']",    # kafelki z h5.tn-title
    "a[href^='/news/']"               # fallback
]

# tylko prawdziwe artykuły (slug,ID)
ID_LINK = re.compile(r"^/news/.+,\d+$")

HEADERS = {"User-Agent": "Mozilla/5.0 (+https://github.com/) RSS static builder"}

# ile pozycji w RSS
MAX_ITEMS = 500

# ile artykułów wzbogacać o datę/lead (żeby workflow nie przekraczał limitów czasu)
DETAIL_LIMIT = 500

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

def build_lead_from_paras(soup: BeautifulSoup, max_chars: int = 800) -> str | None:
    """Zbuduj lead z kilku pierwszych akapitów (do max_chars), przytnij na granicy wyrazu."""
    paras = soup.select(
        ".news-content p, .article-content p, .entry-content p, "
        "article .content p, article p, .post-content p, .content p"
    )
    chunks, total = [], 0
    for p in paras:
        t = p.get_text(" ", strip=True)
        if not t or len(t) < 30:
            continue  # pomiń bardzo krótkie/ozdobne akapity
        chunks.append(t)
        total += len(t) + 1
        if total >= max_chars:
            break
    if not chunks:
        return None
    lead = " ".join(chunks)
    if len(lead) > max_chars:
        cut = lead[:max_chars]
        cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
        lead = cut.rstrip() + "…"
    return lead

def extract_from_jsonld(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """Spróbuj wyciągnąć datę i opis/treść z JSON-LD (Article/NewsArticle)."""
    pub_rfc, lead = None, None
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or (tag.contents[0] if tag.contents else "")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            typ = obj.get("@type") or obj.get("type")
            if isinstance(typ, list):
                typ = next((t for t in typ if isinstance(t, str)), None)
            if not typ or not isinstance(typ, str):
                continue
            if "Article" not in typ and "NewsArticle" not in typ:
                continue

            # data publikacji
            dp = obj.get("datePublished") or obj.get("dateCreated")
            if dp and not pub_rfc:
                try:
                    if dp.endswith("Z"):
                        dt = datetime.fromisoformat(dp.replace("Z", "+00:00"))
                        pub_rfc = to_rfc2822(dt.astimezone(tz=None).replace(tzinfo=None))
                    else:
                        dt = datetime.fromisoformat(dp)
                        pub_rfc = to_rfc2822(dt.replace(tzinfo=None))
                except Exception:
                    pass

            # opis / treść
            desc = obj.get("description")
            body = obj.get("articleBody")
            txt = (body or desc)
            if txt and not lead:
                lead = " ".join(str(txt).split())

        if pub_rfc or lead:
            break
    return pub_rfc, lead

def fetch_article_details(url: str) -> tuple[str | None, str | None]:
    """Zwraca (pubDate_rfc2822, lead_txt) z podstrony artykułu,
    próbując: JSON-LD → AMP → klasyczny HTML.
    """
    def _get(url_):
        r = requests.get(url_, headers=HEADERS, timeout=25)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")

    pub_rfc, lead = None, None

    # 0) Strona podstawowa
    try:
        soup = _get(url)
    except Exception as e:
        print(f"[WARN] Nie udało się pobrać artykułu: {url} -> {e}", file=sys.stderr)
        return None, None

    # 1) JSON-LD (najpierw)
    j_pub, j_lead = extract_from_jsonld(soup)
    if j_pub:
        pub_rfc = j_pub
    if j_lead and len(j_lead) > 60:
        lead = j_lead

    # 2) AMP (jeśli wciąż brakuje dobrego leada lub daty)
    if not pub_rfc or not lead:
        amp = soup.find("link", rel=lambda v: v and "amphtml" in v.lower())
        if amp and amp.get("href"):
            try:
                amp_url = urljoin(url, amp["href"])
                amp_soup = _get(amp_url)
                if not pub_rfc:
                    a_pub, _ = extract_from_jsonld(amp_soup)
                    if a_pub:
                        pub_rfc = a_pub
                if not lead:
                    a_lead = build_lead_from_paras(amp_soup, max_chars=800)
                    if a_lead and len(a_lead) > 60:
                        lead = a_lead
            except Exception as e:
                print(f"[WARN] AMP fetch failed: {amp_url} -> {e}", file=sys.stderr)

    # 3) Klasyczny HTML – meta + akapity (fallback)
    if not pub_rfc:
        meta = soup.find("meta", attrs={"property": "article:published_time"}) \
            or soup.find("meta", attrs={"name": "article:published_time"}) \
            or soup.find("meta", attrs={"itemprop": "datePublished"}) \
            or soup.find("meta", attrs={"name": "date"})
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
                pass
        if not pub_rfc:
            date_el = soup.select_one(".news-date") or soup.find("time")
            if date_el:
                pub_rfc = parse_polish_date(date_el.get_text(" ", strip=True))

    if not lead:
        lead = build_lead_from_paras(soup, max_chars=800)
        if not lead:
            md = soup.find("meta", attrs={"name": "description"})
            if md and md.get("content"):
                lead = md["content"].strip()

    # lekkie czyszczenie (uniknij ucięcia w pół zdania/wyrazu)
    if lead:
        lead = " ".join(lead.split())
        if len(lead) < 80 and not re.search(r"[.!?…]$", lead):
            # bardzo krótki teaser bez interpunkcji na końcu – odrzuć
            lead = None

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
