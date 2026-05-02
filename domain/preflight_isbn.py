"""Pre-flight: collect missing ISBNs for items in book categories.

Vinted requires an ISBN for any listing under "Entretenimiento > Libros".
Wallapop treats ISBN as optional, so most listings extracted from Wallapop
don't carry one. Without an ISBN, Vinted refuses to publish ("Introduce el
ISBN para continuar") and the upload falls back to draft — silently for
every book in the catalogue.

This module runs before the browser opens:

- :func:`find_books_missing_isbn` walks the queued items, returns those
  whose Vinted nav path contains ``"Libros"`` and whose
  ``attributes.isbn`` is empty/missing. Pure: no IO.
- :func:`prompt_for_isbns` shows each item to the user and accepts an
  ISBN (free-form string) or empty input to skip. ``input_fn`` /
  ``output_fn`` are injected for tests.
- :func:`apply_isbns` mutates the items list in place: writes the ISBN
  into ``attributes.isbn`` for items the user filled, and a
  ``skip_reason`` marker for items the user skipped. Returns the ids
  the orchestrator should drop from the upload queue.

Persistence (writing ``downloaded_items.json``) lives in the orchestrator
so this module stays IO-pure on the data structure.
"""

from __future__ import annotations

from typing import Callable, Iterable

# Substring match against the nav path. Spanish-only deploys today, but
# stable as the section name in Vinted's category tree.
_BOOKS_SEGMENT = "Libros"

ISBN_FIELD = "isbn"
SKIP_REASON_KEY = "skip_reason"
SKIP_REASON_NO_ISBN = "missing_isbn"


def _has_isbn(item: dict) -> bool:
    attrs = item.get("attributes") or {}
    isbn = attrs.get(ISBN_FIELD)
    return bool(isbn) and bool(str(isbn).strip())


def _is_book(item: dict, get_nav: Callable[[dict], list | None]) -> bool:
    nav = get_nav(item)
    return bool(nav) and _BOOKS_SEGMENT in nav


def find_books_missing_isbn(
    items: Iterable[dict], get_nav: Callable[[dict], list | None]
) -> list[dict]:
    """Return items in book categories whose ISBN attribute is empty/missing.

    Items already marked with ``skip_reason`` are excluded — they're
    explicitly opted out of the run and shouldn't be re-prompted.
    """
    out: list[dict] = []
    for it in items:
        if it.get(SKIP_REASON_KEY):
            continue
        if not _is_book(it, get_nav):
            continue
        if _has_isbn(it):
            continue
        out.append(it)
    return out


def prompt_for_isbns(
    missing: list[dict],
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> dict[str, str | None]:
    """Prompt the user once per book item.

    Returns a mapping ``{item_id: isbn_or_None}``. ``None`` means the
    user explicitly skipped this item (empty input, or EOF on the
    prompt).
    """
    out: dict[str, str | None] = {}
    if not missing:
        return out

    output_fn(f"Pre-flight: {len(missing)} book item(s) need an ISBN before upload.")
    output_fn("  Enter the ISBN (digits / dashes ok), or leave blank to skip the item.")
    output_fn("  Tip: ISBNs auto-fill author / publisher / language on Vinted.")

    for it in missing:
        item_id = str(it.get("id", ""))
        title = (it.get("title") or "").strip() or "(no title)"
        try:
            raw = input_fn(f"  ISBN for [{item_id}] {title}: ").strip()
        except EOFError:
            out[item_id] = None
            continue
        out[item_id] = raw or None
    return out


def apply_isbns(
    items: list[dict], answers: dict[str, str | None]
) -> list[str]:
    """Mutate ``items`` with the user's answers; return ids to drop.

    For items where the user provided an ISBN, write it into
    ``attributes.isbn``. For items where the user skipped, set
    ``skip_reason = "missing_isbn"`` so re-runs don't re-prompt and the
    orchestrator can exclude them from the upload queue.

    Returns the list of item ids that the orchestrator should drop from
    the upload queue (the skipped ones).
    """
    skipped: list[str] = []
    for it in items:
        item_id = str(it.get("id", ""))
        if item_id not in answers:
            continue
        ans = answers[item_id]
        if ans is None:
            it[SKIP_REASON_KEY] = SKIP_REASON_NO_ISBN
            skipped.append(item_id)
        else:
            attrs = it.setdefault("attributes", {})
            attrs[ISBN_FIELD] = ans
    return skipped
