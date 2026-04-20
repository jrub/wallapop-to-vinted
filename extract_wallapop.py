"""
extract_wallapop.py
Fetches all active listings from a Wallapop profile and downloads their images.
Produces data/downloaded_items.json.

Wallapop has no public API. This script uses their internal REST API
(reverse-engineered from browser traffic) and parses __NEXT_DATA__ from the
profile page HTML to resolve the numeric user ID from a URL slug.
"""

import os
import re
import sys
import json
import time
import argparse
import requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

USER_SLUG = os.getenv("WALLAPOP_USER_ID")  # full slug, e.g. javierr-80973334
DATA_DIR = Path("data")
IMAGES_DIR = DATA_DIR / "images"
ITEMS_PATH = DATA_DIR / "downloaded_items.json"
CATEGORIES_PATH = DATA_DIR / "category_mapping.json"
WALLAPOP_CATS_PATH = DATA_DIR / "wallapop_categories.json"

# Load full Wallapop category tree once for name resolution in auto-discovery.
# Used by ensure_category_mapped() to populate the 'name' field of new stubs.
_WALLAPOP_CATS: dict = {}
if WALLAPOP_CATS_PATH.exists():
    _WALLAPOP_CATS = json.loads(WALLAPOP_CATS_PATH.read_text(encoding="utf-8"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": "https://es.wallapop.com/",
}


def load_items() -> dict:
    """Load existing downloaded_items.json, returning an empty dict if it doesn't exist."""
    if not ITEMS_PATH.exists():
        return {}
    return json.loads(ITEMS_PATH.read_text(encoding="utf-8"))


def save_items(items: dict):
    ITEMS_PATH.write_text(
        json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def resolve_internal_id(slug: str) -> str:
    """Resolve the numeric Wallapop user ID from a profile URL slug.

    Wallapop embeds full page state in a __NEXT_DATA__ script tag on the profile page.
    The numeric ID is required for the items API endpoint but is not visible in the URL.
    """
    url = f"https://es.wallapop.com/user/{slug}"
    print(f"  Slug: {slug}")
    print(f"  URL:  {url}")
    resp = requests.get(
        url,
        headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"},
        timeout=15,
    )
    resp.raise_for_status()
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.DOTALL)
    if not match:
        raise ValueError("__NEXT_DATA__ not found in profile page")
    data = json.loads(match.group(1))
    user_id = data["props"]["pageProps"]["user"]["id"]
    return user_id


# Safety cap to avoid infinite pagination or runaway requests
MAX_PAGES = 20  # 20 × 40 = 800 items max


def fetch_items(user_id: str, max_items: int = None, stop_when_all_known: set = None) -> list[dict]:
    """Fetch published listings for the given user via cursor-based pagination.

    stop_when_all_known: if every ID in a batch is already in this set, stop early.
    Wallapop returns items newest-first, so a fully-known batch means there's nothing new.
    Circular pagination (cursor looping back to already-seen IDs) is also detected.
    """
    items = []
    cursor = None
    page_size = 40
    seen_ids: set[str] = set()
    page = 0

    while True:
        url = f"https://api.wallapop.com/api/v3/users/{user_id}/items?status=published&limit={page_size}"
        if cursor:
            url += f"&since_cursor={cursor}"

        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"  Error {resp.status_code}: {resp.text[:200]}")
            break

        data = resp.json()

        # Log response shape on the first page to catch API changes early
        if page == 0:
            top_keys = list(data.keys())
            print(f"  [API] response keys: {top_keys}")
            batch_raw = data.get("data", data.get("search_objects", data.get("items", [])))
            if batch_raw and isinstance(batch_raw, list):
                sample = batch_raw[0]
                print(f"  [API] item keys: {list(sample.keys())[:12]}")
                status_field = sample.get("status") or sample.get("state") or "?"
                owner_id = sample.get("seller_id") or sample.get("user_id") or "?"
                print(f"  [API] sample — status: {status_field!r}  owner_id: {owner_id!r}  expected: {user_id!r}")

        batch = data.get("data", [])
        if not batch:
            print("  Empty batch, end of pagination.")
            break

        batch_ids = {it.get("id") for it in batch if it.get("id")}

        if batch_ids & seen_ids:
            print(f"  Circular pagination detected on page {page + 1}, stopping.")
            break
        seen_ids |= batch_ids

        items.extend(batch)
        page += 1

        if max_items and len(items) >= max_items:
            items = items[:max_items]
            break

        if stop_when_all_known and batch_ids.issubset(stop_when_all_known):
            print(f"  Whole batch already known (page {page}), stopping pagination.")
            break

        if page >= MAX_PAGES:
            print(f"  WARNING: hit the {MAX_PAGES}-page limit ({len(items)} items). "
                  f"Raise MAX_PAGES if your profile has more than {MAX_PAGES * page_size}.")
            break

        cursor = data.get("meta", {}).get("next")
        if not cursor or len(batch) < page_size:
            break

        time.sleep(0.5)

    return items


