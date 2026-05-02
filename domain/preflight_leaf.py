"""Pre-flight: collect Vinted leaf overrides for ambiguous items.

A single Wallapop ``category_id`` often covers items Vinted splits across
several leaves. ``10304 Componentes y piezas de ordenador`` can be RAM,
a GPU, or a floppy disk — three different Vinted leaves under the same
parent. The local resolver (``categories.resolve_nav_to_leaf`` +
``pick_leaf_from_hints``) handles the unambiguous cases via stem-matching,
but stops at the first intermediate node when the item text gives no hint.

This module runs before the browser opens and captures the human decision
so the upload flow can take it on faith:

- :func:`find_ambiguous` walks the queue, resolves each item's nav with
  the same logic the upload flow uses, and returns the ones whose
  resolved path stops short of a leaf, paired with the candidate child
  titles. Items that already carry a ``vinted_nav_override`` are
  excluded from the prompt.
- :func:`prompt_for_overrides` shows each ambiguous item with title,
  description, and numbered candidates; accepts a 1-based index, or
  empty / ``q`` / EOF to skip. Returns ``{item_id: full_path | None}``.
- :func:`apply_overrides` mutates each item with the picked full path
  under ``vinted_nav_override``. Skipped items are left untouched
  (no marker — leaf-skip is per-run by design; a future run with new
  context can decide differently).

Persistence (writing ``downloaded_items.json``) lives in the orchestrator
so this module stays IO-pure on the data structure.
"""

from __future__ import annotations

from typing import Callable, Iterable

from domain.categories import resolve_nav_to_leaf

NAV_OVERRIDE_KEY = "vinted_nav_override"


def _children_of(path: list[str], nodes: list[dict]) -> list[str]:
    depth = len(path)
    key = tuple(path)
    return [
        n["title"]
        for n in nodes
        if len(n["path"]) == depth + 1 and tuple(n["path"][:depth]) == key
    ]


def find_ambiguous(
    items: Iterable[dict],
    get_nav: Callable[[dict], list | None],
    *,
    nodes: list[dict],
    path_index: dict[tuple, dict],
) -> list[tuple[dict, list[str], list[str]]]:
    """Return items whose nav resolves to an intermediate node, with candidates.

    Each tuple is ``(item, resolved_path, candidate_children)``. Items
    are skipped from the result when:

    - They already have a ``vinted_nav_override`` (the user decided on a
      previous run).
    - ``get_nav`` returns ``None`` (unmapped category — handled elsewhere).
    - The resolved path lands on a leaf or the path isn't in the tree
      (the latter is logged later as a separate issue).
    """
    out: list[tuple[dict, list[str], list[str]]] = []
    for it in items:
        if it.get(NAV_OVERRIDE_KEY):
            continue
        nav = get_nav(it)
        if not nav:
            continue
        hints = f"{it.get('title', '')} {it.get('description', '')}"
        resolved = resolve_nav_to_leaf(nav, hints, nodes=nodes, path_index=path_index)
        node = path_index.get(tuple(resolved))
        if node is None or node.get("is_leaf"):
            continue
        children = _children_of(resolved, nodes)
        if not children:
            continue
        out.append((it, resolved, children))
    return out


def prompt_for_overrides(
    ambiguous: list[tuple[dict, list[str], list[str]]],
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> dict[str, list[str] | None]:
    """Prompt once per ambiguous item; return ``{item_id: full_path | None}``.

    ``None`` means the user explicitly skipped (empty input, ``q``,
    ``quit``, or EOF). Skipped items get no marker — the upload flow
    will fall back to its existing resolver chain.
    """
    out: dict[str, list[str] | None] = {}
    if not ambiguous:
        return out

    output_fn(
        f"Pre-flight: {len(ambiguous)} item(s) need a Vinted leaf decision."
    )
    output_fn(
        "  Pick a child by number, or leave blank / 'q' to skip."
    )

    for it, resolved, children in ambiguous:
        item_id = str(it.get("id", ""))
        title = (it.get("title") or "").strip() or "(no title)"
        desc = (it.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 120:
            desc = desc[:117] + "..."
        output_fn("")
        output_fn(f"  [{item_id}] {title}")
        if desc:
            output_fn(f"    {desc}")
        output_fn(f"    Resolved so far: {' > '.join(resolved)}")
        output_fn(f"    Candidates:")
        for i, c in enumerate(children, start=1):
            output_fn(f"      {i}. {c}")

        try:
            raw = input_fn(f"    Pick [1-{len(children)}, blank to skip]: ").strip()
        except EOFError:
            out[item_id] = None
            continue
        if not raw or raw.lower() in ("q", "quit", "exit"):
            out[item_id] = None
            continue
        try:
            idx = int(raw)
        except ValueError:
            output_fn(f"    Not a number: {raw!r} — skipping.")
            out[item_id] = None
            continue
        if 1 <= idx <= len(children):
            out[item_id] = resolved + [children[idx - 1]]
        else:
            output_fn(f"    Out of range: {idx} — skipping.")
            out[item_id] = None
    return out


def apply_overrides(
    items: list[dict], answers: dict[str, list[str] | None]
) -> int:
    """Mutate ``items`` with the picked overrides; return count applied.

    Items the user skipped (``None`` value) are left untouched — leaf
    decisions are per-run by design, since item context can change
    between runs.
    """
    applied = 0
    for it in items:
        item_id = str(it.get("id", ""))
        if item_id not in answers:
            continue
        ans = answers[item_id]
        if ans is None:
            continue
        it[NAV_OVERRIDE_KEY] = list(ans)
        applied += 1
    return applied
