# OlxCameraFinder — Claude Code Context

## Cel projektu

Znalezienie top 10 zamienników dla aparatu Premier PC-656 (zły auto-ekspozycja, słaba
optyka) spośród 764 ogłoszeń z OLX.pl. Budżet 20–120 PLN. Kryterium: lepsze
naświetlanie + 90s point-and-shoot vibe, NIE profesjonalny/SLR.

## Stan obecny

- [x] Scraping zakończony — `cameras.db` zawiera **764 ogłoszeń** (tabela `listings`)
- [ ] Analiza LLM — uruchomić `analyze.py` na serwerze z vLLM
- [ ] Raport — uruchomić `report.py` po zakończeniu analizy

## Wymagania serwera

- vLLM z modelem **Qwen3.5 397B VL** (multimodal, vision-language)
- API: `http://localhost:8000/v1` (OpenAI-compatible)
- Min. 8×A100 80GB GPU

## Uruchomienie na serwerze

```bash
# 1. Setup środowiska
uv sync
uv run playwright install chromium

# 2. Test (5 ogłoszeń) — upewnij się że vLLM odpowiada
uv run python analyze.py --limit 5

# 3. Pełna analiza (może działać kilka dni — to normalne)
uv run python analyze.py

# 4. Jeśli przerwane — resume automatyczny (INSERT OR IGNORE)
uv run python analyze.py

# 5. Po zakończeniu — generuj raport
uv run python report.py

# 6. Export JSONL do re-scoringu innym modelem (opcjonalnie)
uv run python export.py
python analyze.py --rescore --model "inny-model"
```

## Architektura pipeline

```
listings (764 rows, scraped)
    ↓
analyze.py per listing:
  Etap 0: httpx → listing_details (pełny opis + wszystkie zdjęcia)
  Etap A: vLLM text call → text_extractions (model, marka, stan, confidence)
  Etap B: DuckDuckGo + httpx → model_specs (cache per unikalny model)
  Etap C: vLLM VL call (lazy, iteracyjny) → vision_obs_raw + vision_obs
  Etap D: vLLM scoring call → scores (overall_score, reasoning, recommended)
    ↓
report.py → top10.md + top10.json
```

## Pliki

| Plik | Opis |
|------|------|
| `cameras.db` | SQLite — scraped data + wyniki analizy |
| `scrape_olx.py` | Playwright scraper (już uruchomiony) |
| `analyze.py` | Główny pipeline LLM |
| `export.py` | Export JSONL do re-scoringu |
| `report.py` | Generuje top10.md |
| `db.py` | Schema SQLite + connection helper |

## Re-scoring z innym modelem

Wszystkie dane (opisy, specyfikacje, obserwacje wizualne) są zapisane w DB niezależnie
od scoringu. Można przemielić scoring z GPT-5.6, GLM5.2 lub Claude bez ponownego
scrapowania:

```bash
python analyze.py --rescore --model "nazwa-modelu"
```

## Konfiguracja

W `analyze.py` linia ~35:
```python
VLLM_BASE_URL = "http://localhost:8000/v1"  # zmień jeśli inny port
CONCURRENCY = 4  # liczba równoległych callów vLLM — zwiększ jeśli GPU ma rezerwę
```

## Kontynuacja sesji Claude Code

Jeśli chcesz kontynuować tę sesję na innej maszynie, powiedz Claude:

> "Kontynuuj projekt OlxCameraFinder. Scraping zakończony (764 ogłoszenia w cameras.db).
> Uruchom analyze.py na serwerze vLLM localhost:8000 z Qwen3.5 397B VL.
> Sprawdź czy vLLM działa, przetestuj na 5 ogłoszeniach, potem uruchom pełną analizę."
