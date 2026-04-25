"""
extract_wallapop.py
Fetches all active listings from a Wallapop profile and downloads their images.
Produces data/downloaded_items.json.

Wallapop has no public API. This script uses their internal REST API
(reverse-engineered from browser traffic) and parses __NEXT_DATA__ from the
profile page HTML to resolve the numeric user ID from a URL slug.
"""

import os
import sys
import json
import argparse
import requests
from pathlib import Path
from dotenv import load_dotenv

from wallapop.items import fetch_items, process_item, resolve_internal_id

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N new items")
    parser.add_argument(
        "--include-in-person",
        action="store_true",
        help="Include items marked 'Sólo venta en persona' on Wallapop. They will be uploaded "
             "to Vinted as shipping items (Vinted has no in-person sale option).",
    )
    args = parser.parse_args()

    if not USER_SLUG:
        print("ERROR: set WALLAPOP_USER_ID in your .env file")
        sys.exit(1)

    DATA_DIR.mkdir(exist_ok=True)
    IMAGES_DIR.mkdir(exist_ok=True)

    # Single Session reused for every Wallapop HTTP call (profile-page slug
    # resolution, paginated items endpoint, taxonomy and image downloads).
    session = requests.Session()
    session.headers.update(HEADERS)

    existing_items = load_items()
    # Items missing the 'attributes' field were extracted before this field was added;
    # treat them as needing a refresh so their attributes get backfilled.
    stale_ids = {k for k, v in existing_items.items() if "attributes" not in v}
    fresh_ids = set(existing_items.keys()) - stale_ids
    if existing_items:
        print(f"Items already downloaded: {len(fresh_ids)} ok, {len(stale_ids)} to refresh")

    print(f"Resolving internal ID for '{USER_SLUG}'...")
    try:
        user_id = resolve_internal_id(USER_SLUG, session=session)
    except Exception as e:
        print(f"ERROR resolving user: {e}")
        sys.exit(1)
    print(f"  Internal ID: {user_id}")

    print(f"Extracting items from Wallapop...")
    # Only use fresh_ids for early-stop: stale items need to be reprocessed
    all_items = fetch_items(
        user_id, session=session, stop_when_known=fresh_ids or None
    )
    print(f"Total items fetched: {len(all_items)}")

    new_items = [it for it in all_items if it.get("id") not in fresh_ids]
    print(f"New or stale items to process: {len(new_items)}")

    # Vinted has no in-person sale option — every listing is shipped. Wallapop's
    # "Sólo venta en persona" badge surfaces in the API as shipping.user_allows_shipping=false.
    # Detect those before downloading any images so the user can decide whether to skip them
    # or include them as shipping items via --include-in-person.
    in_person = [
        it for it in new_items
        if not (it.get("shipping") or {}).get("user_allows_shipping", True)
    ]
    if in_person and not args.include_in_person:
        print("\nWARNING: the following items are marked 'Sólo venta en persona' on Wallapop:")
        for it in in_person:
            print(f"  - {it.get('title', '?')}  ({it.get('id', '?')})")
        print(
            "\nVinted only supports shipped sales. Re-run with --include-in-person to upload "
            "them anyway (they will be listed as shipping items on Vinted)."
        )
        # Drop them from the queue so the rest can still be processed.
        in_person_ids = {it.get("id") for it in in_person}
        new_items = [it for it in new_items if it.get("id") not in in_person_ids]
        if not new_items:
            sys.exit(0)
    elif in_person:
        print(f"\nIncluding {len(in_person)} 'Sólo venta en persona' item(s) as shipping items.")

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
        row = process_item(
            item,
            session=session,
            images_dir=IMAGES_DIR,
            ensure_mapped=ensure_category_mapped,
        )
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
