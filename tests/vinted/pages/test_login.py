"""Stealth-contract tests for ``LoginPage``.

The login flow must funnel every click through ``js_click_button``
(synthetic JS click) rather than native ``locator.click()``. Native
clicks issue CDP ``Input.dispatchMouseEvent`` commands which DataDome
inspects more aggressively than a plain JS event — and once the flow
was rewritten to use native clicks, every login attempt tripped the
slider and the IP got blocked.

These tests freeze that decision so it can't regress silently.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from vinted.pages import login as login_module
from vinted.pages.login import LoginPage


class TrackedLocator:
    """Locator stand-in that records native ``.click()`` calls.

    A native click on this object means the production code bypassed
    ``js_click_button``. The tests assert the counter stays at zero.
    """

    def __init__(self):
        self.click_calls = 0

    def click(self, *args, **kwargs):
        self.click_calls += 1

    def wait_for(self, *args, **kwargs):
        return None

    def fill(self, *args, **kwargs):
        return None

    def type(self, *args, **kwargs):
        return None

    def element_handle(self, *args, **kwargs):
        return MagicMock()


@pytest.fixture
def fake_page():
    """Return a fake ``page`` whose locator factories yield ``TrackedLocator``."""
    locators: list[TrackedLocator] = []

    def _make_locator(*args, **kwargs):
        loc = TrackedLocator()
        locators.append(loc)
        return loc

    page = MagicMock()
    page.locator.side_effect = _make_locator
    page.get_by_test_id.side_effect = _make_locator
    page.get_by_role.side_effect = _make_locator
    page.get_by_placeholder.side_effect = _make_locator
    # URL must satisfy the wait_for_url predicate so login() falls through cleanly.
    page.url = "https://www.vinted.es/items/new"
    page._tracked_locators = locators
    return page


def test_login_funnels_every_click_through_js_click_button(fake_page):
    """Cookie banner + view switch + email button + submit = 4 ``js_click_button`` calls."""
    with patch.object(login_module, "js_click_button") as js_click, patch.object(
        login_module, "human_delay"
    ), patch.object(login_module, "human_type"), patch.object(
        login_module, "abort_if_captcha"
    ):
        LoginPage(fake_page).login(email="x@x.com", password="pw", visible=False)

    assert js_click.call_count == 4, (
        f"LoginPage.login() should route exactly 4 clicks through js_click_button "
        f"(cookie banner, register→login switch, login-with-email, submit). "
        f"Got {js_click.call_count}. If you refactored the login flow, restore "
        f"JS clicks for stealth — see the docstring on LoginPage."
    )


def test_login_never_calls_native_locator_click(fake_page):
    """No locator handed to login() should receive a native ``.click()``."""
    with patch.object(login_module, "js_click_button"), patch.object(
        login_module, "human_delay"
    ), patch.object(login_module, "human_type"), patch.object(
        login_module, "abort_if_captcha"
    ):
        LoginPage(fake_page).login(email="x@x.com", password="pw", visible=False)

    native_clicks = sum(loc.click_calls for loc in fake_page._tracked_locators)
    assert native_clicks == 0, (
        f"LoginPage.login() invoked native locator.click() {native_clicks} time(s). "
        f"Use js_click_button instead — DataDome flags CDP mouse events."
    )
