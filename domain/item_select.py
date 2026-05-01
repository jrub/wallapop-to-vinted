"""Helpers for scoping a run to a single item.

Both helpers are pure / IO-injectable so the CLI flow can be tested
without touching ``stdin`` / ``stdout``:

- :func:`select_by_id` is a pure lookup over a list of item dicts.
- :func:`prompt_selection` accepts ``input_fn`` and ``output_fn``
  callables; the production caller passes the builtins ``input`` and
  ``print``, while tests inject in-memory stand-ins.
"""

from __future__ import annotations

from typing import Callable, Iterable


def select_by_id(items: Iterable[dict], item_id: str) -> dict | None:
    """Return the first item whose ``id`` matches ``item_id``, else ``None``.

    Comparison is string-based: numeric ids in the source dict and
    string ids on the CLI compare equal.
    """
    target = str(item_id)
    for it in items:
        if str(it.get("id")) == target:
            return it
    return None


def prompt_selection(
    items: list[dict],
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> dict | None:
    """Render a numbered menu and return the picked item.

    Returns ``None`` when:

    - ``items`` is empty,
    - the user enters an empty string, ``q``, ``quit`` or ``exit``,
    - or ``input_fn`` raises ``EOFError`` (closed stdin).

    Non-integer or out-of-range responses re-prompt without exiting,
    so a typo doesn't drop the operator back to the shell.
    """
    if not items:
        output_fn("No items to choose from.")
        return None

    for i, it in enumerate(items, start=1):
        title = (it.get("title") or "").strip() or "(no title)"
        cat = it.get("category_id") or "?"
        output_fn(f"  {i}. [{it.get('id')}] {title} (cat {cat})")

    while True:
        try:
            raw = input_fn(f"Pick item [1-{len(items)}, q to quit]: ").strip()
        except EOFError:
            return None
        if not raw or raw.lower() in ("q", "quit", "exit"):
            return None
        try:
            idx = int(raw)
        except ValueError:
            output_fn(f"  Not a number: {raw!r}")
            continue
        if 1 <= idx <= len(items):
            return items[idx - 1]
        output_fn(f"  Out of range: {idx}")
