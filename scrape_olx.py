"""
OLX analog camera listing scraper.

Usage:
    uv run python scrape_olx.py

Resume-safe: INSERT OR IGNORE, re-run anytime.
Selectors may need adjusting if OLX updates its DOM.
"""
import asyncio
import json
import random
import re
import sqlite3

from playwright.async_api import async_playwright

from db import DB_PATH, init_db

BASE_URL = (
    "https://www.olx.pl/elektronika/fotografia/aparaty-analogowe/q-aparat-analogowy/"
    "?courier=1"
    "&search%5Border%5D=filter_float_price%3Aasc"
    "&search%5Bfilter_float_price%3Afrom%5D=20"
    "&search%5Bfilter_float_price%3Ato%5D=120"
    "&search%5Bfilter_enum_state%5D%5B0%5D=used"
)
TOTAL_PAGES = 20


def page_url(n: int) -> str:
    if n == 1:
        return BASE_URL
    return BASE_URL + f"&page={n}"


def parse_price(text: str) -> float | None:
    text = text.replace("\xa0", "").replace(" ", "")
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not m:
        return None
    return float(m.group(1).replace(",", "."))


async def scrape_page(page, url: str) -> list[dict]:
    await page.goto(url, wait_until="networkidle", timeout=45_000)

    # Wait for at least one card
    try:
        await page.wait_for_selector('[data-cy="l-card"]', timeout=15_000)
    except Exception:
        print("  WARNING: no cards found on this page")
        return []

    cards = await page.query_selector_all('[data-cy="l-card"]')
    results = []

    for card in cards:
        try:
            # URL
            a = await card.query_selector("a[href]")
            if not a:
                continue
            href = await a.get_attribute("href") or ""
            if not href:
                continue
            if not href.startswith("http"):
                href = "https://www.olx.pl" + href

            # Skip promoted/external listings (olx.pl/d/... or gratka.pl)
            if "gratka.pl" in href or "otodom.pl" in href:
                continue

            # Title
            title_el = await card.query_selector("h6, h4, [data-cy='ad-card-title']")
            title = (await title_el.inner_text()).strip() if title_el else ""

            # Price
            price_el = await card.query_selector('[data-testid="ad-price"], .price')
            price_text = (await price_el.inner_text()).strip() if price_el else ""
            price = parse_price(price_text)

            # Location + date (combined element on OLX)
            loc_el = await card.query_selector('[data-testid="location-date"]')
            loc_text = (await loc_el.inner_text()).strip() if loc_el else ""

            # Thumbnail image URLs
            imgs = await card.query_selector_all("img[src]")
            image_urls = []
            for img in imgs:
                src = await img.get_attribute("src") or ""
                if src and not src.startswith("data:") and "olx" in src:
                    # Upgrade thumbnail to full-size if possible
                    src = re.sub(r";s=\d+x\d+", "", src)
                    image_urls.append(src)

            results.append({
                "url": href,
                "title": title,
                "price_pln": price,
                "location": loc_text,
                "image_urls": json.dumps(image_urls),
            })
        except Exception as e:
            print(f"  card error: {e}")

    return results


async def main() -> None:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="pl-PL",
        )
        # Accept cookies banner if present
        page = await ctx.new_page()
        await page.goto(BASE_URL, wait_until="networkidle", timeout=45_000)
        for selector in ['[id*="onetrust-accept"]', '[id*="accept-all"]', 'button:has-text("Akceptuję")']:
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await asyncio.sleep(1)
                break

        total = 0
        for n in range(1, TOTAL_PAGES + 1):
            url = page_url(n)
            print(f"Page {n}/{TOTAL_PAGES}: {url}")
            listings = await scrape_page(page, url)

            inserted = 0
            for item in listings:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO listings (url, title, price_pln, location, image_urls) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (item["url"], item["title"], item["price_pln"], item["location"], item["image_urls"]),
                )
                inserted += cur.rowcount
            conn.commit()
            total += len(listings)
            print(f"  {len(listings)} cards, {inserted} new (total in DB: {total})")

            delay = random.uniform(1.5, 3.5)
            print(f"  sleeping {delay:.1f}s...")
            await asyncio.sleep(delay)

        await browser.close()

    conn.close()
    print(f"\nScraping done. Listings in DB: run 'SELECT COUNT(*) FROM listings' to verify.")


if __name__ == "__main__":
    asyncio.run(main())
