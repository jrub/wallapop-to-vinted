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
from upload_vinted import login as vinted_login  # noqa: E402
from vinted.pages._common import human_delay  # noqa: E402
from vinted.pages.new_item import NewItemPage  # noqa: E402
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


def _ensure_logged_in_and_at(page, target_url: str, anchor: str) -> None:
    """Navigate to ``target_url`` and ensure ``anchor`` is visible.

    If the session is expired Vinted redirects away from ``target_url``
    and the anchor never appears — in that case we delegate to
    ``upload_vinted.login`` (which knows the auth flow and waits up to
    2 minutes for a manual DataDome slider in visible mode), then
    re-navigate. This reuses the same login path the uploader uses, so
    the capture script doesn't drift from production behaviour.
    """
    page.goto(target_url)
    locator = page.locator(anchor)
    try:
        locator.wait_for(state="visible", timeout=5000)
        return
    except Exception:
        pass
    print()
    print(f"Anchor not found at {page.url} — session likely expired. Logging in...")
    vinted_login(page, visible=True)
    page.goto(target_url)
    locator.wait_for(state="visible", timeout=15000)


def _capture_new_item(page) -> None:
    _ensure_logged_in_and_at(
        page,
        target_url=f"{BASE_URL}/items/new",
        anchor="input[data-testid='title--input']",
    )


def _capture_new_item_books(page) -> None:
    """Capture the /items/new form after selecting a book category (No ficción).

    This fixture is used to test that ``NewItemPage.scan_dynamic_fields`` correctly
    detects the ISBN field (and other book-specific inputs) that fall outside the
    ``*-single-list-input`` / ``*-dropdown-input`` patterns caught by the generic
    scanner.

    Navigation path: Libros, películas y música > Libros > No ficción
    Adjust the ``_BOOKS_NAV`` constant below if Vinted's Spanish category tree has
    changed — the anchor ``[data-testid='category-condition-single-list-input']``
    should appear once a leaf category is selected.
    """
    _BOOKS_NAV = ["Libros, películas y música", "Libros", "No ficción"]

    _ensure_logged_in_and_at(
        page,
        target_url=f"{BASE_URL}/items/new",
        anchor="input[data-testid='title--input']",
    )
    human_delay(1.5, 2.5)

    new_item_page = NewItemPage(page)
    cat_ok, sub_opts = new_item_page.select_category(_BOOKS_NAV)
    if not cat_ok:
        raise RuntimeError(
            f"Category navigation to {_BOOKS_NAV} failed (sub_options: {sub_opts}). "
            "Verify the nav path against data/vinted_categories.json and update _BOOKS_NAV."
        )

    # Wait for dynamic fields to render after category selection.
    # The condition field is a reliable indicator that the category was accepted.
    human_delay(2.0, 3.0)
    try:
        page.wait_for_selector(
            "[data-testid='category-condition-single-list-input']",
            state="visible",
            timeout=10000,
        )
    except Exception:
        # Even if condition isn't visible, continue — some book sub-categories
        # don't expose condition. The ISBN field is what matters.
        pass
    human_delay(1.5, 2.5)


def _dump(page, scope: str) -> Path:
    target = FIXTURES_DIR / f"{scope}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page.content(), encoding="utf-8")
    return target


def _capture(page, scope: str) -> Path:
    if scope == "new_item":
        _capture_new_item(page)
    elif scope == "new_item_books":
        _capture_new_item_books(page)
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
