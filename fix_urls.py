"""Replace all hallucinated OLX URLs in top10.md with correct ones from DB."""
import sqlite3, re
from pathlib import Path

conn = sqlite3.connect("cameras.db")
ids = [520,412,700,307,322,291,427,75,51,652,345,641,284,335,579,30,116,88,
       235,300,253,566,118,587,269,498,311,458,456,264,686,226,543,466,258,
       393,355,706,690,761,600]

db_urls = {}
for row in conn.execute(f"SELECT id, url FROM listings WHERE id IN ({','.join(map(str,ids))})"):
    db_urls[row[0]] = row[1].split("?")[0]

# Map: fragment that appears in wrong URL -> correct URL from DB
# We identify by the slug part (before CID99)
replacements = {
    # Tier 1
    "braun-bravo-m5-af-CID99":           db_urls[520],
    "pentax-zoom-105-super-kompaktowy":   db_urls[412],
    "samsung-slim-zoom-130s-etui":        db_urls[700],
    "tcm-af-35-70mm-auto-focus-CID99":    db_urls[307],
    "tcm-autofocus-34mm-gwarancja-CID99": db_urls[322],
    "objecta-zoom-1100-af":               db_urls[291],
    "exakta-dx-34mm-f3-5-CID99":          db_urls[427],
    "aparat-fotograficzny-analogowy-CID99-ID15bnU2": db_urls[75],
    "aparat-analogowy-samsung-af-300-CID99": db_urls[51],
    "aparat-analogowy-panasoni-CID99":    db_urls[652],
    "kodak-cameo-auto-focus-panoramic":   db_urls[641],
    "kodak-s-series-s900-tele":           db_urls[345],
    "chinon-pocket-zoom-bateria":         db_urls[284],
    "aparat-praktica-sk-5600-auto-focus": db_urls[335],
    "aparat-kompaktowy-minolta-riva-100-af": db_urls[579],
    # Tier 2
    "aparat-analogowy-braun-trend-af-ii-motor": db_urls[30],
    "aparat-analogowy-premier-bf300":     db_urls[116],
    "carena-af-zoom-70-aparat-analogowy": db_urls[88],
    "aparat-analogowy-carena-af-38mm-motor": db_urls[235],
    "aparat-analogowy-carena-super-mini-CID99-ID17OsPK": db_urls[300],
    "kompaktowy-aparat-analogowy-carena-motor-advance": db_urls[253],
    "wyjatkowy-analogowy-kompakt-canon-prima-4": db_urls[566],
    "aparat-fotograficzny-analogowy-traveler-af-mini": db_urls[118],
    "aparat-analogowy-na-klisze-minolta-riva-zoom-70ex": db_urls[587],
    "analogowy-aparat-fotograficzny-praktica-bf-md": db_urls[269],
    "aparat-analogowy-praktica-450af":    db_urls[498],
    "analogowy-aparat-fotograficzny-exacta-800-af": db_urls[311],
    "aparat-analogowy-exakta-macro-70-af": db_urls[458],
    "aparat-maginon-lucky-shot-motor-dx": db_urls[456],
    "aparat-fotograficzny-analogowy-panasonic-c-225ef": db_urls[264],
    "aparat-konica-pop-af":               db_urls[686],
    "analogowy-aparat-fotograficzny-fuji-dl-25-CID99-IDTz3DR": db_urls[226],
    "aparat-analogowy-kodak-vr35":        db_urls[543],
    "aparat-analogowy-carena-50-mf":      db_urls[466],
    "aparat-fotograficzny-analogowy-concord-CID99-ID16jJAT": db_urls[258],
    # Vintage
    "aparat-analogowy-certo-kn-35":       db_urls[393],
    "aparat-fotograficzny-analogowy-minolta-f-CID99": db_urls[355],
    "agfa-optima-ii-s-prontormator":      db_urls[706],
}

txt = Path("top10.md").read_text(encoding="utf-8")
changed = 0
for slug, correct_url in replacements.items():
    # Find any line containing this slug and replace the whole URL
    pattern = rf'https://www\.olx\.pl/d/oferta/{slug}[^\s]*'
    new_txt = re.sub(pattern, correct_url, txt)
    if new_txt != txt:
        changed += 1
        txt = new_txt

# Also fix Minolta Hi-Matic F — this was a hallucination, ID=355 is actually Minolta F10BF
# Remove that entry entirely or flag it
txt = txt.replace(
    "### Minolta Hi-Matic F | 65 PLN | f/2.7 rangefinder",
    "### Minolta F10BF | 65 PLN | ⚠️ specs nieznane (agent pomylił model)"
)

Path("top10.md").write_text(txt, encoding="utf-8")
print(f"Fixed {changed} URLs")

# Verify first 2
lines = [l for l in txt.split('\n') if 'olx.pl' in l][:5]
for l in lines:
    print(l)
