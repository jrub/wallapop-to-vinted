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

from datetime import datetime
from pathlib import Path
from typing import Callable

import requests


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
        "extracted_at": datetime.utcnow().isoformat(),
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
