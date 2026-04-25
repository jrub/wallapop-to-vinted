"""Wallapop listings — fetch, download, and shape into the canonical record.

Split into three layers so each can be tested in isolation:

- :func:`build_item_record` is pure (no I/O, no network) and owns the shape
  every downstream consumer relies on (``data/downloaded_items.json``).
- :func:`download_item_images` does HTTP + filesystem with dedup.
- :func:`process_item` orchestrates the two and consults a caller-supplied
  ``ensure_mapped`` hook so the category-mapping side effect lives outside
  this module while it's still in ``extract_wallapop.py`` (Step 3 of the
  Phase 2 plan moves it here).
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests


# Safety cap on cursor-based pagination: 20 pages × 40 items = 800 items max
MAX_PAGES = 20


def parse_user_id_from_html(html: str) -> str:
    """Extract the numeric Wallapop user id from a profile page's HTML.

    Wallapop embeds full page state in a ``<script id="__NEXT_DATA__">``
    tag. The numeric id is required for the items API endpoint but is not
    visible in the URL (only the slug is). Pure helper so the
    "missing script" case can be tested without an HTTP fixture.
    """
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if not match:
        raise ValueError("__NEXT_DATA__ not found in profile page")
    data = json.loads(match.group(1))
    return data["props"]["pageProps"]["user"]["id"]


def resolve_internal_id(slug: str, *, session: requests.Session) -> str:
    """Resolve the numeric Wallapop user id from a profile URL slug."""
    url = f"https://es.wallapop.com/user/{slug}"
    print(f"  Slug: {slug}")
    print(f"  URL:  {url}")
    resp = session.get(
        url,
        headers={"Accept": "text/html,application/xhtml+xml"},
        timeout=15,
    )
    resp.raise_for_status()
    return parse_user_id_from_html(resp.text)


def fetch_items(
    user_id: str,
    *,
    session: requests.Session,
    max_items: int | None = None,
    stop_when_known: set | None = None,
    max_pages: int = MAX_PAGES,
) -> list[dict]:
    """Fetch published listings for ``user_id`` via cursor-based pagination.

    Stops on any of: empty batch, non-200 response (returns partial), full
    batch already in ``stop_when_known`` (Wallapop returns items
    newest-first so a fully-known batch means there's nothing new),
    circular pagination (cursor looped back to seen ids), ``max_items``
    reached, ``max_pages`` cap, or empty/missing next-cursor.
    """
    items: list[dict] = []
    cursor = None
    page_size = 40
    seen_ids: set[str] = set()
    page = 0

    while True:
        url = (
            f"https://api.wallapop.com/api/v3/users/{user_id}/items"
            f"?status=published&limit={page_size}"
        )
        if cursor:
            url += f"&since_cursor={cursor}"

        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            print(f"  Error {resp.status_code}: {resp.text[:200]}")
            break

        data = resp.json()

        # Log response shape on the first page so an API change surfaces early
        if page == 0:
            top_keys = list(data.keys())
            print(f"  [API] response keys: {top_keys}")
            batch_raw = data.get(
                "data", data.get("search_objects", data.get("items", []))
            )
            if batch_raw and isinstance(batch_raw, list):
                sample = batch_raw[0]
                print(f"  [API] item keys: {list(sample.keys())[:12]}")
                status_field = sample.get("status") or sample.get("state") or "?"
                owner_id = sample.get("seller_id") or sample.get("user_id") or "?"
                print(
                    f"  [API] sample — status: {status_field!r}  "
                    f"owner_id: {owner_id!r}  expected: {user_id!r}"
                )

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

        if stop_when_known and batch_ids.issubset(stop_when_known):
            print(
                f"  Whole batch already known (page {page}), stopping pagination."
            )
            break

        if page >= max_pages:
            print(
                f"  WARNING: hit the {max_pages}-page limit ({len(items)} items). "
                f"Raise max_pages if your profile has more than "
                f"{max_pages * page_size}."
            )
            break

        cursor = data.get("meta", {}).get("next")
        if not cursor or len(batch) < page_size:
            break

        time.sleep(0.5)

    return items


def fetch_leaf_category_id(item_id: str, *, session: requests.Session) -> str:
    """Return the leaf category id for ``item_id`` from the detail endpoint.

    The list endpoint only exposes the top-level ``category_id`` (e.g. 24200
    for all electronics). The detail endpoint includes a ``taxonomy`` array
    whose last entry is the most specific subcategory. Returns an empty
    string on any failure so the orchestrator can fall back to the root id.
    """
    try:
        resp = session.get(
            f"https://api.wallapop.com/api/v3/items/{item_id}",
            timeout=15,
        )
        if resp.status_code == 200:
            taxonomy = resp.json().get("taxonomy", [])
            if taxonomy:
                return str(taxonomy[-1]["id"])
    except Exception as e:
        print(f"    WARNING: could not fetch taxonomy for {item_id}: {e}")
    return ""


def download_image(url: str, dest: Path, *, session: requests.Session) -> bool:
    """Download a single image to ``dest``. Returns ``True`` on HTTP 200."""
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 200:
            dest.write_bytes(resp.content)
            return True
    except Exception as e:
        print(f"    Error downloading {url}: {e}")
    return False


def download_item_images(
    raw_item: dict, dest_dir: Path, *, session: requests.Session
) -> list[str]:
    """Download every image referenced by ``raw_item`` into ``dest_dir``.

    Skips URLs whose dest file already exists so re-runs are idempotent,
    preserves input order, and silently drops empty URLs and failed
    downloads. The destination directory is created if it doesn't exist.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    image_urls = [
        img.get("urls", {}).get("big", "") for img in raw_item.get("images", [])
    ]
    local_paths: list[str] = []

    for i, img_url in enumerate(image_urls):
        if not img_url:
            continue
        ext = img_url.split(".")[-1].split("?")[0] or "jpg"
        dest = dest_dir / f"{i}.{ext}"
        if dest.exists():
            local_paths.append(str(dest))
        elif download_image(img_url, dest, session=session):
            local_paths.append(str(dest))
            print(f"    Image {i+1}/{len(image_urls)} downloaded")

    return local_paths


