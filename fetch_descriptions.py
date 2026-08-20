"""Fetch OLX listing descriptions for all candidates, filter by condition."""
import asyncio, json, re, sqlite3
from pathlib import Path
import httpx
from bs4 import BeautifulSoup

DB = Path(__file__).parent / "cameras.db"
CANDIDATES = Path(__file__).parent / "candidates.jsonl"
OUT = Path(__file__).parent / "conditions.jsonl"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9",
}

BAD = re.compile(
    r"(nie\s+sprawny|nie\s+dzia[łl]a|uszkodzon|do\s+naprawy|na\s+cz[eę][śs]ci|"
    r"nie\s+w[łl][aą]cza|nie\s+w[łl][aą]cz\b|wla[łl]\s+si[eę]|wla[łl]a\s+si[eę]|"
    r"zalany|zalana|grzyb|nie\s+testowa[łl]|nie\s+testow[aą]\b|nie\s+sprawdza[łl]|"
    r"nie\s+wiem\s+czy\s+dzia[łl]|mo[żz]liwa\s+usterka|wymaga\s+naprawy|"
    r"nie\s+reaguje|nie\s+w[łl][aą]cza\s+si[eę]|brak\s+reakcji)",
    re.I,
)
GOOD = re.compile(
    r"(sprawny\s+100|sprawna\s+100|w\s+pe[łl]ni\s+sprawny|przetestowany|"
    r"dzia[łl]a\s+prawid[łl]owo|bez\s+zastrze[żz]e[ńn]|idealny\s+stan|"
    r"[śs]wietny\s+stan|bardzo\s+dobry\s+stan)",
    re.I,
)

CONCURRENCY = 20


async def fetch_one(client: httpx.AsyncClient, item: dict) -> dict:
    url = item["url"]
    try:
        r = await client.get(url, timeout=12)
        soup = BeautifulSoup(r.text, "html.parser")
        el = soup.find(attrs={"data-cy": "ad_description"}) or soup.find(id="textContent")
        desc = el.get_text(" ", strip=True) if el else ""
        expired = "oferta-wygasla" in str(r.url) or "Ogłoszenie wygasło" in r.text
        bad = bool(BAD.search(desc)) if desc else False
        good = bool(GOOD.search(desc)) if desc else False
        return {**item, "desc": desc[:600], "expired": expired, "bad": bad, "good": good}
    except Exception as e:
        return {**item, "desc": "", "expired": False, "bad": False, "good": False, "err": str(e)}


async def main() -> None:
    lines = CANDIDATES.read_text(encoding="utf-8").strip().split("\n")
    items = [json.loads(l) for l in lines]
    print(f"Fetching {len(items)} listings...")

    sem = asyncio.Semaphore(CONCURRENCY)
    results = []

    async def bounded(item: dict) -> dict:
        async with sem:
            return await fetch_one(client, item)

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        tasks = [bounded(item) for item in items]
        for i, coro in enumerate(asyncio.as_completed(tasks), 1):
            r = await coro
            results.append(r)
            if i % 50 == 0:
                print(f"  {i}/{len(items)}...")

    with open(OUT, "w", encoding="utf-8") as f:
        for r in sorted(results, key=lambda x: x["id"]):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    expired = sum(1 for r in results if r["expired"])
    bad = sum(1 for r in results if r["bad"] and not r["expired"])
    good = sum(1 for r in results if r["good"] and not r["bad"] and not r["expired"])
    no_desc = sum(1 for r in results if not r["desc"] and not r["expired"])
    ok = len(results) - expired - bad

    print(f"\nWyniki:")
    print(f"  Wygasłe:        {expired}")
    print(f"  Uszkodzone:     {bad}")
    print(f"  Brak opisu:     {no_desc}")
    print(f"  Potencjalnie OK:{ok}")
    print(f"  Wyraźnie dobre: {good}")
    print(f"\nZapisano → {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
