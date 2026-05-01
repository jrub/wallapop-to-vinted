"""Page Object for Vinted's login flow (/member/signup/select_type).

Handles cookie-banner dismissal, register→login view switch, email/password
fill, and submit. Every click goes through ``js_click_button`` (synthetic
DOM event) rather than native ``locator.click()``: native clicks issue CDP
``Input.dispatchMouseEvent`` commands that DataDome inspects more
aggressively than plain JS events, and once the flow used native clicks
every login attempt tripped the slider and the IP got blocked.

Raises :class:`vinted.errors.LoginFailed` when the URL doesn't leave the
auth pages, and :class:`vinted.errors.CaptchaDetected` (via
:func:`vinted.session.abort_if_captcha`) when a DataDome challenge
persists in headless mode.
"""

from __future__ import annotations

from patchright.sync_api import TimeoutError as PlaywrightTimeout

from vinted.errors import LoginFailed
from vinted.pages._common import human_delay, human_type, js_click_button
from vinted.session import abort_if_captcha

# URL fragments that mark "still on an auth page". The login is complete
# when the URL no longer contains any of these.
_AUTH_PATHS = ("/member/signup", "/member/login", "/member/verify", "/oauth", "/auth/")


class LoginPage:
    """DOM interaction layer for Vinted's login form.

    The page opens on the register view by default; this object switches
    to the login view, fills credentials, and submits. All clicks are
    funneled through ``js_click_button`` to evade DataDome's CDP-mouse-
    event detection.
    """

    def __init__(self, page, base_url: str = "https://www.vinted.es"):
        self.page = page
        self.base_url = base_url

    def login(self, email: str, password: str, visible: bool = True) -> None:
        """Run the login flow end-to-end.

        In ``visible`` mode the post-submit wait is 2 minutes so the user
        can solve a DataDome slider manually. In headless it's 20 s — the
        URL must settle on its own.
        """
        print("Logging in to Vinted...")
        # domcontentloaded — not networkidle. Vinted's tracking/telemetry keeps
        # XHRs alive long past initial paint, so networkidle reliably times
        # out at 30 s. The per-step wait_for_selector calls below gate progress.
        self.page.goto(
            f"{self.base_url}/member/signup/select_type",
            wait_until="domcontentloaded",
        )
        human_delay(1, 2)

        self._dismiss_cookie_banner()
        self._switch_to_login_view()
        self._fill_credentials(email, password)
        self._submit_and_wait(visible)

    def _dismiss_cookie_banner(self) -> None:
        try:
            btn = self.page.locator("#onetrust-accept-btn-handler")
            btn.wait_for(state="visible", timeout=5000)
            js_click_button(self.page, btn)
            human_delay(0.5, 1)
        except Exception:
            pass

    def _switch_to_login_view(self) -> None:
        sw = self.page.get_by_test_id("auth-select-type--register-switch")
        js_click_button(self.page, sw)
        self.page.wait_for_selector("[data-testid='auth-select-type--login-email']")
        human_delay(0.8, 1.5)

        email_btn = self.page.get_by_test_id("auth-select-type--login-email")
        js_click_button(self.page, email_btn)
        self.page.wait_for_selector("input[type='password']")
        human_delay(0.8, 1.5)

    def _fill_credentials(self, email: str, password: str) -> None:
        human_type(self.page.get_by_placeholder("Nombre de usuario o e-mail"), email)
        human_delay(0.5, 1)
        human_type(self.page.locator("input[type='password']"), password)
        human_delay(0.8, 1.5)

    def _submit_and_wait(self, visible: bool) -> None:
        # Vinted's login button is labelled "Continuar" with an onClick handler
        # rather than type=submit. Locate by accessible name; exact=True so we
        # don't catch "Continuar con Apple/Google/Facebook" if rendered.
        submit = self.page.get_by_role("button", name="Continuar", exact=True)
        js_click_button(self.page, submit)

        if visible:
            print("  Waiting... solve the slider in the browser if it appears.")

        try:
            self.page.wait_for_url(
                lambda url: "vinted.es" in url and not any(p in url for p in _AUTH_PATHS),
                timeout=120000 if visible else 20000,
            )
        except PlaywrightTimeout:
            abort_if_captcha(self.page, visible)
            raise LoginFailed(reason="timeout waiting for redirect", url=self.page.url)
        abort_if_captcha(self.page, visible)
        print(f"  Login OK — {self.page.url}")
