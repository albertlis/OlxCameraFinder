import json, sqlite3
from pathlib import Path

conn = sqlite3.connect(Path(__file__).parent / "cameras.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT l.id, l.title, l.price_pln, l.url, l.image_urls,
           ld.full_description
    FROM listings l
    LEFT JOIN listing_details ld ON l.id = ld.listing_id
    ORDER BY l.id
""").fetchall()
with open(Path(__file__).parent / "listings_dump.jsonl", "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
print(f"Dumped {len(rows)} listings -> listings_dump.jsonl")
