# epiotrkow-rss (fixed)

Statyczny kanał RSS generowany ze stron listowych epiotrkow.pl:
- P1 = `/news/`
- P2..P9 = `/news/wydarzenia-pX`

Tytuły pobierane z `.tn-title`, linki tylko w formacie `/news/<slug>,<ID>`.

## Użycie
1. Workflow `.github/workflows/rss.yml` uruchamia `scraper.py` co 1 godzinę (UTC).
2. W repo pojawia się `feed.xml`.
3. Włącz GitHub Pages → link do RSS:
   `https://<twoj-login>.github.io/epiotrkow-rss/feed.xml`

## Dostosowanie
- Jeśli portal zmieni strukturę:
  - zaktualizuj `SOURCE_URLS`,
  - dopasuj `ARTICLE_LINK_SELECTORS` lub regex `ID_LINK`,
  - ew. rozszerz pobieranie tytułu (np. inna klasa).

## Zależności
`requests`, `beautifulsoup4`, `lxml`