def build_item_record(
    raw_item: dict, leaf_cat_id: str, image_paths: list[str]
) -> dict:
    """Assemble the canonical ``downloaded_items.json`` record for ``raw_item``.

    Pure: no I/O, no network. ``leaf_cat_id`` and ``image_paths`` are
    pre-computed by :func:`process_item`. ``type_attributes`` is flattened
    into ``attributes`` (covers condition, size, color, author, publisher,
    language, format, and any other category-specific field), dropping
    ``None`` and empty values so downstream code doesn't have to filter.
    """
    price_info = raw_item.get("price", {})
    shipping = raw_item.get("shipping", {})

    raw_attrs = raw_item.get("type_attributes", {})
    attributes = {
        k: v.get("value", "")
        for k, v in raw_attrs.items()
        if isinstance(v, dict) and v.get("value") not in (None, "")
    }

    item_id = raw_item.get("id", "")
    return {
        "title": raw_item.get("title", ""),
        "description": raw_item.get("description", ""),
        "price": price_info.get("amount", ""),
        "currency": price_info.get("currency", "EUR"),
        "category_id": leaf_cat_id,
        "attributes": attributes,
        "shipping_allowed": shipping.get("user_allows_shipping", True),
        "images": image_paths,
        "url": f"https://es.wallapop.com/item/{raw_item.get('slug', item_id)}",
        # ``utcnow()`` is deprecated in 3.12+; this preserves the same naive
        # ISO string format ("YYYY-MM-DDTHH:MM:SS.ffffff", no tz suffix).
        "extracted_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }


def process_item(
    raw_item: dict,
    *,
    session: requests.Session,
    images_dir: Path,
    ensure_mapped: Callable[[str], None] = lambda _: None,
) -> dict | None:
    """Download images, resolve the leaf category, build the record.

    Returns ``None`` when ``raw_item`` has no id (the caller must skip it).
    Falls back to the root ``category_id`` when the detail endpoint has no
    taxonomy. ``ensure_mapped`` is invoked with the resolved leaf id so the
    caller can persist a ``category_mapping.json`` stub without this module
    knowing about the file (decouples I/O from orchestration during the
    Phase 2 transition).
    """
    item_id = raw_item.get("id", "")
    if not item_id:
        print("    WARNING: item without ID, skipping.")
        return None

    item_dir = images_dir / item_id
    image_paths = download_item_images(raw_item, item_dir, session=session)

    leaf_cat_id = fetch_leaf_category_id(item_id, session=session) or str(
        raw_item.get("category_id") or ""
    )
    ensure_mapped(leaf_cat_id)

    return build_item_record(raw_item, leaf_cat_id, image_paths)
