"""Tests for NewItemPage verbs against synthetic HTML.

Locks the package-size selector contract against the bug where clicking
``[data-testid='2-package-size--cell']`` (a div) did not propagate to the
nested ``<input type="radio">``.  The fix clicks
``[data-testid='package_type_selector_2--input']`` directly.
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
