"""Tests for the Vinted error taxonomy.

Smoke-level: each class instantiates with the documented kwargs, exposes them
as attributes, and inherits from ``VintedError`` so ``except VintedError``
catches all three. This locks the public contract before Phase 4 page objects
start raising the concrete classes from the publish/login flows.
"""

import pytest

from vinted.errors import (
    CaptchaDetected,
    LoginFailed,
    PublishRejected,
    VintedError,
)


class TestVintedError:
    def test_is_an_exception(self):
        assert issubclass(VintedError, Exception)

    def test_can_be_raised(self):
        with pytest.raises(VintedError):
            raise VintedError("boom")


class TestCaptchaDetected:
    def test_inherits_from_vinted_error(self):
        assert issubclass(CaptchaDetected, VintedError)

    def test_stores_url(self):
        err = CaptchaDetected(url="https://www.vinted.es/items/new")
        assert err.url == "https://www.vinted.es/items/new"

    def test_url_defaults_to_empty_string(self):
        # Some call-sites won't have a URL handy (e.g. constructed from a
        # locator check before any navigation finished). Empty string keeps
        # the attribute always-readable without ``hasattr`` dances.
        assert CaptchaDetected().url == ""


class TestLoginFailed:
    def test_inherits_from_vinted_error(self):
        assert issubclass(LoginFailed, VintedError)

    def test_stores_reason_and_url(self):
        err = LoginFailed(reason="timeout", url="https://www.vinted.es/member/login")
        assert err.reason == "timeout"
        assert err.url == "https://www.vinted.es/member/login"

    def test_url_optional(self):
        err = LoginFailed(reason="invalid credentials")
        assert err.reason == "invalid credentials"
        assert err.url == ""


class TestPublishRejected:
    def test_inherits_from_vinted_error(self):
        assert issubclass(PublishRejected, VintedError)

    def test_stores_validation_errors(self):
        msgs = ["Falta marca", "Falta talla"]
        err = PublishRejected(errors=msgs)
        assert err.errors == msgs

    def test_errors_defaults_to_empty_list(self):
        # The publish flow occasionally rejects without surfacing per-field
        # messages (a generic toast); the orchestrator should still be able
        # to ``raise PublishRejected()`` and inspect ``.errors`` safely.
        err = PublishRejected()
        assert err.errors == []


class TestTaxonomyDistinguishability:
    def test_each_class_is_distinct(self):
        cap = CaptchaDetected()
        login = LoginFailed(reason="x")
        pub = PublishRejected()
        assert isinstance(cap, CaptchaDetected) and not isinstance(cap, LoginFailed)
        assert isinstance(login, LoginFailed) and not isinstance(login, PublishRejected)
        assert isinstance(pub, PublishRejected) and not isinstance(pub, CaptchaDetected)

    def test_all_caught_by_vinted_error(self):
        for err in (CaptchaDetected(), LoginFailed(reason="x"), PublishRejected()):
            assert isinstance(err, VintedError)
