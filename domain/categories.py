"""Vinted category-tree navigation helpers.

Pure functions over the parsed `data/vinted_categories.json` payload — no I/O,
no module-level state. The caller (e.g. ``upload_vinted.py``) loads the JSON
once, builds an index with :func:`build_path_index`, and passes both to
:func:`resolve_nav_to_leaf` per item.
"""

from .text import normalize_label, stem


def build_path_index(nodes: list[dict]) -> dict[tuple, dict]:
    """Index Vinted category nodes by their path tuple for O(1) lookup.

    Each node is expected to have a ``"path"`` list of titles from root to self.
    """
    return {tuple(n["path"]): n for n in nodes}


def pick_leaf_from_hints(sub_options: list[str], hints: str) -> str | None:
    """Choose a Vinted sub-option whose main keyword appears in the item text.

    For each sub-option we take the longest alphabetic word (the "main" word,
    typically the category name — 'Repetidores de red' → 'repetidores') and
    stem it. If exactly one sub-option's stem shows up as a whole word (or
    stemmed word) in the normalized item hints, we return it. Otherwise None.

    Deliberately strict: we won't auto-pick on an ambiguous match. Anything
    that's not unambiguous ends up as a draft for the human to finish.
    """
    norm_hints = normalize_label(hints)
    if not norm_hints:
        return None
    hint_words = set(norm_hints.split())
    hint_stems = {stem(w) for w in hint_words}

    matches: list[str] = []
    for opt in sub_options:
        words = [w for w in normalize_label(opt).split() if w.isalpha()]
        if not words:
            continue
        main = max(words, key=len)
        main_stem = stem(main)
        if main_stem in hint_stems or main_stem in hint_words:
            matches.append(opt)

    return matches[0] if len(matches) == 1 else None


def resolve_nav_to_leaf(
    nav: list[str],
    hints: str,
    *,
    nodes: list[dict],
    path_index: dict[tuple, dict],
) -> list[str]:
    """Extend a nav path to a Vinted leaf using the local category tree.

    If ``path_index`` is empty (no tree loaded), or the path isn't found, the
    nav is returned unchanged.

    If the path lands on an intermediate node (has children), we attempt to
    pick the right child via :func:`pick_leaf_from_hints` and recurse so paths
    that are two steps short of a leaf can still resolve in one call.
    """
    if not path_index:
        return nav

    path_key = tuple(nav)
    node = path_index.get(path_key)
    if node is None:
        return nav  # path not found in tree — let the caller fail gracefully
    if node["is_leaf"]:
        return nav  # already a leaf, nothing to extend

    depth = len(nav)
    children = [
        n["title"]
        for n in nodes
        if len(n["path"]) == depth + 1 and tuple(n["path"][:depth]) == path_key
    ]
    if not children:
        return nav

    picked = pick_leaf_from_hints(children, hints)
    if picked is None:
        return nav

    extended = nav + [picked]
    return resolve_nav_to_leaf(extended, hints, nodes=nodes, path_index=path_index)
