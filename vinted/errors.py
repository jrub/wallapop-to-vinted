"""Vinted-side error taxonomy.

Three failure modes the upload flow needs to react to are now first-class
exceptions instead of `sys.exit`/return-tuple/generic ``RuntimeError`` mixes:

- :class:`CaptchaDetected` — DataDome iframe is blocking the page.
- :class:`LoginFailed` — login finished without redirecting away from the
  auth pages (timeout, invalid credentials, or a captcha that the visible
  user didn't solve in time).
- :class:`PublishRejected` — Vinted refused the listing (missing required
  field, unknown dropdown value); the orchestrator typically falls back to
  saving as a draft.

All three inherit from :class:`VintedError` so callers can catch the whole
family with one ``except`` clause.
"""


class VintedError(Exception):
    """Base for every Vinted-side failure surfaced by the session/page layer."""


class CaptchaDetected(VintedError):
    """A DataDome challenge iframe is present on the current page.

    Raised by :func:`vinted.session.abort_if_captcha` when running headless.
    The orchestrator catches this and tells the user to re-run with
    ``--visible`` so they can solve the slider manually.
    """

    def __init__(self, url: str = ""):
        super().__init__(f"Captcha detected at {url}" if url else "Captcha detected")
        self.url = url


class LoginFailed(VintedError):
    """Login finished without redirecting away from the auth pages."""

    def __init__(self, reason: str, url: str = ""):
        super().__init__(f"Login failed: {reason}")
        self.reason = reason
        self.url = url


class PublishRejected(VintedError):
    """Vinted rejected the listing submission.

    ``errors`` is the list of validation messages scraped from the form
    (e.g. ``["Falta marca", "Falta talla"]``). May be empty when Vinted
    only surfaces a generic toast without per-field details.
    """

    def __init__(self, errors: list[str] | None = None):
        self.errors = list(errors) if errors else []
        msg = "; ".join(self.errors) if self.errors else "publish rejected"
        super().__init__(msg)
