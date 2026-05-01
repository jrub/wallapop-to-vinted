"""Tests for NewItemPage verbs against minimal and captured HTML fixtures.

Selector correctness for the two known bugs:

1. Package size: clicking ``[data-testid='2-package-size--cell']`` (a div)
   does not propagate to the nested ``<input type="radio">``.  The fix is to
   click ``[data-testid='package_type_selector_2--input']`` directly.

2. Books scanner: ``scan_dynamic_fields`` uses selectors that end in
   ``'single-list-input'`` or ``'-dropdown-input'``.  Vinted's book-specific
   fields (ISBN, Autor, Editorial…) use different testid patterns and are
   therefore not returned.  This test auto-skips until the ``new_item_books``
   fixture is captured.
"""

from __future__ import annotations

import pytest

from vinted.pages.new_item import NewItemPage

# ---------------------------------------------------------------------------
# Minimal HTML for the package size selector block.
# Mimics the Vinted DOM structure: each size option is a cell div that wraps
# a radio input.  Clicking the div does NOT check the radio (there's no
# <label> wrapper) — which is the root cause of the bug.
# ---------------------------------------------------------------------------
_PACKAGE_SIZE_HTML = """\
<!DOCTYPE html>
<html><body>
  <div data-testid="2-package-size--cell">
    <input type="radio"
           data-testid="package_type_selector_2--input"
           name="package_type"
           value="2">
    <span>Mediano</span>
  </div>
</body></html>"""

_EMPTY_HTML = "<!DOCTYPE html><html><body></body></html>"


# ---------------------------------------------------------------------------
# Package size — Bug #1
# ---------------------------------------------------------------------------


@pytest.mark.playwright
def test_select_package_size_checks_radio_input(page):
    """select_package_size must check the radio input, not just click the cell div.

    The old code clicked the cell div; clicking a plain div does not propagate
    to a nested <input type="radio"> (that propagation only happens via <label>
    for-/wrapping).  The fix is to click the radio directly.
    """
    page.set_content(_PACKAGE_SIZE_HTML, wait_until="domcontentloaded")
    new_item = NewItemPage(page)

    result = new_item.select_package_size()

    assert result is True
    radio = page.locator("[data-testid='package_type_selector_2--input']")
    assert radio.is_checked(), (
        "Radio input must be checked after select_package_size — "
        "clicking only the cell div does not propagate to the radio."
    )


@pytest.mark.playwright
def test_select_package_size_returns_false_when_block_absent(page):
    """select_package_size must return False quickly when the package block is absent.

    When the selected category is a non-leaf (draft fallback path), Vinted
    does not render the package-size block.  The old code waited 5 s on
    Playwright's visibility retry (which caused erratic scrolling in --visible
    mode).  The fix uses a 2 s timeout and returns False cleanly.
    """
    page.set_content(_EMPTY_HTML, wait_until="domcontentloaded")
    new_item = NewItemPage(page)

    result = new_item.select_package_size()

    assert result is False


# ---------------------------------------------------------------------------
# Books scanner — Bug #2 (auto-skips until fixture is captured)
# ---------------------------------------------------------------------------


@pytest.mark.playwright
def test_scan_dynamic_fields_finds_isbn_field(load_vinted_fixture):
    """scan_dynamic_fields must return an ISBN field when a book category is active.

    Auto-skips if the ``new_item_books`` fixture hasn't been captured yet.
    Capture it with::

        python scripts/capture_vinted_fixtures.py --scope new_item_books

    Once the fixture is present this test turns red (confirming Bug #2), the
    selector in ``scan_dynamic_fields`` is extended, and the test turns green.
    """
    page = load_vinted_fixture("new_item_books")
    new_item = NewItemPage(page)

    fields = new_item.scan_dynamic_fields()
    testids = [f["testid"] for f in fields]

    assert any("isbn" in t.lower() for t in testids), (
        f"No ISBN field detected among: {testids}\n"
        "Vinted may have changed the ISBN testid — re-capture and update "
        "the selector in NewItemPage.scan_dynamic_fields."
    )
