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
from pydantic import BaseModel, Field
from typing import Literal

from db import DB_PATH, init_db, connect

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

VLLM_BASE_URL = "http://localhost:8000/v1"
SEARCH_BASE_URL = "http://localhost:8099"  # open-websearch HTTP API (SSH tunnel z Windows)
CONCURRENCY = 8
THINKING_BUDGET = 16000
MAX_TOOL_ROUNDS = 5

SYSTEM_SCORING = (
    "Jesteś ekspertem aparatów analogowych lat 90. Szukasz zamiennika dla Premier PC-656 "
    "(38mm f/4.5, stały fokus, zawodny pomiar centralny, słaba optyka). "
    "Kryterium: lepsze naświetlanie + automatyka ekspozycji, 90s point-and-shoot vibe, NIE SLR, budżet 20–120 PLN.\n\n"
    "INSTRUKCJA: Jeśli znasz markę/model aparatu, UŻYJ narzędzi wyszukiwania aby znaleźć "
    "specyfikacje techniczne (ogniskowa mm, max przysłona f/, typ pomiaru ekspozycji, autofokus/fokus stały). "
    "Szukaj precyzyjnie np. '{marka} {model} specifications film camera'. "
    "Jeśli strona nie zawiera liczb — wejdź w link i czytaj dalej. Powtarzaj aż znajdziesz dane lub wyczerpiesz 5 prób."
)

DDGO_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Wyszukaj w internecie specyfikacje aparatu fotograficznego.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_content",
            "description": "Pobierz treść strony internetowej ze specyfikacją aparatu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_length": {"type": "integer", "default": 6000},
                },
                "required": ["url"],
            },
        },
    },
]

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
    overall_score: float = Field(ge=1.0, le=10.0)
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
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
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
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
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
# Stage B: tool executor (Qwen searches itself)
# ---------------------------------------------------------------------------


async def _exec_tool(name: str, args: dict[str, Any]) -> str:
    """Wykonaj tool przez open-websearch HTTP API (SSH tunnel z Windows)."""
    async with httpx.AsyncClient(timeout=20) as http:
        if name == "search":
            resp = await http.post(f"{SEARCH_BASE_URL}/search", json={
                "query": args.get("query", ""),
                "limit": args.get("max_results", 5),
                "engines": ["startpage"],
            })
            data = resp.json()
            results = data.get("data", {}).get("results", [])
            return "\n\n".join(
                f"{r.get('title','')}\n{r.get('url','')}\n{r.get('description','')}"
                for r in results
            )
        elif name == "fetch_content":
            resp = await http.post(f"{SEARCH_BASE_URL}/fetch-web", json={
                "url": args.get("url", ""),
                "maxChars": args.get("max_length", 6000),
            })
            data = resp.json()
            return data.get("data", {}).get("content", "")
    return ""


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

    # Phase 1: agentic research — Qwen decyduje czy i co szukać (thinking ON)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_SCORING},
        {
            "role": "user",
            "content": (
                f"Tytuł: {title}\n"
                f"Cena: {price} PLN\n"
                f"Model: {text_ext.canonical_name or 'nieznany'} (pewność: {text_ext.model_confidence})\n"
                f"Stan z opisu: {text_ext.condition} (pewność: {text_ext.condition_confidence})\n"
                f"Stan wizualny: {vision_condition}, defekty: {defects or 'brak'}\n"
                f"Premier PC-656 baseline: 38mm f/4.5, stały fokus, pomiar centralny (zawodny)\n\n"
                "Zbadaj i oceń jako zamiennik dla Premier PC-656."
            ),
        },
    ]

    for _ in range(MAX_TOOL_ROUNDS):
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=DDGO_TOOLS,
            tool_choice="auto",
            temperature=0.6,
            max_tokens=THINKING_BUDGET + 2048,
            extra_body={"chat_template_kwargs": {"thinking_budget": THINKING_BUDGET}},
        )
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)

        if not tool_calls:
            messages.append({"role": "assistant", "content": msg.content})
            break

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            tool_result = await _exec_tool(
                tc.function.name, json.loads(tc.function.arguments)
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_result[:5000]})

    # Phase 2: ekstrakcja do JSON (thinking OFF)
    messages.append({"role": "user", "content": "Wypełnij teraz strukturę oceny JSON."})
    resp2 = await client.chat.completions.create(
        model=model,
        messages=messages,
        response_format=_response_format(ListingScore, "listing-score"),
        temperature=0.1,
        max_tokens=1024,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    result = ListingScore.model_validate_json(resp2.choices[0].message.content or "{}")  # type: ignore[return-value]

    async with lock:
        conn.execute(
            "INSERT OR REPLACE INTO scores "
            "(listing_id, canonical_name, overall_score, metering_upgrade, optics_upgrade, "
            "is_point_and_shoot, vibe_90s, condition_ok, recommended, reasoning, skip_reason, model_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                listing_id, text_ext.canonical_name, result.overall_score,
                int(result.metering_upgrade), int(result.optics_upgrade),
                int(result.is_point_and_shoot), int(result.vibe_90s),
                int(result.condition_ok), int(result.recommended),
                result.reasoning, result.skip_reason, model,
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

            # Stage C: vision scan (lazy)
            vision = await vision_scan(
                listing_id, text_ext, all_images, client, model, conn, lock
            )

            # Stage D: agentic scoring (Qwen searches specs + reasons)
            score = await score_listing(
                listing_id, title, price, text_ext, vision,
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
