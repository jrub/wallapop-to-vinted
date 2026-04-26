"""Match Vinted drafts back to the Wallapop items that produced them.

Pure logic — operates on dicts of normalised titles. Used by the
``--retry-drafts`` flow in the orchestrator: scrape Vinted's draft list,
normalise each draft title, find the originating Wallapop item by
title equivalence, then re-open the draft and re-attempt publish.
"""

import re
import unicodedata


def normalize_title(title: str | None) -> str:
    """Strip accents/case, collapse non-alphanumerics to single spaces.

    Aggressive on purpose: drafts and Wallapop titles can disagree on
    punctuation, slashes, repeated whitespace, accented characters, and
    case. Reducing both to a flat ascii word stream is what lets the
    matcher rely on simple equality / substring checks.
    """
    s = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def match_draft_to_item(draft_title: str, items: dict) -> tuple[str | None, bool]:
    """Match a Vinted draft title to a Wallapop item id.

    Returns ``(item_id, ambiguous)``. Exact-normalised match wins;
    otherwise a substring match on the longer of (draft, wallapop) title
    is attempted. If two Wallapop items tie at either stage, returns
    ``(None, True)`` so the caller can skip rather than guess.
    """
    dn = normalize_title(draft_title)
    if not dn:
        return None, False
    exact = [iid for iid, it in items.items() if normalize_title(it.get("title", "")) == dn]
    if len(exact) == 1:
        return exact[0], False
    if len(exact) > 1:
        return None, True
    substr = [
        iid
        for iid, it in items.items()
        if (tn := normalize_title(it.get("title", ""))) and (tn in dn or dn in tn)
    ]
    if len(substr) == 1:
        return substr[0], False
    if len(substr) > 1:
        return None, True
    return None, False
