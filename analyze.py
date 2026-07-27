"""
OLX camera listing analyzer.

Usage:
    uv run python analyze.py                    # full pipeline
    uv run python analyze.py --limit 5          # test: first 5 listings
    uv run python analyze.py --rescore          # re-run scoring only (keep VL/specs)
    uv run python analyze.py --rescore --model gpt-5.6

Resume-safe: each stage uses INSERT OR IGNORE / INSERT OR REPLACE.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
from typing import Any, Optional, TypeVar

import httpx
from bs4 import BeautifulSoup
from openai import AsyncOpenAI
from pydantic import BaseModel
from typing import Literal

from db import DB_PATH, init_db, connect

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

VLLM_BASE_URL = "http://localhost:8000/v1"
CONCURRENCY = 4  # concurrent vLLM calls; tune per GPU headroom

PREMIER_PC656 = {
    "focal_mm": 38,
    "aperture_max": 4.5,
    "metering_type": "center",
    "focus_type": "fixed",
    "is_slr": False,
    "notes": "Zawodny pomiar centralny, stały fokus, słaba jakość optyki",
}

SYSTEM_SCORING = (
    "Jesteś ekspertem aparatów analogowych. Oceniasz czy ogłoszenie to dobry zamiennik "
    "dla Premier PC-656 (słaba ekspozycja, 38mm f/4.5, stały fokus, zawodny pomiar centralny). "
    "Użytkownik chce: nieco lepsze naświetlanie, 90s point-and-shoot vibe, "
    "NIE profesjonalny/SLR, budżet 20–120 PLN."
)

DDGO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
OLX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9",
}

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class TextExtraction(BaseModel):
    brand: Optional[str] = None
    model: Optional[str] = None
    canonical_name: Optional[str] = None  # e.g. "Olympus mju-1", key for model_specs
    condition: Literal["sprawny", "uszkodzony", "niepewny"] = "niepewny"
    condition_confidence: Literal["high", "low"] = "low"
    model_confidence: Literal["high", "low"] = "low"


class VisionObsRaw(BaseModel):
    visible_model_text: str = ""
    condition: Literal["sprawny", "uszkodzony", "niepewny"] = "niepewny"
    defects: list[
        Literal[
            "pekniecie_obudowy",
            "korozja",
            "brud_obiektyw",
            "grzyb_obiektyw",
            "urwany_uchwyt",
            "zarysowania_obiektyw",
            "brak_klapki_baterii",
            "inne",
        ]
    ] = []
    lens_visible: Literal["czyste", "brudne", "niewidoczne"] = "niewidoczne"
    needs_more_images: bool = False


class ListingScore(BaseModel):
    overall_score: float  # 1.0–10.0
    metering_upgrade: bool
    optics_upgrade: bool
    is_point_and_shoot: bool
    vibe_90s: bool
    condition_ok: bool
    recommended: bool
    reasoning: str
    skip_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# vLLM helpers
# ---------------------------------------------------------------------------


def _response_format(model_cls: type[BaseModel], name: str) -> Any:
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": model_cls.model_json_schema()},
    }


async def get_model_name(client: AsyncOpenAI) -> str:
    models = await client.models.list()
    return models.data[0].id


async def llm_text_call(
    client: AsyncOpenAI,
    model: str,
    system: str,
    user: str,
    response_model: type[T],
    schema_name: str,
) -> T:
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format=_response_format(response_model, schema_name),
        temperature=0,
        max_tokens=512,
    )
    return response_model.model_validate_json(resp.choices[0].message.content or "{}")  # type: ignore[return-value]


async def llm_vision_call(
    client: AsyncOpenAI,
    model: str,
    image_url: str,
    prompt: str,
    response_model: type[T],
    schema_name: str,
) -> T:
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        response_format=_response_format(response_model, schema_name),
        temperature=0,
        max_tokens=256,
    )
    return response_model.model_validate_json(resp.choices[0].message.content or "{}")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Stage 0: Fetch full OLX listing page
# ---------------------------------------------------------------------------


def _parse_olx_listing(html: str) -> tuple[str, list[str]]:
    """Returns (full_description, all_image_urls)."""
    soup = BeautifulSoup(html, "html.parser")

    # Description
    desc_el = (
        soup.find(attrs={"data-cy": "ad_description"})
        or soup.find(id="textContent")
        or soup.find("div", class_=re.compile(r"description", re.I))
    )
    description = desc_el.get_text("\n", strip=True) if desc_el else ""

    # Images — OLX embeds them in og:image meta or in photo gallery
    image_urls: list[str] = []
    for meta in soup.find_all("meta", property="og:image"):
        url = meta.get("content", "")
        if url:
            image_urls.append(url)

    if not image_urls:
        for img in soup.find_all("img", src=True):
            src = img["src"]
            if "olx" in src and not src.startswith("data:"):
                src = re.sub(r";s=\d+x\d+", "", src)
                image_urls.append(src)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_images = [u for u in image_urls if not (u in seen or seen.add(u))]  # type: ignore[func-returns-value]

    return description, unique_images


async def fetch_listing_details(
    listing_id: int, url: str, conn: sqlite3.Connection, lock: asyncio.Lock
) -> tuple[str, list[str]] | None:
    """Fetch + store full OLX listing. Returns (description, image_urls) or None on error."""
    # Check cache
    row = conn.execute(
        "SELECT full_description, all_image_urls FROM listing_details WHERE listing_id=?",
        (listing_id,),
    ).fetchone()
    if row:
        return row["full_description"], json.loads(row["all_image_urls"] or "[]")

    try:
        async with httpx.AsyncClient(headers=OLX_HEADERS, follow_redirects=True, timeout=20) as http:
            resp = await http.get(url)
            resp.raise_for_status()
    except Exception as e:
        print(f"  [L{listing_id}] fetch error: {e}")
        return None

    description, image_urls = _parse_olx_listing(resp.text)

    async with lock:
        conn.execute(
            "INSERT OR IGNORE INTO listing_details (listing_id, full_description, all_image_urls) "
            "VALUES (?, ?, ?)",
            (listing_id, description, json.dumps(image_urls)),
        )
        conn.commit()

    return description, image_urls


# ---------------------------------------------------------------------------
# Stage A: Text extraction
# ---------------------------------------------------------------------------


async def extract_text(
    listing_id: int,
    title: str,
    description: str,
    client: AsyncOpenAI,
    model: str,
    conn: sqlite3.Connection,
    lock: asyncio.Lock,
) -> TextExtraction:
    row = conn.execute(
        "SELECT * FROM text_extractions WHERE listing_id=?", (listing_id,)
    ).fetchone()
    if row:
        return TextExtraction(
            brand=row["brand"],
            model=row["model"],
            canonical_name=row["canonical_name"],
            condition=row["condition"],
            condition_confidence=row["condition_confidence"],
            model_confidence=row["model_confidence"],
        )

    result = await llm_text_call(
        client, model,
        system=(
            "Wyciągasz dane z ogłoszenia aparatu analogowego. "
            "canonical_name to '{Marka} {Model}' w standardowej pisowni angielskiej, "
            "np. 'Olympus mju-1', 'Canon Sure Shot 85'. Null jeśli model nieznany."
        ),
        user=(
            f"Tytuł: {title}\n"
            f"Opis: {description[:3000]}\n\n"
            "Wyciągnij: markę, model, canonical_name, stan aparatu i pewność każdej odpowiedzi."
        ),
        response_model=TextExtraction,
        schema_name="text-extraction",
    )

    async with lock:
        conn.execute(
            "INSERT OR REPLACE INTO text_extractions "
            "(listing_id, brand, model, canonical_name, condition, condition_confidence, model_confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                listing_id, result.brand, result.model, result.canonical_name,
                result.condition, result.condition_confidence, result.model_confidence,
            ),
        )
        conn.commit()

    return result


# ---------------------------------------------------------------------------
# Stage B: Model specs cache (web search + fetch)
# ---------------------------------------------------------------------------

_METERING_PATTERNS = [
    ("multi", re.compile(r"multi[- ]?(zone|segment|pattern)|evaluative|matrix", re.I)),
    ("center", re.compile(r"center[- ]?weighted|centre[- ]?weighted", re.I)),
    ("spot", re.compile(r"\bspot\b", re.I)),
    ("fixed", re.compile(r"fixed|program|no metering|manual", re.I)),
]
_APERTURE_RE = re.compile(r"f[/\s]?(\d+\.?\d*)", re.I)
_FOCAL_RE = re.compile(r"(\d{2,3})\s*mm", re.I)
_SLR_RE = re.compile(r"\bSLR\b|\bsingle.?lens.?reflex\b", re.I)
_AF_RE = re.compile(r"\bautofocus\b|\bauto focus\b|\bAF\b", re.I)


def _parse_specs_from_text(text: str) -> dict[str, Any]:
    specs: dict[str, Any] = {}

    m = _FOCAL_RE.search(text)
    specs["focal_mm"] = int(m.group(1)) if m else None

    apertures = [float(x) for x in _APERTURE_RE.findall(text)]
    specs["aperture_max"] = min(apertures) if apertures else None  # smallest f-number = widest

    specs["metering_type"] = None
    for name, pat in _METERING_PATTERNS:
        if pat.search(text):
            specs["metering_type"] = name
            break

    specs["focus_type"] = "af" if _AF_RE.search(text) else "fixed"
    specs["is_slr"] = 1 if _SLR_RE.search(text) else 0

    return specs


async def _ddgo_search_urls(query: str) -> list[str]:
    url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
    try:
        async with httpx.AsyncClient(headers=DDGO_HEADERS, follow_redirects=True, timeout=15) as http:
            resp = await http.get(url)
            resp.raise_for_status()
    except Exception as e:
        print(f"  DDGo search error: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    urls = []
    for a in soup.select("a.result__url"):
        href = a.get("href", "")
        if href.startswith("http") and "duckduckgo" not in href:
            urls.append(href)
    # Fallback: any result__a links
    if not urls:
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    from urllib.parse import unquote
                    urls.append(unquote(m.group(1)))
    return urls[:3]


async def fetch_model_specs(
    canonical_name: str,
    conn: sqlite3.Connection,
    lock: asyncio.Lock,
) -> dict | None:
    if not canonical_name:
        return None

    row = conn.execute(
        "SELECT * FROM model_specs WHERE canonical_name=?", (canonical_name,)
    ).fetchone()
    if row:
        return dict(row)

    query = f"{canonical_name} film camera specifications focal length aperture metering"
    urls = await _ddgo_search_urls(query)

    specs: dict = {}
    source_url = ""
    for url in urls:
        try:
            async with httpx.AsyncClient(headers=DDGO_HEADERS, follow_redirects=True, timeout=15) as http:
                resp = await http.get(url)
                resp.raise_for_status()
            text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
            parsed = _parse_specs_from_text(text)
            # Accept if we found at least aperture or focal length
            if parsed.get("focal_mm") or parsed.get("aperture_max"):
                specs = parsed
                source_url = url
                break
        except Exception as e:
            print(f"  spec fetch error ({url}): {e}")

    parts = canonical_name.split(" ", 1)
    brand = parts[0] if parts else canonical_name

    async with lock:
        conn.execute(
            "INSERT OR IGNORE INTO model_specs "
            "(canonical_name, brand, focal_mm, aperture_max, metering_type, focus_type, is_slr, specs_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                canonical_name, brand,
                specs.get("focal_mm"), specs.get("aperture_max"),
                specs.get("metering_type"), specs.get("focus_type"),
                specs.get("is_slr", 0), source_url,
            ),
        )
        conn.commit()

    row = conn.execute(
        "SELECT * FROM model_specs WHERE canonical_name=?", (canonical_name,)
    ).fetchone()
    return dict(row) if row else None


async def ensure_pc656_specs(conn: sqlite3.Connection, lock: asyncio.Lock) -> None:
    """Ensure Premier PC-656 baseline is in model_specs."""
    existing = conn.execute(
        "SELECT 1 FROM model_specs WHERE canonical_name='Premier PC-656'"
    ).fetchone()
    if existing:
        return
    async with lock:
        conn.execute(
            "INSERT OR IGNORE INTO model_specs "
            "(canonical_name, brand, focal_mm, aperture_max, metering_type, focus_type, is_slr, specs_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("Premier PC-656", "Premier", 38, 4.5, "center", "fixed", 0, "hardcoded-baseline"),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Stage C: Vision scan (lazy, iterative)
# ---------------------------------------------------------------------------

VL_PROMPT = (
    "Patrząc TYLKO na to jedno zdjęcie:\n"
    "1. Czy widzisz markę/model na obudowie? Przepisz dokładnie lub wpisz pusty string.\n"
    "2. Stan fizyczny aparatu: sprawny/uszkodzony/niepewny?\n"
    "3. Wylicz widoczne defekty (pęknięcia, korozja, brud obiektywu, grzyb, urwany uchwyt itp.).\n"
    "4. Potrzebujesz kolejnego zdjęcia żeby ocenić stan? true jeśli tak, false jeśli masz wystarczająco."
)


def _merge_vision_obs(raws: list[VisionObsRaw]) -> dict:
    """Merge multiple per-image observations into one summary."""
    # Pessimistic condition: uszkodzony > niepewny > sprawny
    priority = {"uszkodzony": 2, "niepewny": 1, "sprawny": 0}
    condition = max(raws, key=lambda r: priority[r.condition]).condition

    all_defects: set[str] = set()
    for r in raws:
        all_defects.update(r.defects)

    model_text = next((r.visible_model_text for r in raws if r.visible_model_text), "")

    return {
        "visible_model_text": model_text,
        "condition": condition,
        "defects": json.dumps(list(all_defects)),
        "images_used": len(raws),
    }


async def vision_scan(
    listing_id: int,
    text_extraction: TextExtraction,
    all_image_urls: list[str],
    client: AsyncOpenAI,
    model: str,
    conn: sqlite3.Connection,
    lock: asyncio.Lock,
) -> dict | None:
    # Check merged cache
    row = conn.execute(
        "SELECT * FROM vision_obs WHERE listing_id=?", (listing_id,)
    ).fetchone()
    if row:
        return dict(row)

    # Skip if text gave high confidence on both
    if (
        text_extraction.model_confidence == "high"
        and text_extraction.condition_confidence == "high"
    ):
        return None

    if not all_image_urls:
        return None

    raws: list[VisionObsRaw] = []
    for img_url in all_image_urls:
        try:
            obs = await llm_vision_call(
                client, model, img_url, VL_PROMPT, VisionObsRaw, "vision-obs"
            )
            raws.append(obs)

            async with lock:
                conn.execute(
                    "INSERT INTO vision_obs_raw "
                    "(listing_id, image_url, visible_model_text, condition, defects, lens_visible, needs_more_images) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        listing_id, img_url, obs.visible_model_text, obs.condition,
                        json.dumps(obs.defects), obs.lens_visible, int(obs.needs_more_images),
                    ),
                )
                conn.commit()

            if not obs.needs_more_images:
                break
        except Exception as e:
            print(f"  [L{listing_id}] VL error ({img_url}): {e}")

    if not raws:
        return None

    merged = _merge_vision_obs(raws)
    async with lock:
        conn.execute(
            "INSERT OR REPLACE INTO vision_obs "
            "(listing_id, visible_model_text, condition, defects, images_used) "
            "VALUES (?, ?, ?, ?, ?)",
            (listing_id, merged["visible_model_text"], merged["condition"],
             merged["defects"], merged["images_used"]),
        )
        conn.commit()

    return merged


# ---------------------------------------------------------------------------
# Stage D: Scoring
# ---------------------------------------------------------------------------


async def score_listing(
    listing_id: int,
    title: str,
    price: float | None,
    text_ext: TextExtraction,
    specs: dict[str, Any] | None,
    vision: dict[str, Any] | None,
    client: AsyncOpenAI,
    model: str,
    conn: sqlite3.Connection,
    lock: asyncio.Lock,
    rescore: bool = False,
) -> ListingScore:
    if not rescore:
        row = conn.execute(
            "SELECT * FROM scores WHERE listing_id=?", (listing_id,)
        ).fetchone()
        if row:
            return ListingScore(
                overall_score=row["overall_score"] or 0,
                metering_upgrade=bool(row["metering_upgrade"]),
                optics_upgrade=bool(row["optics_upgrade"]),
                is_point_and_shoot=bool(row["is_point_and_shoot"]),
                vibe_90s=bool(row["vibe_90s"]),
                condition_ok=bool(row["condition_ok"]),
                recommended=bool(row["recommended"]),
                reasoning=row["reasoning"] or "",
                skip_reason=row["skip_reason"],
            )

    vision_condition = vision["condition"] if vision else text_ext.condition
    defects = json.loads(vision["defects"]) if vision and vision["defects"] else []

    specs_text = (
        f"ogniskowa: {specs.get('focal_mm')}mm, "
        f"przysłona: f/{specs.get('aperture_max')}, "
        f"pomiar: {specs.get('metering_type')}, "
        f"fokus: {specs.get('focus_type')}, "
        f"SLR: {'tak' if specs.get('is_slr') else 'nie'}"
        if specs else "brak specyfikacji"
    )

    user_prompt = (
        f"Tytuł: {title}\n"
        f"Cena: {price} PLN\n"
        f"Model: {text_ext.canonical_name or 'nieznany'} "
        f"(pewność modelu: {text_ext.model_confidence})\n"
        f"Stan z opisu: {text_ext.condition} (pewność: {text_ext.condition_confidence})\n"
        f"Stan z wizji: {vision_condition}, defekty: {defects or 'brak'}\n"
        f"Specyfikacje modelu: {specs_text}\n"
        f"Premier PC-656 baseline: "
        f"38mm f/4.5, stały fokus, pomiar centralny (zawodny)\n\n"
        "Oceń to ogłoszenie jako potencjalny zamiennik."
    )

    result = await llm_text_call(
        client, model,
        system=SYSTEM_SCORING,
        user=user_prompt,
        response_model=ListingScore,
        schema_name="listing-score",
    )

    async with lock:
        conn.execute(
            "INSERT OR REPLACE INTO scores "
            "(listing_id, canonical_name, overall_score, metering_upgrade, optics_upgrade, "
            "is_point_and_shoot, vibe_90s, condition_ok, recommended, reasoning, skip_reason, model_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                listing_id,
                text_ext.canonical_name,
                result.overall_score,
                int(result.metering_upgrade),
                int(result.optics_upgrade),
                int(result.is_point_and_shoot),
                int(result.vibe_90s),
                int(result.condition_ok),
                int(result.recommended),
                result.reasoning,
                result.skip_reason,
                model,
            ),
        )
        conn.commit()

    return result


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


async def process_listing(
    row: sqlite3.Row,
    client: AsyncOpenAI,
    model: str,
    semaphore: asyncio.Semaphore,
    conn: sqlite3.Connection,
    lock: asyncio.Lock,
    rescore: bool,
) -> None:
    listing_id = row["id"]
    title = row["title"] or ""
    price = row["price_pln"]
    url = row["url"]

    async with semaphore:
        try:
            # Stage 0: fetch full page
            details = await fetch_listing_details(listing_id, url, conn, lock)
            if details is None:
                print(f"  [L{listing_id}] skipping: fetch failed")
                return
            description, all_images = details

            # Stage A: text extraction
            text_ext = await extract_text(
                listing_id, title, description, client, model, conn, lock
            )

            # Stage B: model specs
            specs = None
            if text_ext.canonical_name:
                specs = await fetch_model_specs(text_ext.canonical_name, conn, lock)

            # Stage C: vision scan (lazy)
            vision = await vision_scan(
                listing_id, text_ext, all_images, client, model, conn, lock
            )

            # Stage D: scoring
            score = await score_listing(
                listing_id, title, price, text_ext, specs, vision,
                client, model, conn, lock, rescore=rescore,
            )

            status = "✓ recommended" if score.recommended else f"✗ skip={score.skip_reason}"
            print(f"  [L{listing_id}] {title[:50]}: {score.overall_score:.1f} {status}")

        except Exception as e:
            print(f"  [L{listing_id}] ERROR: {e}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Process only N listings (0=all)")
    parser.add_argument("--rescore", action="store_true", help="Re-run scoring only")
    parser.add_argument("--model", type=str, default="", help="Override model name")
    args = parser.parse_args()

    init_db()
    conn = connect()
    lock = asyncio.Lock()

    client = AsyncOpenAI(base_url=VLLM_BASE_URL, api_key="none")

    model = args.model or await get_model_name(client)
    print(f"Using model: {model}")

    await ensure_pc656_specs(conn, lock)

    # Listings to process
    if args.rescore:
        # Rescore all that have text_extractions (stages 0-C already done)
        rows = conn.execute(
            "SELECT l.* FROM listings l "
            "JOIN text_extractions te ON l.id = te.listing_id"
            + (" LIMIT ?" if args.limit else ""),
            (args.limit,) if args.limit else (),
        ).fetchall()
    else:
        # Full pipeline: skip already-scored
        rows = conn.execute(
            "SELECT l.* FROM listings l "
            "WHERE l.id NOT IN (SELECT listing_id FROM scores)"
            + (" LIMIT ?" if args.limit else ""),
            (args.limit,) if args.limit else (),
        ).fetchall()

    print(f"Listings to process: {len(rows)}")

    semaphore = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        process_listing(row, client, model, semaphore, conn, lock, rescore=args.rescore)
        for row in rows
    ]
    await asyncio.gather(*tasks)

    print("\nDone.")
    total_scored = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    recommended = conn.execute("SELECT COUNT(*) FROM scores WHERE recommended=1").fetchone()[0]
    print(f"Scored: {total_scored}, Recommended: {recommended}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
