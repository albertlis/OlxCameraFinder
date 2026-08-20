"""Pass 1: odrzuca oczywiste nie-kandydaty. Kryterium: gorsze LUB zupełnie inne niż PC-656."""
import json, re
from pathlib import Path

REJECT_TITLE = re.compile(
    r"\b(zenit|praktica\s+mtl|praktica\s+b\d|canon\s+ae-1|canon\s+f-?1\b|pentax\s+k\d|"
    r"pentax\s+mx|pentax\s+me|lustrzank|slr|fotonex|ix240|aps\s+film|"
    r"rolleiflex|hasselblad|mamiya|yashica\s+mat|twin.?lens|tlr|"
    # APS models
    r"ixus|nuvis|advantix|revio|pronea|nexus\s+ix|vectis|"
    # SLR bodies
    r"eos\s+\d+|dynax|om-?\d{2,}|"
    # Bridge/superzoom
    r"is-?200|is-?300|is-?3000|superzoom|"
    # Wrong formats
    r"pocket\s+110|tele\s+110|\b110\b.*cf|agfamatic.*pocket|porst.*126|126\s+sport|"
    # Non-cameras
    r"telefon|telefonicz|lampa\s+b[łl]yskow|aparat\s+telefoniczn)\b",
    re.IGNORECASE,
)
REJECT_DESC = re.compile(
    r"(nie\s+dzia[łl]a|grzyb\s+na\s+obiektywie|uszkodzony\s+obiektyw|"
    r"p[eę]kni[eę]ta\s+soczewka|do\s+naprawy|niesprawny)",
    re.IGNORECASE,
)

kept, rejected = [], []
reasons: dict[str, int] = {}

with open(Path(__file__).parent / "listings_dump.jsonl", encoding="utf-8") as f:
    for line in f:
        r = json.loads(line)
        title = r.get("title") or ""
        price = r.get("price_pln") or 0
        desc = r.get("full_description") or ""

        reason = None
        if price > 120:
            reason = f"cena>{price}"
        elif REJECT_TITLE.search(title):
            reason = "tytu_slr_aps"
        elif desc and REJECT_DESC.search(desc):
            reason = "opis_uszkodzony"

        if reason:
            reasons[reason[:20]] = reasons.get(reason[:20], 0) + 1
            rejected.append({**r, "_reject_reason": reason})
        else:
            kept.append(r)

out_candidates = Path(__file__).parent / "candidates.jsonl"
out_rejected = Path(__file__).parent / "rejected.jsonl"

with open(out_candidates, "w", encoding="utf-8") as f:
    for r in kept:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

with open(out_rejected, "w", encoding="utf-8") as f:
    for r in rejected:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

print(f"Kept:     {len(kept)}")
print(f"Rejected: {len(rejected)}")
print("Rejection breakdown:")
for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")
