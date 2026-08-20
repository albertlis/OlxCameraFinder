# OlxCameraFinder — Claude Code Context

## Cel projektu

Znalezienie wszystkich aparatów **zauważalnie lepszych** od Premier PC-656 (38mm f/4.5,
stały fokus, CdS metering, bez DX) spośród 764 ogłoszeń z OLX.pl.
Budżet 20–120 PLN. Film zawsze ISO 200. NIE SLR, NIE APS, NIE bridge.

## Stan projektu — ZAKOŃCZONY ✅

- [x] Scraping — `cameras.db` zawiera **764 ogłoszeń**
- [x] Filter pass 1 — regex odrzuca SLR/APS/Vectis/Ixus → **697 kandydatów**
- [x] Fetch descriptions — httpx pobiera opisy OLX, wykrywa uszkodzone → **conditions.jsonl**
- [x] Analiza roju agentów WebSearch — 78 batchy × 5 modeli → specs zweryfikowane online
- [x] Raport — `top10.md` zawiera **~35 modeli** w Tier 1 / Tier 2 / Vintage + lista odrzuconych
- [x] URL audit — wszystkie linki OLX pochdzą z `cameras.db` (nie halucynowane)

## Wynik

**`top10.md`** — lista wszystkich aparatów godnych zakupu z:
- ceną, stanem ogłoszenia, przysłoną, typem AF/FF, DX coding
- bezpośrednim linkiem OLX (ID z bazy, nie z palca)
- ostrzeżeniami ⚠️ dla niesprawdzonych / ekstrapolowanych specs

## Pipeline — jak odtworzyć od zera

```bash
# 1. Dump z DB
uv run python dump_listings.py          # → listings_dump.jsonl (764 wierszy)

# 2. Filtr regex (SLR/APS/Vectis/Ixus/focus-free)
uv run python filter_pass1.py           # → candidates.jsonl (697)

# 3. Pobierz opisy OLX + wykryj uszkodzone
uv run python fetch_descriptions.py     # → conditions.jsonl (697, ~20 req/s)

# 4. Rój agentów CC weryfikuje specs online (max 5 modeli / agent)
# → wynik ręcznie scalony do top10.md

# 5. Napraw halucynowane URL-e
uv run python fix_urls.py               # poprawia ID w top10.md z cameras.db
```

## Pliki

| Plik | Opis |
|------|------|
| `cameras.db` | SQLite — 764 ogłoszeń (url, title, price, image_urls) |
| `top10.md` | **WYNIK** — lista godnych aparatów z linkami OLX |
| `scrape_olx.py` | Playwright scraper (jednorazowy) |
| `dump_listings.py` | Dump cameras.db → listings_dump.jsonl |
| `filter_pass1.py` | Regex filtr SLR/APS/focus-free |
| `fetch_descriptions.py` | Async httpx — opisy OLX + detekcja uszkodzonych |
| `fix_urls.py` | Naprawa URL-i w top10.md z bazy DB |
| `analyze.py` | Stary pipeline vLLM (nieużywany — halucynował specs) |
| `report.py` | Generator raportu (stary, pod analyze.py) |
| `db.py` | Schema SQLite + connection helper |

## Kluczowe lekcje z projektu

- **vLLM/Qwen halucynował specs** (złe przysłony, zły format, zły AF) → przełączono na WebSearch
- **Agenci muszą czytać PEŁNY opis OLX** — pierwsze 300 znaków może ukryć "sok w obiektywie"
- **Nigdy nie pisać URL ręcznie** — ID OLX musi pochodzić z bazy (fix_urls.py pattern)
- **DX coding** ≠ lepszy pomiar; to tylko automatyczny odczyt ISO z kasety
- **CdS metering** degraduje się, SPD/SPC program AE jest nowocześniejszy

## Kontynuacja / aktualizacja listy

Jeśli pojawią się nowe ogłoszenia — uruchom ponownie scraper, potem pipeline od dump_listings.py.
Wyniki agentów scalić ręcznie z top10.md (dodaj nowe modele, usuń sprzedane).
