"""extract_vinted_categories.py — Phase 0: crawl the Vinted category tree.

Fetches vinted.es/catalog (public, no auth required), extracts the catalogTree
embedded in the Next.js streaming payload, and writes data/vinted_categories.json.

Output format:
  A list of all nodes (both intermediate and leaf), each with:
    id       — Vinted catalog numeric ID
    code     — machine-readable code (e.g. "CAPES_PONCHOS")
    title    — Spanish display name
    url      — relative URL slug (e.g. "/catalog/1773-capes-and-ponchos")
    path     — full nav path as a list of titles, root first
               e.g. ["Mujer", "Ropa", "Abrigos y cazadoras", "Capas y ponchos"]
    is_leaf  — true when catalogs == []

Run:
  python extract_vinted_categories.py
"""

import json
import sys
from pathlib import Path

import requests

OUTPUT_PATH = Path("data/vinted_categories.json")
CATALOG_URL = "https://www.vinted.es/catalog"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
}


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _extract_catalog_tree(html: str) -> list:
    """Parse catalogTree from the Next.js streaming payload embedded in the HTML.

    Vinted uses Next.js App Router, which inlines RSC payloads as
    ``self.__next_f.push([1, "ID:JSON"])`` script tags.  The chunk containing
    ``catalogTree`` holds the full public category tree without requiring auth.
    """
    marker = "catalogTree"
    ct_idx = html.find(marker)
    if ct_idx == -1:
        raise ValueError("catalogTree not found in page HTML")

    # Walk back to the opening quote of the push string argument.
    chunk_start = html.rfind("self.__next_f.push", 0, ct_idx)
    if chunk_start == -1:
        raise ValueError("Could not locate self.__next_f.push before catalogTree")

    arg_start = html.index('"', chunk_start + len("self.__next_f.push([1,"))

    # Scan forward to find the closing unescaped quote of the JS string literal.
    pos = arg_start + 1
    while pos < len(html):
        ch = html[pos]
        if ch == "\\":
            pos += 2  # skip escape sequence
            continue
        if ch == '"':
            break
        pos += 1
    else:
        raise ValueError("Unterminated JS string in self.__next_f.push argument")

    raw_str = html[arg_start + 1 : pos]

    # The raw_str is the *content* of a JSON string literal — decode it.
    decoded: str = json.loads('"' + raw_str + '"')

    # RSC format: "ID:JSON_VALUE"
    colon = decoded.index(":")
    json_part = decoded[colon + 1 :]
    parsed = json.loads(json_part)

    # parsed is ["$", "$L<n>", null, {"catalogTree": [...]}]
    try:
        return parsed[3]["catalogTree"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected RSC structure: {exc}") from exc


def _flatten(nodes: list, path: tuple = ()) -> list:
    """Recursively flatten the nested catalogTree into a list of node dicts."""
    result = []
    for node in nodes:
        current_path = path + (node["title"],)
        children = node.get("catalogs") or []
        result.append(
            {
                "id": node["id"],
                "code": node["code"],
                "title": node["title"],
                "url": node.get("url", ""),
                "path": list(current_path),
                "is_leaf": len(children) == 0,
            }
        )
        result.extend(_flatten(children, current_path))
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"Fetching {CATALOG_URL} …")
    try:
        response = requests.get(CATALOG_URL, headers=HEADERS, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"ERROR: HTTP request failed: {exc}")
        sys.exit(1)

    print(f"  Page size: {len(response.text):,} bytes")

    print("Extracting catalogTree …")
    try:
        catalog_tree = _extract_catalog_tree(response.text)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    nodes = _flatten(catalog_tree)
    leaves = [n for n in nodes if n["is_leaf"]]
    max_depth = max(len(n["path"]) for n in nodes)

    print(f"  Total nodes : {len(nodes)}")
    print(f"  Leaf nodes  : {len(leaves)}")
    print(f"  Max depth   : {max_depth}")

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(nodes, fh, ensure_ascii=False, indent=2)

    print(f"Saved → {OUTPUT_PATH}")

    # Quick sanity sample
    print("\nSample leaves:")
    for n in leaves[:5]:
        print(f"  {' > '.join(n['path'])}  [id={n['id']}]")


if __name__ == "__main__":
    main()
