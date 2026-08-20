"""
Generate top 10 ranking report from scored listings.

Usage:
    uv run python report.py              # → top10.md + top10.json + manual_review.md
    uv run python report.py --top 20     # show top 20
"""
import argparse
import json
from pathlib import Path

from db import connect


def generate_reports(top_n: int = 10) -> None:
    conn = connect()

    # Top recommended listings
    top_rows = conn.execute(
        """
        SELECT
            l.title, l.price_pln, l.url,
            s.overall_score, s.reasoning, s.skip_reason,
            s.metering_upgrade, s.optics_upgrade, s.is_point_and_shoot,
            s.vibe_90s, s.condition_ok, s.model_used,
            te.canonical_name, te.condition AS text_condition,
            vo.condition AS vision_condition, vo.defects, vo.images_used,
            ms.focal_mm, ms.aperture_max, ms.metering_type, ms.focus_type,
            ms.specs_source
        FROM listings l
        JOIN scores s ON l.id = s.listing_id
        LEFT JOIN text_extractions te ON l.id = te.listing_id
        LEFT JOIN vision_obs vo ON l.id = vo.listing_id
        LEFT JOIN model_specs ms ON te.canonical_name = ms.canonical_name
        WHERE s.recommended = 1
        ORDER BY s.overall_score DESC
        LIMIT ?
        """,
        (top_n,),
    ).fetchall()

    # Manual review (no data / skip)
    manual_rows = conn.execute(
        """
        SELECT l.title, l.price_pln, l.url, s.skip_reason,
               te.canonical_name
        FROM listings l
        JOIN scores s ON l.id = s.listing_id
        LEFT JOIN text_extractions te ON l.id = te.listing_id
        WHERE s.skip_reason = 'brak_danych'
        ORDER BY l.price_pln ASC
        LIMIT 50
        """
    ).fetchall()

    # Stats
    total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    scored = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    recommended = conn.execute("SELECT COUNT(*) FROM scores WHERE recommended=1").fetchone()[0]

    conn.close()

    # --- top10.md ---
    md_lines = [
        "# Top aparatów analogowych — zamiennik Premier PC-656",
        "",
        f"Budżet: 20–120 PLN | Kryterium: lepsze naświetlanie, 90s point-and-shoot vibe",
        f"Przeanalizowano: {scored}/{total} ogłoszeń | Rekomendowanych: {recommended}",
        "",
        "---",
        "",
    ]

    for rank, row in enumerate(top_rows, 1):
        defects = json.loads(row["defects"] or "[]") if row["defects"] else []
        condition = row["vision_condition"] or row["text_condition"] or "nieznany"
        upgrades = []
        if row["metering_upgrade"]:
            upgrades.append("lepsze naświetlanie")
        if row["optics_upgrade"]:
            upgrades.append("lepsza optyka")

        md_lines += [
            f"## #{rank} — {row['title']}",
            f"**Cena:** {row['price_pln']} PLN | **Ocena:** {row['overall_score']:.1f}/10",
            f"**Model:** {row['canonical_name'] or 'nieznany'}",
            f"**Stan:** {condition}" + (f" | Defekty: {', '.join(defects)}" if defects else ""),
            f"**Przewagi vs PC-656:** {', '.join(upgrades) or 'ogólnie lepszy'}",
            f"**Uzasadnienie:** {row['reasoning']}",
            f"**Link:** {row['url']}",
            "",
            "---",
            "",
        ]

    Path("top10.md").write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Written: top10.md ({len(top_rows)} listings)")

    # --- top10.json ---
    json_data = [
        {
            "rank": rank,
            "title": row["title"],
            "price_pln": row["price_pln"],
            "url": row["url"],
            "overall_score": row["overall_score"],
            "canonical_name": row["canonical_name"],
            "condition": row["vision_condition"] or row["text_condition"],
            "defects": json.loads(row["defects"] or "[]") if row["defects"] else [],
            "specs": {
                "focal_mm": row["focal_mm"],
                "aperture_max": row["aperture_max"],
                "metering_type": row["metering_type"],
                "focus_type": row["focus_type"],
                "specs_source": row["specs_source"],
            },
            "upgrades": {
                "metering": bool(row["metering_upgrade"]),
                "optics": bool(row["optics_upgrade"]),
            },
            "reasoning": row["reasoning"],
            "model_used": row["model_used"],
        }
        for rank, row in enumerate(top_rows, 1)
    ]
    Path("top10.json").write_text(
        json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Written: top10.json")

    # --- manual_review.md ---
    if manual_rows:
        mr_lines = [
            "# Ogłoszenia wymagające ręcznego przeglądu",
            "(brak wystarczających danych do automatycznej oceny)",
            "",
        ]
        for row in manual_rows:
            mr_lines.append(
                f"- [{row['title']}]({row['url']}) — "
                f"{row['price_pln']} PLN | model: {row['canonical_name'] or 'nieznany'}"
            )
        Path("manual_review.md").write_text("\n".join(mr_lines), encoding="utf-8")
        print(f"Written: manual_review.md ({len(manual_rows)} listings)")

    print(f"\nStats: {scored}/{total} scored, {recommended} recommended, {len(top_rows)} in top{top_n}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    generate_reports(args.top)


if __name__ == "__main__":
    main()
