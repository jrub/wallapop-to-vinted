"""Smoke + structural-regression tests for the captured Vinted HTML fixtures.

These tests verify two things:

1. ``load_vinted_fixture`` actually loads the HTML into a Playwright page
   (smoke test of the fixture machinery itself).
2. The captured HTML still contains the anchor element each Page Object
   relies on (a cheap structural check). When Vinted refactors a form,
   the anchor disappears and this test fails loudly — at which point
   the maintainer re-captures the fixture and reviews the diff.

Each scope's anchor is the same element the capture script waits for
before dumping the page, so a fresh capture always satisfies its own
anchor. Failures here mean the captured HTML drifted (manual edit, or
Vinted served a different page than expected).
"""

from __future__ import annotations

import pytest

# Per-scope anchor selectors. Keep in sync with ``scripts/capture_vinted_fixtures.py``.
ANCHORS = {
    "new_item": "input[data-testid='title--input']",
}


@pytest.mark.playwright
@pytest.mark.parametrize("scope", list(ANCHORS.keys()))
def test_fixture_loads_and_has_anchor(load_vinted_fixture, scope):
    page = load_vinted_fixture(scope)
    anchor = ANCHORS[scope]
    assert page.locator(anchor).count() > 0, (
        f"Fixture '{scope}' is missing its anchor selector {anchor!r}. "
        f"Re-capture with: python scripts/capture_vinted_fixtures.py --scope {scope}"
    )
