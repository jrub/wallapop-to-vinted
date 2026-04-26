"""Pure URL helpers used by the Vinted upload flow.

No I/O, no network — just regex parsing of Vinted URL shapes. The
functions live in ``domain/`` (not ``vinted/``) because they don't
depend on the browser session and are easily verifiable in isolation.
"""

import re

_ITEM_ID_RE = re.compile(r"/items/(\d+)")
_FORM_URL_RE = re.compile(r"/items/(new|\d+/edit)")


def extract_item_id_from_url(url: str) -> str:
    """Pull the Vinted item ID out of any post-upload URL, or ``""`` if absent."""
    match = _ITEM_ID_RE.search(url or "")
    return match.group(1) if match else ""


def is_form_url(url: str) -> bool:
    """``True`` while the URL still shows a Vinted item form (create or edit).

    A successful publish or draft save navigates away from these paths
    (to ``/items/<id>`` or ``/member/<id>``), so leaving a form URL is
    our signal that the submit succeeded.
    """
    return bool(_FORM_URL_RE.search(url or ""))