def fetch_leaf_category_id(item_id: str) -> str:
    """Fetch the leaf category ID from the item detail endpoint.

    The list endpoint returns only the top-level category_id (e.g. 24200 for all electronics).
    The detail endpoint exposes a 'taxonomy' array with the full hierarchy; the last entry
    is the most specific subcategory (e.g. 10175 for 'Teléfonos vintage').
    """
    try:
        resp = requests.get(
            f"https://api.wallapop.com/api/v3/items/{item_id}",
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            taxonomy = resp.json().get("taxonomy", [])
            if taxonomy:
                return str(taxonomy[-1]["id"])
    except Exception as e:
        print(f"    WARNING: could not fetch taxonomy for {item_id}: {e}")
    return ""


def download_image(url: str, dest: Path) -> bool:
    """Download a single image to disk. Returns True on success."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            dest.write_bytes(resp.content)
            return True
    except Exception as e:
        print(f"    Error downloading {url}: {e}")
    return False


def ensure_category_mapped(category_id: str) -> None:
    """If category_id is not in category_mapping.json, add a stub entry with vinted=null.

    The stub uses the Wallapop category name from wallapop_categories.json if available.
    Entries with vinted=null are skipped during upload and reported to the user.
    """
    if not category_id or not CATEGORIES_PATH.exists():
        return
    cats = json.loads(CATEGORIES_PATH.read_text(encoding="utf-8"))
    if category_id in cats:
        return

    # Look up the name in the full Wallapop category tree (loaded once at module level)
    name = _WALLAPOP_CATS.get(category_id, {}).get("name", category_id)

    cats[category_id] = {"name": name, "vinted": None}
    CATEGORIES_PATH.write_text(json.dumps(cats, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"    NOTE: category {category_id} ({name!r}) added to category_mapping.json without a Vinted mapping.")


def process_item(item: dict) -> dict | None:
    """Extract relevant fields from a raw API item dict and download its images.

    Returns None if the item has no ID and must be skipped.
    Images are stored under data/images/<item_id>/ and referenced by local path.
    """
    item_id = item.get("id", "")
    if not item_id:
        print("    WARNING: item without ID, skipping.")
        return None
    item_dir = IMAGES_DIR / item_id
    item_dir.mkdir(parents=True, exist_ok=True)

    image_urls = [img.get("urls", {}).get("big", "") for img in item.get("images", [])]
    local_paths = []

    for i, img_url in enumerate(image_urls):
        if not img_url:
            continue
        ext = img_url.split(".")[-1].split("?")[0] or "jpg"
        dest = item_dir / f"{i}.{ext}"
        if dest.exists():
            local_paths.append(str(dest))
        elif download_image(img_url, dest):
            local_paths.append(str(dest))
            print(f"    Image {i+1}/{len(image_urls)} downloaded")

    price_info = item.get("price", {})
    shipping = item.get("shipping", {})

    # Flatten all type_attributes into {attr_name: value} — covers condition, size,
    # color, author, publisher, language, format, and any other category-specific fields.
    raw_attrs = item.get("type_attributes", {})
    attributes = {
        k: v.get("value", "")
        for k, v in raw_attrs.items()
        if isinstance(v, dict) and v.get("value") not in (None, "")
    }

    # The list endpoint returns only the root category_id. Fetch the detail endpoint
    # to get the leaf subcategory from the taxonomy array.
    category_id = fetch_leaf_category_id(item_id) or str(item.get("category_id") or "")
    ensure_category_mapped(category_id)

    return {
        "title": item.get("title", ""),
        "description": item.get("description", ""),
        "price": price_info.get("amount", ""),
        "currency": price_info.get("currency", "EUR"),
        "category_id": category_id,
        "attributes": attributes,  # all category-specific attributes from Wallapop
        "shipping_allowed": shipping.get("user_allows_shipping", True),
        "images": local_paths,
        "url": f"https://es.wallapop.com/item/{item.get('slug', item_id)}",
        "extracted_at": datetime.utcnow().isoformat(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N new items")
    args = parser.parse_args()

    if not USER_SLUG:
        print("ERROR: set WALLAPOP_USER_ID in your .env file")
        sys.exit(1)

    DATA_DIR.mkdir(exist_ok=True)
    IMAGES_DIR.mkdir(exist_ok=True)

    existing_items = load_items()
    # Items missing the 'attributes' field were extracted before this field was added;
    # treat them as needing a refresh so their attributes get backfilled.
    stale_ids = {k for k, v in existing_items.items() if "attributes" not in v}
    fresh_ids = set(existing_items.keys()) - stale_ids
    if existing_items:
        print(f"Items already downloaded: {len(fresh_ids)} ok, {len(stale_ids)} to refresh")

    print(f"Resolving internal ID for '{USER_SLUG}'...")
    try:
        user_id = resolve_internal_id(USER_SLUG)
    except Exception as e:
        print(f"ERROR resolving user: {e}")
        sys.exit(1)
    print(f"  Internal ID: {user_id}")

    print(f"Extracting items from Wallapop...")
    # Only use fresh_ids for early-stop: stale items need to be reprocessed
    all_items = fetch_items(user_id, stop_when_all_known=fresh_ids or None)
    print(f"Total items fetched: {len(all_items)}")

    new_items = [it for it in all_items if it.get("id") not in fresh_ids]
    print(f"New or stale items to process: {len(new_items)}")

    if args.limit:
        new_items = new_items[: args.limit]

    if not new_items:
        print("Nothing new to download.")
        sys.exit(0)

    added = 0
    for i, item in enumerate(new_items):
        item_id = item.get("id", "?")
        title = item.get("title", "untitled")
        print(f"\n[{i+1}/{len(new_items)}] {title}  (id: {item_id})")
        row = process_item(item)
        if row:
            existing_items[item_id] = row
            save_items(existing_items)  # write after each item so a crash doesn't lose progress
            added += 1
        else:
            print(f"  WARNING: item {item_id!r} skipped (no ID or error).")

    print(f"\nExtraction complete.")
    print(f"  Items:  {ITEMS_PATH} ({len(existing_items)} total)")
    print(f"  New:    {added}")
    print(f"  Images: {IMAGES_DIR}")


if __name__ == "__main__":
    main()
