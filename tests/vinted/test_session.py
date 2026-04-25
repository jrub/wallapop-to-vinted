"""Tests for the Vinted session helpers (captcha detector + JWT cookie helper).

Stubbed ``page`` objects only — Patchright is not launched here. Bootstrap
(``build_session``) is verified by ``make smoke`` and a real
``--limit 1 --visible`` run, since spinning up Chromium under pytest would
couple the unit suite to a binary install for no extra coverage.
"""

import base64
import json
from types import SimpleNamespace

import pytest

from vinted.errors import CaptchaDetected
from vinted.session import (
    abort_if_captcha,
    is_captcha_present,
    user_id_from_jwt_cookie,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeLocator:
    """Minimal Playwright Locator stand-in: configurable count() return/raise."""

    def __init__(self, count_value: int = 0, count_raises: Exception | None = None):
        self._count = count_value
        self._raises = count_raises

    def count(self) -> int:
        if self._raises is not None:
            raise self._raises
        return self._count


class FakePage:
    """Minimal Page stand-in.

    ``locator(selector) -> FakeLocator`` looks up the selector in
    ``locators_by_selector``; missing selectors return an empty locator
    (count == 0). ``cookies`` is the value returned by
    ``page.context.cookies()``; ``cookies_raises`` lets a test simulate the
    Playwright call blowing up.
    """

    def __init__(
        self,
        *,
        url: str = "https://www.vinted.es/items/new",
        locators_by_selector: dict[str, FakeLocator] | None = None,
        cookies: list[dict] | None = None,
        cookies_raises: Exception | None = None,
    ):
        self.url = url
        self._locators = locators_by_selector or {}

        # Wire ``page.context.cookies()``
        def _cookies():
            if cookies_raises is not None:
                raise cookies_raises
            return list(cookies or [])

        self.context = SimpleNamespace(cookies=_cookies)

    def locator(self, selector: str) -> FakeLocator:
        return self._locators.get(selector, FakeLocator(count_value=0))


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _make_jwt(payload: dict) -> str:
    """Build a JWT-shaped string (header.payload.signature) for tests.

    Only the payload matters for the helper under test — header and
    signature can be any non-empty base64url chunks.
    """
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    return f"header.{encoded}.sig"


# ---------------------------------------------------------------------------
# is_captcha_present
# ---------------------------------------------------------------------------


class TestIsCaptchaPresent:
    """The detector now only checks for the DataDome iframe.

    The original ``"captcha-delivery.com" in page.url`` branch was dead code
    in production — DataDome always overlays the Vinted page with an iframe,
    never replaces it with a top-level redirect.
    """

    def test_iframe_present_returns_true(self):
        page = FakePage(
            locators_by_selector={
                "iframe[src*='captcha-delivery.com']": FakeLocator(count_value=1),
            }
        )
        assert is_captcha_present(page) is True

    def test_no_iframe_returns_false(self):
        page = FakePage()  # default locator returns count=0
        assert is_captcha_present(page) is False

    def test_locator_exception_swallowed_returns_false(self):
        # Playwright sometimes raises when the page is mid-navigation. The
        # original implementation swallowed those errors with a bare except
        # — preserve that behaviour so a transient hiccup doesn't crash
        # the upload loop.
        page = FakePage(
            locators_by_selector={
                "iframe[src*='captcha-delivery.com']": FakeLocator(
                    count_raises=RuntimeError("page closing"),
                ),
            }
        )
        assert is_captcha_present(page) is False


# ---------------------------------------------------------------------------
# abort_if_captcha
# ---------------------------------------------------------------------------


class TestAbortIfCaptcha:
    def test_visible_mode_never_raises_even_with_captcha(self):
        # In visible mode the user solves the slider manually — the
        # function returns silently regardless of detector state.
        page = FakePage(
            locators_by_selector={
                "iframe[src*='captcha-delivery.com']": FakeLocator(count_value=1),
            }
        )
        abort_if_captcha(page, visible=True)  # no raise

    def test_headless_no_captcha_does_not_raise(self):
        page = FakePage()
        abort_if_captcha(page, visible=False)  # no raise

    def test_headless_with_captcha_raises_with_url(self):
        url = "https://www.vinted.es/items/new"
        page = FakePage(
            url=url,
            locators_by_selector={
                "iframe[src*='captcha-delivery.com']": FakeLocator(count_value=1),
            },
        )
        with pytest.raises(CaptchaDetected) as exc_info:
            abort_if_captcha(page, visible=False)
        assert exc_info.value.url == url


# ---------------------------------------------------------------------------
# user_id_from_jwt_cookie
# ---------------------------------------------------------------------------


class TestUserIdFromJwtCookie:
    def test_extracts_sub_from_access_token(self):
        token = _make_jwt({"sub": "12345"})
        page = FakePage(cookies=[{"name": "access_token_web", "value": token}])
        assert user_id_from_jwt_cookie(page) == "12345"

    def test_falls_back_to_refresh_token_when_access_token_missing(self):
        token = _make_jwt({"sub": "67890"})
        page = FakePage(cookies=[{"name": "refresh_token_web", "value": token}])
        assert user_id_from_jwt_cookie(page) == "67890"

    def test_prefers_access_token_over_refresh_token(self):
        access = _make_jwt({"sub": "11111"})
        refresh = _make_jwt({"sub": "22222"})
        page = FakePage(
            cookies=[
                {"name": "access_token_web", "value": access},
                {"name": "refresh_token_web", "value": refresh},
            ]
        )
        assert user_id_from_jwt_cookie(page) == "11111"

    def test_non_numeric_sub_returns_empty(self):
        # A JWT with a non-numeric ``sub`` (e.g. an OAuth client id) is not
        # the user id we want; skip and try the next cookie.
        token = _make_jwt({"sub": "not-a-number"})
        page = FakePage(cookies=[{"name": "access_token_web", "value": token}])
        assert user_id_from_jwt_cookie(page) == ""

    def test_malformed_token_skipped_falls_back_to_next_cookie(self):
        good = _make_jwt({"sub": "99999"})
        page = FakePage(
            cookies=[
                {"name": "access_token_web", "value": "not.a.jwt.too.many.dots"},
                {"name": "refresh_token_web", "value": good},
            ]
        )
        assert user_id_from_jwt_cookie(page) == "99999"

    def test_no_cookies_returns_empty(self):
        page = FakePage(cookies=[])
        assert user_id_from_jwt_cookie(page) == ""

    def test_cookies_call_raising_returns_empty(self):
        # Patchright sometimes raises when the context is being torn down.
        # Mirror the original swallow-and-return-empty behaviour so the
        # caller can decide what to do without an exception bubbling up.
        page = FakePage(cookies_raises=RuntimeError("context closed"))
        assert user_id_from_jwt_cookie(page) == ""

    def test_corrupt_payload_returns_empty(self):
        # Token has the right ``a.b.c`` shape but the middle segment isn't
        # valid base64-encoded JSON. Skip it.
        page = FakePage(
            cookies=[{"name": "access_token_web", "value": "header.@@@.sig"}]
        )
        assert user_id_from_jwt_cookie(page) == ""
