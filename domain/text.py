"""Pure string helpers used across the upload flow.

These have no I/O and no module-level state — safe to import anywhere.
"""

import re
import unicodedata


def normalize_label(s: str | None) -> str:
    """Lowercase, strip accents, normalise separators, collapse whitespace.

    Used both for comparing Vinted labels against Wallapop attribute keys and
    for comparing dropdown options against item values. Accepts ``None`` and
    returns an empty string — some callers pass values straight from dicts.
    """
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def soften_title_caps(title: str) -> str:
    """Always lowercase titles (with first letter capitalised) for Vinted's validator.

    Vinted rejects titles with "too many uppercase letters" ("El título contiene demasiadas
    mayúsculas") and the threshold is conservative enough that even mixed-case titles like
    "Libros SUSE LINUX oficial certificación" trip it. Rather than guess the exact ratio,
    we just lowercase every title — Vinted's UI capitalises display anyway. The user can
    re-edit the title in the draft if the casing matters for a specific listing.
    """
    if not title:
        return title
    return title.lower().capitalize()


def find_option_match(options: list[str], value: str, strict: bool = False) -> str | None:
    """Find the best matching option from a dropdown option list.

    Exact normalized match wins; substring match is the fallback (unless strict=True).
    Pass strict=True for typed autocomplete inputs (e.g. Brand) where Vinted filters
    results based on what was typed — a non-exact result means the value isn't in the
    catalogue, so substring matches would be false positives.
    """
    norm_value = normalize_label(value)
    if not norm_value:
        return None
    for opt in options:
        if normalize_label(opt) == norm_value:
            return opt
    if strict:
        return None
    for opt in options:
        no = normalize_label(opt)
        if no and (no in norm_value or norm_value in no):
            return opt
    return None


def stem(word: str) -> str:
    """Crude Spanish/English plural stemmer: 'routers'→'router', 'módems'→'modem'.

    Good enough for matching Vinted leaf labels against item titles. Not a full
    stemmer — just chops a trailing 's' or 'es' if the remainder is long enough
    to avoid butchering short words.
    """
    w = normalize_label(word)
    for suffix in ("es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w
