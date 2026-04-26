"""Capture HTML fixtures from a live Vinted session for the Phase 4 test suite.

Run once per scope by the maintainer in ``--visible`` mode against a logged-in
session. Each scope navigates to a critical surface, waits for an anchor
element to confirm the DOM has rendered, and dumps ``page.content()`` to
``tests/fixtures/vinted_html/<scope>.html``. The HTML is committed to the
repo and consumed by browser tests via ``page.set_content(...)``.

This script is intentionally thin — it only owns the navigation + dump per
scope. Page Objects are not exercised here. Re-running a scope overwrites
the previous capture; review the diff before committing.

Usage::

    python scripts/capture_vinted_fixtures.py --scope new_item
    python scripts/capture_vinted_fixtures.py --scope new_item_books
    python scripts/capture_vinted_fixtures.py --scope login

Scopes are added incrementally as the refactor needs them. Today only the
harness is in place; per-scope captures land alongside the test that
consumes each fixture.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from patchright.sync_api import sync_playwright

# Repo-relative imports — the script is expected to run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vinted.session import build_session  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTH_STATE_PATH = REPO_ROOT / "data" / "auth_state.json"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "vinted_html"

BASE_URL = "https://www.vinted.es"


SCOPES = (
    "new_item",
    "new_item_books",
    "new_item_no_shipping",
    "edit_draft",
    "login",
    "login_failed",
    "publish_failed",
    "profile_with_drafts",
)


def _capture_new_item(page) -> None:
    page.goto(f"{BASE_URL}/items/new")
    page.locator("[data-testid='title-input']").wait_for(state="visible", timeout=15000)


def _dump(page, scope: str) -> Path:
    target = FIXTURES_DIR / f"{scope}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page.content(), encoding="utf-8")
    return target


def _capture(page, scope: str) -> Path:
    if scope == "new_item":
        _capture_new_item(page)
    else:
        # Per-scope handlers are added when the corresponding test lands.
        # For now reject unknown scopes loudly so the maintainer doesn't
        # capture an empty/wrong page silently.
        raise NotImplementedError(
            f"scope '{scope}' has no capture handler yet. Add one in scripts/capture_vinted_fixtures.py"
        )
    return _dump(page, scope)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        required=True,
        choices=SCOPES,
        help="Which surface to capture. See SCOPES in the source for the full list.",
    )
    args = parser.parse_args()

    with sync_playwright() as p:
        browser, context, page = build_session(
            p, visible=True, auth_state_path=AUTH_STATE_PATH
        )
        try:
            target = _capture(page, args.scope)
        finally:
            context.close()
            browser.close()

    print(f"Captured {args.scope} -> {target}")
    print("Review the diff before committing. Sanitize any personal data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
