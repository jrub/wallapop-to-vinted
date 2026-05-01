"""Page Object for Vinted's draft-edit form (/items/<id>/edit).

The edit form on ``/items/<id>/edit`` renders the same fields and uses
the same ``data-testid`` selectors as ``/items/new``, so every form verb
(category, condition, package size, brand, publish/draft) is inherited
from :class:`vinted.pages.new_item.NewItemPage` unchanged. Two
edit-specific verbs are added so the orchestrator can interleave a
captcha check between navigation and the form-load wait:

- :meth:`goto` navigates to the edit URL with the standard
  ``wait_until="domcontentloaded"``.
- :meth:`wait_for_form_loaded` returns ``True`` when the title input
  becomes visible, ``False`` on Playwright timeout.
"""

from __future__ import annotations

from patchright.sync_api import TimeoutError as PlaywrightTimeout

from vinted.pages.new_item import NewItemPage


class EditDraftPage(NewItemPage):
    """Form interactions on ``/items/<id>/edit``.

    Reuses every method from :class:`NewItemPage` (the testids are
    identical) and adds the navigate + wait-for-hydrate split so the
    caller can fire a captcha check in between.
    """

    def goto(self, edit_url: str) -> None:
        """Navigate to the draft edit URL with ``wait_until="domcontentloaded"``."""
        self.page.goto(edit_url, wait_until="domcontentloaded")

    def wait_for_form_loaded(self, timeout: int = 30000) -> bool:
        """Wait for the title input to become visible. Returns ``False`` on timeout."""
        try:
            self.page.wait_for_selector(
                "input[data-testid='title--input']", state="visible", timeout=timeout
            )
            return True
        except PlaywrightTimeout:
            return False
