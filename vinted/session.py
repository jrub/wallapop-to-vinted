"""Vinted session helpers — Patchright bootstrap, captcha detection, JWT.

Owns the construction side of the browser session and the small helpers
that work on the running ``page`` object. Page Objects (Phase 4) layer
on top of this; the orchestrator wires the lifecycle.

No module-level state, no environment variables, no path constants — the
caller (``upload_vinted.py`` for now) passes everything in.
"""

import base64
import json
from pathlib import Path
from typing import Tuple

from .errors import CaptchaDetected


# DataDome's challenge is always served as an iframe over the Vinted page.
# A top-level redirect to ``captcha-delivery.com`` was guarded against in
# the original implementation but never observed in production, so the
# detector only checks the iframe.
_CAPTCHA_IFRAME_SELECTOR = "iframe[src*='captcha-delivery.com']"


def is_captcha_present(page) -> bool:
    """Return ``True`` if a DataDome challenge iframe is on the current page.

    Swallows exceptions from the Playwright call (the page can be mid-
    navigation when this is invoked, which makes ``count()`` raise) so a
    transient hiccup never crashes the upload loop.
    """
    try:
        return page.locator(_CAPTCHA_IFRAME_SELECTOR).count() > 0
    except Exception:
        return False


def abort_if_captcha(page, visible: bool) -> None:
    """Raise :class:`CaptchaDetected` when running headless and captcha shows.

    In visible mode this is a no-op — the user is expected to solve the
    slider manually. The orchestrator catches the exception, prints the
    user-facing instructions, and exits with code 2.
    """
    if visible or not is_captcha_present(page):
        return
    raise CaptchaDetected(url=getattr(page, "url", "") or "")


def user_id_from_jwt_cookie(page) -> str:
    """Extract Vinted's numeric user id from the session JWT cookies.

    Vinted stores JWTs in ``access_token_web`` (preferred) and
    ``refresh_token_web`` (fallback). The payload's ``sub`` claim is the
    numeric user id. This is the only DOM/API-free source and survives
    DataDome blocking the XHR endpoints. Returns ``""`` on any failure
    (no cookies, malformed token, non-numeric ``sub``, exception fetching
    cookies) so the caller can decide whether to retry or give up.
    """
    try:
        cookies = page.context.cookies()
    except Exception:
        return ""
    for name in ("access_token_web", "refresh_token_web"):
        tok = next((c["value"] for c in cookies if c["name"] == name), "")
        if not tok or tok.count(".") != 2:
            continue
        payload_b64 = tok.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        except Exception:
            continue
        sub = str(payload.get("sub") or "")
        if sub.isdigit():
            return sub
    return ""


def build_session(
    playwright,
    *,
    visible: bool,
    auth_state_path: Path,
) -> Tuple[object, object, object]:
    """Launch Patchright, restore ``auth_state.json`` if present, return triple.

    Returns ``(browser, context, page)``. The caller drives the post-
    bootstrap navigation, decides whether to log in, and persists the
    storage state. This function only owns the construction of the
    browser/context/page triplet and the patched fingerprint init script.

    Patchright is used instead of stock Playwright so the launch already
    patches the obvious bot-detection signals (Runtime.enable CDP,
    ``navigator.webdriver``, ``--enable-automation``). The init script
    here covers the few fingerprints Patchright doesn't touch
    (``navigator.languages``, ``navigator.plugins``,
    ``navigator.permissions.query``).
    """
    browser = playwright.chromium.launch(
        headless=not visible,
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx_kwargs = dict(
        viewport={"width": 1440, "height": 900},
        locale="es-ES",
        timezone_id="Europe/Madrid",
    )
    if auth_state_path.exists():
        ctx_kwargs["storage_state"] = str(auth_state_path)
    context = browser.new_context(**ctx_kwargs)
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es'] });
        Object.defineProperty(navigator, 'plugins', {
            get: () => { const p = [1,2,3,4,5]; p.item = () => null; return p; }
        });
        const origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (params) =>
            params.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : origQuery(params);
        """
    )
    page = context.new_page()
    return browser, context, page
