"""
Export all analyzed listings to JSONL for re-processing with other models.

Usage:
    uv run python export.py                      # → listings_full.jsonl
    uv run python export.py --out my_export.jsonl
"""
import argparse
import json
import sqlite3
from pathlib import Path

from db import DB_PATH, connect

PC656_BASELINE = {
    "canonical_name": "Premier PC-656",
    "focal_mm": 38,
    "aperture_max": 4.5,
    "metering_type": "center",
    "focus_type": "fixed",
    "is_slr": False,
    "notes": "Zawodny pomiar centralny, stały fokus, słaba jakość optyki",
}


def export_jsonl(out_path: str) -> int:
    conn = connect()

    rows = conn.execute("""
        SELECT
            l.id, l.url, l.title, l.price_pln, l.location, l.date_posted,
            ld.full_description, ld.all_image_urls,
            te.brand, te.model, te.canonical_name,
            te.condition AS text_condition,
            te.condition_confidence, te.model_confidence,
            vo.condition AS vision_condition, vo.defects, vo.images_used,
            vo.visible_model_text,
            ms.focal_mm, ms.aperture_max, ms.metering_type, ms.focus_type, ms.is_slr,
            ms.specs_source,
            s.overall_score, s.recommended, s.reasoning, s.skip_reason, s.model_used
        FROM listings l
        LEFT JOIN listing_details ld ON l.id = ld.listing_id
        LEFT JOIN text_extractions te ON l.id = te.listing_id
        LEFT JOIN vision_obs vo ON l.id = vo.listing_id
        LEFT JOIN model_specs ms ON te.canonical_name = ms.canonical_name
        LEFT JOIN scores s ON l.id = s.listing_id
        ORDER BY l.id
    """).fetchall()

    count = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for row in rows:
            record = {
                "listing": {
                    "id": row["id"],
                    "url": row["url"],
                    "title": row["title"],
                    "price_pln": row["price_pln"],
                    "location": row["location"],
                    "date_posted": row["date_posted"],
                    "full_description": row["full_description"],
                    "all_image_urls": json.loads(row["all_image_urls"] or "[]"),
                },
                "text_extraction": {
                    "brand": row["brand"],
                    "model": row["model"],
                    "canonical_name": row["canonical_name"],
                    "condition": row["text_condition"],
                    "condition_confidence": row["condition_confidence"],
                    "model_confidence": row["model_confidence"],
                },
                "vision_obs": {
                    "condition": row["vision_condition"],
                    "defects": json.loads(row["defects"] or "[]"),
                    "images_used": row["images_used"],
                    "visible_model_text": row["visible_model_text"],
                } if row["vision_condition"] else None,
                "model_specs": {
                    "canonical_name": row["canonical_name"],
                    "focal_mm": row["focal_mm"],
                    "aperture_max": row["aperture_max"],
                    "metering_type": row["metering_type"],
                    "focus_type": row["focus_type"],
                    "is_slr": bool(row["is_slr"]),
                    "specs_source": row["specs_source"],
                } if row["focal_mm"] or row["aperture_max"] else None,
                "score": {
                    "overall_score": row["overall_score"],
                    "recommended": bool(row["recommended"]),
                    "reasoning": row["reasoning"],
                    "skip_reason": row["skip_reason"],
                    "model_used": row["model_used"],
                } if row["overall_score"] is not None else None,
                "pc656_baseline": PC656_BASELINE,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1

    conn.close()
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="listings_full.jsonl")
    args = parser.parse_args()

    print(f"Exporting to {args.out}...")
    count = export_jsonl(args.out)
    size_mb = Path(args.out).stat().st_size / 1024 / 1024
    print(f"Done: {count} records, {size_mb:.1f} MB")
    print(f"\nRe-score example:")
    print(f"  python analyze.py --rescore --model gpt-5.6")


if __name__ == "__main__":
    main()
