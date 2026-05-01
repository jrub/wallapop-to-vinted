"""Shared low-level browser utilities for the Vinted page objects.

These helpers are deliberately free of application logic so they can be
reused across NewItemPage, EditDraftPage, and any future page objects
without creating cross-module dependencies.
"""

from __future__ import annotations

import random
import time

# Vinted marks open dropdown panels with data-testid ending in "-dropdown-content".
# Scoping queries to this element avoids matching buttons elsewhere on the page.
_PANEL_SELECTOR = '[data-testid$="-dropdown-content"]'


def human_delay(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def human_type(locator, text: str) -> None:
    """Type text into a Playwright locator character-by-character.

    Vinted's React form listens to keyboard events, not DOM value changes.
    page.fill() sets the value directly without firing key events, leaving
    the field visually empty. press_sequentially() fires the correct events.
    """
    locator.click()
    human_delay(0.2, 0.5)
    for char in text:
        locator.press_sequentially(char)
        time.sleep(random.uniform(0.06, 0.22))
        if random.random() < 0.08:
            time.sleep(random.uniform(0.15, 0.45))


def js_click_button(page, locator) -> None:
    """Click a button via JavaScript to avoid Playwright's scroll-into-view behaviour.

    Playwright's locator.click() scrolls the element into view before clicking,
    causing the viewport to jump — which DataDome interprets as bot-like behaviour.
    A JS click fires the event without moving the scroll position.
    """
    try:
        el = locator.element_handle(timeout=3000)
        if el:
            page.evaluate("el => el.click()", el)
    except Exception:
        locator.click(timeout=10000)  # graceful fallback
