"""Migration state — load/save ``migration.json`` and filter the upload queue.

The file lives under ``data/migration.json`` (path is injected by the caller
so the module stays I/O-agnostic and easy to test). It records, per Wallapop
item id, the resulting ``vinted_id``, ``status`` (``published`` / ``draft`` /
``failed``), ``missing_fields`` and ``last_error`` so re-runs are idempotent.

:func:`filter_pending` packages the queue-trimming rules: skip items already
``published`` or saved as ``draft``; separate items without a Vinted nav path
into the ``unmapped`` bucket; apply ``--limit`` *after* those filters so
``--limit 1`` always means "one real upload attempt" regardless of how many
entries already exist in ``migration.json``.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def load_migration(path: Path) -> dict:
    """Read ``migration.json`` from ``path``. Returns ``{}`` if the file is absent."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def mark_migrated(
    migration: dict,
    path: Path,
    item_id: str,
    vinted_id: str,
    status: str,
    missing_fields: list[str] | None = None,
    error: str = "",
) -> None:
    """Record an upload outcome and persist ``migration`` to ``path`` atomically.

    Written after every item for crash safety: if the script aborts mid-run,
    re-running it picks up exactly where it left off.
    """
    migration.setdefault(item_id, {}).update(
        {
            "vinted_id": vinted_id,
            "status": status,
            "missing_fields": missing_fields or [],
            "last_error": error,
            # ``utcnow()`` is deprecated in 3.12+; this preserves the same
            # naive ISO string format already on disk in migration.json.
            "uploaded_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        }
    )
    path.write_text(
        json.dumps(migration, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def migration_status(entry: dict) -> str:
    """Read status from a ``migration.json`` entry.

    Falls back to the legacy ``vinted_status`` key so older files written
    before the rename still work without manual migration.
    """
    return entry.get("status") or entry.get("vinted_status") or ""


def filter_pending(
    items: list[dict],
    migration: dict,
    get_nav: Callable[[dict], list | None],
    *,
    retry_drafts: bool = False,
    limit: int = 0,
) -> tuple[list[dict], int, list[tuple[str, str, str]]]:
    """Trim the upload queue.

    Returns ``(pending, already_done, unmapped)``:

    - ``pending``: items to actually upload (failed retries, fresh items),
      capped to ``limit`` when ``limit > 0``.
    - ``already_done``: count of items skipped because they're already
      ``published`` or saved as ``draft`` in ``migration``.
    - ``unmapped``: ``(item_id, title, category_id)`` for items whose
      ``get_nav`` returned ``None``.

    With ``retry_drafts=True`` the orchestrator handles its own filtering
    against existing Vinted drafts, so this function returns ``items``
    untouched.
    """
    if retry_drafts:
        return list(items), 0, []

    already_done = 0
    pending: list[dict] = []
    unmapped: list[tuple[str, str, str]] = []

    for item in items:
        item_id = item.get("id", "")
        prev = migration.get(item_id, {}) if item_id else {}
        if prev.get("vinted_id") and migration_status(prev) in ("published", "draft"):
            already_done += 1
            continue
        if get_nav(item) is None:
            unmapped.append(
                (item_id, item.get("title", ""), item.get("category_id", "?"))
            )
            continue
        pending.append(item)

    if limit:
        pending = pending[:limit]

    return pending, already_done, unmapped
