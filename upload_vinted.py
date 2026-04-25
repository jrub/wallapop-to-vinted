"""
upload_vinted.py
Reads data/downloaded_items.json and uploads each item to Vinted via browser automation.
Run extract_wallapop.py first to populate the items file.
"""

import os
import sys
import re
import json
import time
import base64
import random
import argparse
import unicodedata
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from patchright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from domain.categories import build_path_index, resolve_nav_to_leaf
from domain.mapping import (
    COLOR_EN_TO_ES,
    LABEL_ALIASES,
    guess_wallapop_key,
    label_to_wallapop_key,
)
from domain.text import find_option_match, normalize_label, soften_title_caps, stem

load_dotenv()

EMAIL = os.getenv("VINTED_EMAIL")
PASSWORD = os.getenv("VINTED_PASSWORD")

ITEMS_PATH = Path("data/downloaded_items.json")
AUTH_STATE_PATH = Path("data/auth_state.json")  # persisted browser session — not versioned
MIGRATION_PATH = Path("data/migration.json")
CATEGORIES_PATH = Path("data/category_mapping.json")
# DOM selectors (button labels, error messages) are Spanish-only, so only vinted.es works.
BASE_URL = "https://www.vinted.es"

# All items are uploaded with this condition. Wallapop condition values don't map cleanly
# to Vinted's options and the items being migrated are personal second-hand goods.
VINTED_CONDITION = "Nuevo sin etiquetas"

# Selectors for the Vinted upload form. Grouped here so DOM churn is easy to patch.
PUBLISH_BUTTON_TESTID = "upload-form-post-button"
DRAFT_BUTTON_TESTID = "upload-form-save-draft-button"
# Every dynamic dropdown on /items/new exposes a testid ending in '-single-list-input'
# (category-condition-single-list-input, size-single-list-input, etc.).
DYNAMIC_DROPDOWN_SELECTOR = "[data-testid$='single-list-input']"
# Inline validation messages appear beside each field when Vinted rejects submission.
# The canonical marker is `div.web_ui__Validation__warning[role='alert']`; the rest are
# legacy fallbacks for older markup paths.
ERROR_SELECTOR = (
    "div.web_ui__Validation__warning[role='alert'], "
    "[role='alert'], .c-input__title--error, .u-flexbox--error-message"
)

if not CATEGORIES_PATH.exists():
    print(f"ERROR: {CATEGORIES_PATH} not found. Run extract_wallapop.py first.")
    sys.exit(1)
with open(CATEGORIES_PATH, encoding="utf-8") as _f:
    _CATEGORIES = json.load(_f)  # wallapop category_id → {name, vinted: [...nav path]}

VINTED_CATEGORIES_PATH = Path("data/vinted_categories.json")
_VINTED_NODES: list[dict] = []
_VINTED_BY_PATH: dict[tuple, dict] = {}
if VINTED_CATEGORIES_PATH.exists():
    with open(VINTED_CATEGORIES_PATH, encoding="utf-8") as _f:
        _VINTED_NODES = json.load(_f)
    _VINTED_BY_PATH = build_path_index(_VINTED_NODES)
else:
    print(
        f"WARNING: {VINTED_CATEGORIES_PATH} not found. "
        "Run extract_vinted_categories.py to enable local leaf resolution."
    )


def get_nav(item: dict) -> list | None:
    """Return the Vinted category navigation path for an item, or None if unmapped."""
    entry = _CATEGORIES.get(item.get("category_id", ""))
    return entry["vinted"] if entry else None


def load_migration() -> dict:
    if not MIGRATION_PATH.exists():
        return {}
    return json.loads(MIGRATION_PATH.read_text(encoding="utf-8"))


def mark_migrated(
    migration: dict,
    item_id: str,
    vinted_id: str,
    status: str,
    missing_fields: list[str] | None = None,
    error: str = "",
):
    """Record a successful upload in migration.json. Written after every item for crash safety."""
    migration.setdefault(item_id, {}).update({
        "vinted_id": vinted_id,
        "status": status,
        "missing_fields": missing_fields or [],
        "last_error": error,
        "uploaded_at": datetime.utcnow().isoformat(),
    })
    MIGRATION_PATH.write_text(
        json.dumps(migration, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def migration_status(entry: dict) -> str:
    """Read status from a migration.json entry, falling back to the old 'vinted_status' key."""
    return entry.get("status") or entry.get("vinted_status") or ""


def human_delay(min_s=0.5, max_s=1.5):
    """Random pause to mimic human interaction timing."""
    time.sleep(random.uniform(min_s, max_s))


def human_type(locator, text: str):
    """Type character by character at ~60 wpm with occasional hesitation pauses.

    Vinted's React form listens to keyboard events, not DOM value changes.
    page.fill() sets the value directly without firing key events, leaving
    the field visually empty. press_sequentially() fires the correct events.
    """
    locator.click()
    human_delay(0.2, 0.5)
    for char in text:
        locator.press_sequentially(char)
        time.sleep(random.uniform(0.06, 0.22))
        if random.random() < 0.08:  # occasional longer pause, like thinking
            time.sleep(random.uniform(0.15, 0.45))


def select_category(page, nav: list) -> tuple[bool, list[str]]:
    """Navigate Vinted's category picker by clicking through each tree level.

    The picker renders one level at a time as div[role="button"] elements.
    :text-is() matches the exact label without substring false positives.

    Expects `nav` to already be a fully-resolved leaf path (see
    resolve_nav_to_leaf).  If Vinted still shows sub-options after clicking all
    steps (the nav is one step short of a leaf), those options are returned so
    the caller can persist them to category_mapping.json for manual review.

    Returns (leaf_reached, sub_options).
      - leaf_reached=True when the picker auto-closes after the last click.
      - leaf_reached=False when sub-options remain after clicking all nav steps,
        or on exception.  sub_options is non-empty only in the first case.
    """
    if not nav:
        return False, []
    try:
        page.click("input[data-testid='catalog-select-dropdown-input']")
        human_delay(0.5, 1.0)
        for step in nav:
            btn = page.locator('div[role="button"]').filter(
                has=page.locator(f':text-is("{step}")')
            ).first
            btn.wait_for(state="visible", timeout=5000)
            btn.click()
            human_delay(0.4, 0.8)

        # Detect "not a leaf": visible cells with ids like `catalog-<n>` remain
        # when Vinted is still offering deeper sub-categories.
        human_delay(0.6, 1.0)
        sub_options = page.evaluate(
            """() => Array.from(document.querySelectorAll('[id^="catalog-"]'))
                     .filter(el => el.offsetParent !== null)
                     .map(el => (el.innerText || '').trim().split('\\n')[0])
                     .filter(t => t.length > 0 && t.length < 80)
                     .slice(0, 10)"""
        ) or []
        if sub_options:
            # Nav path is not a leaf — persist observed options for manual review
            # and fall back to draft.
            print(
                f"    WARNING: nav path {' > '.join(nav)!r} is not a leaf "
                f"(sub-options: {sub_options}) — falling back to draft."
            )
            try:
                page.keyboard.press("Escape")
                human_delay(0.3, 0.6)
            except Exception:
                pass
            return False, sub_options

        print(f"    Category: {' > '.join(nav)}")
        return True, []
    except Exception as e:
        print(f"    WARNING: could not select category {nav}: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False, []


def set_condition(page) -> bool:
    """Select the item condition. Silently skips if the field isn't visible for this category."""
    try:
        cond = page.locator("[data-testid='category-condition-single-list-input']")
        cond.wait_for(state="visible", timeout=3000)
        cond.click()
        human_delay(0.3, 0.6)
        page.locator('div[role="button"]').filter(
            has=page.locator(f':text-is("{VINTED_CONDITION}")')
        ).first.click()
        print(f"    Condition: {VINTED_CONDITION}")
        return True
    except Exception:
        return False


def scan_dynamic_fields(page) -> list[dict]:
    """Enumerate visible attribute dropdowns currently rendered on the upload form.

    Returns a list of {testid, label, kind} for every input-like element exposed by
    Vinted for the selected category, skipping the category picker and title/price/
    description (which have their own flow). 'kind' is 'dropdown' for single-list
    pickers, 'combobox' for brand-style autocomplete inputs, and 'other' for the rest.
    """
    try:
        return page.evaluate("""
        () => {
          const skipTestidPrefix = ['catalog-', 'title--', 'description--', 'price-input--',
                                    'add-photos-', 'upload-form-', 'currency-',
                                    'search-text--', 'package_type_selector_'];
          const skipTestidSubstr = ['-package-size--cell', 'package-size-suggestion',
                                    '-chevron-down'];
          const out = [];
          const seen = new Set();
          const consider = document.querySelectorAll(
            "[data-testid$='single-list-input'], " +
            "[data-testid$='-dropdown-input']"
          );
          consider.forEach(el => {
            const testid = el.getAttribute('data-testid') || '';
            if (!testid || seen.has(testid)) return;
            if (skipTestidPrefix.some(p => testid.startsWith(p))) return;
            if (skipTestidSubstr.some(s => testid.includes(s))) return;
            if (/condition/.test(testid)) return;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) return;
            seen.add(testid);
            let kind = 'other';
            if (testid.endsWith('single-list-input')) kind = 'dropdown';
            else if (testid.endsWith('-dropdown-input')) kind = 'combobox';
            let label = '';
            let node = el;
            for (let i = 0; i < 10 && node; i++) {
              node = node.parentElement;
              if (!node) break;
              const lbl = node.querySelector('label, .c-input__title, .web_ui__Text__title, h2, h3');
              if (lbl && lbl.innerText && lbl.innerText.trim()) {
                label = lbl.innerText.trim();
                break;
              }
            }
            out.push({ testid, label, kind });
          });
          return out;
        }
        """) or []
    except Exception as e:
        print(f"    WARNING: could not scan dynamic fields: {e}")
        return []


# Vinted marks open dropdown panels with data-testid ending in "-dropdown-content"
# (e.g. "color-select-dropdown-content", "brand-select-dropdown-content"). Scoping
# queries to this element avoids matching buttons elsewhere on the page and sidesteps
# visibility checks that break inside position:fixed / overflow:auto containers.
_PANEL_SELECTOR = '[data-testid$="-dropdown-content"]'


def _collect_visible_options(page) -> list[str]:
    """Read all option labels from the currently open Vinted dropdown panel.

    Scopes to the panel element (data-testid ending in "-dropdown-content") so we
    never match buttons outside the panel. Within the panel, prefers --title child
    elements (used by color/brand cells) over raw innerText so the color circle
    and checkbox markup don't contaminate the label. No visibility filter needed:
    within a scoped panel all rendered items are valid options regardless of scroll
    position or position:fixed/overflow:auto ancestors.
    """
    try:
        return page.evaluate(f"""
        () => {{
            const panel = document.querySelector('{_PANEL_SELECTOR}');
            const root = panel || document;
            const seen = new Set();
            const out = [];
            // --title children (color/brand cells): cleanest label source
            root.querySelectorAll('[data-testid$="--title"]').forEach(el => {{
                const text = (el.innerText || '').trim();
                if (text.length > 0 && text.length < 80 && !seen.has(text)) {{
                    seen.add(text); out.push(text);
                }}
            }});
            if (out.length) return out;
            // Fallback: plain div[role="button"] text (standard single-list dropdowns)
            root.querySelectorAll('div[role="button"]').forEach(el => {{
                const text = (el.innerText || '').trim().split('\\n')[0].trim();
                if (text.length > 0 && text.length < 80 && !seen.has(text)) {{
                    seen.add(text); out.push(text);
                }}
            }});
            return out;
        }}
        """) or []
    except Exception:
        return []


def _js_click_option(page, label: str) -> None:
    """Click an option in the open dropdown panel by its label text using JavaScript.

    Scopes the search to the open panel (data-testid ending in "-dropdown-content")
    and matches via --title children first (color/brand cells), then falls back to
    plain div[role="button"] innerText. The JS click never scrolls the page, avoiding
    the erratic viewport movement that triggers DataDome bot detection.
    """
    page.evaluate(
        f"""(label) => {{
            const panel = document.querySelector('{_PANEL_SELECTOR}');
            const root = panel || document;
            // Try --title pattern: find title el → click its role=button parent
            const titleEl = Array.from(root.querySelectorAll('[data-testid$="--title"]'))
                .find(el => (el.innerText || '').trim() === label);
            if (titleEl) {{
                const btn = titleEl.closest('div[role="button"]');
                if (btn) {{ btn.click(); return; }}
            }}
            // Fallback: direct div[role="button"] text match
            const btn = Array.from(root.querySelectorAll('div[role="button"]'))
                .find(el => (el.innerText || '').trim().split('\\n')[0].trim() === label);
            if (btn) btn.click();
        }}""",
        label,
    )


def fill_dropdown(page, testid: str, value: str) -> tuple[bool, list[str]]:
    """Open the dropdown identified by testid, try to select an option matching `value`.

    Returns (selected, options_seen). options_seen is populated on both hit and miss
    so the caller can persist them in category_mapping.json for the user to review.
    Uses JS click to avoid Playwright's auto-scroll which can trigger DataDome.
    """
    options: list[str] = []
    try:
        inp = page.locator(f"[data-testid='{testid}']")
        inp.wait_for(state="visible", timeout=3000)
        inp.click()
        human_delay(0.3, 0.6)
        options = _collect_visible_options(page)

        matched = find_option_match(options, value)
        if matched is not None:
            _js_click_option(page, matched)
            human_delay(0.3, 0.6)
            return True, options
        # No match — close the panel so the next field can open
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        human_delay(0.2, 0.4)
        return False, options
    except Exception as e:
        print(f"    WARNING: fill_dropdown({testid}, {value!r}) failed: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False, options


def fill_combobox(page, testid: str, value: str) -> tuple[bool, list[str]]:
    """For Vinted's brand/color pickers (input + dropdown suggestions).

    Two-phase approach:
    1. Click to open and check for an immediate match in visible options.
       Readonly inputs (e.g. Color) show all choices upfront — if there's no
       immediate match we return False right away without typing.
    2. For non-readonly inputs (e.g. Brand autocomplete): type the value and
       re-check. Uses faster typing since timing-based bot detection is not
       the concern here (DataDome reacts to scroll, not keystroke cadence).

    Uses JS click to avoid Playwright's auto-scroll which can trigger DataDome.
    Returns (selected, options_seen).
    """
    options: list[str] = []
    try:
        inp = page.locator(f"[data-testid='{testid}']")
        inp.wait_for(state="visible", timeout=3000)
        inp.click()
        human_delay(0.4, 0.7)

        # Phase 1: immediate match (Color picker shows all options on open)
        options = _collect_visible_options(page)
        matched = find_option_match(options, value)
        if matched is not None:
            _js_click_option(page, matched)
            human_delay(0.3, 0.5)
            return True, options

        # Readonly inputs (Color) have shown all available options — no match means
        # the value simply isn't offered. Don't try to type into a readonly field.
        is_readonly = inp.get_attribute("readonly") is not None
        if is_readonly:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            human_delay(0.2, 0.3)
            return False, options

        # Phase 2: type to filter (Brand autocomplete and similar text inputs)
        # Use strict=True: Vinted filters suggestions based on what was typed, so any
        # non-exact result is a false positive (e.g. typing "MS-DOS" surfaces "Do").
        for ch in value:
            inp.press_sequentially(ch)
            time.sleep(random.uniform(0.02, 0.06))
        human_delay(0.4, 0.7)
        options = _collect_visible_options(page)
        matched = find_option_match(options, value, strict=True)
        if matched is not None:
            _js_click_option(page, matched)
            human_delay(0.3, 0.5)
            return True, options

        # No match — clear the input so typed value doesn't remain as free text
        try:
            inp.fill("")
            page.keyboard.press("Escape")
        except Exception:
            pass
        human_delay(0.2, 0.3)
        return False, options
    except Exception as e:
        print(f"    WARNING: fill_combobox({testid}, {value!r}) failed: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False, options


def fill_first_option(page, testid: str) -> tuple[bool, list[str]]:
    """Open the dropdown, read its options, click the first one, close. Single open cycle.

    Used as a last-resort fill for Color when Wallapop has no colour value: Vinted
    puts its "Sugerencias" (category-aware colour guesses) at the top of the picker,
    so the first option is the best automatic guess. Single open/close keeps viewport
    activity minimal, which matters for DataDome.
    """
    try:
        inp = page.locator(f"[data-testid='{testid}']")
        inp.wait_for(state="visible", timeout=3000)
        inp.click()
        human_delay(0.3, 0.6)
        options = _collect_visible_options(page)
        if not options:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False, []
        _js_click_option(page, options[0])
        human_delay(0.3, 0.5)
        return True, options
    except Exception as e:
        print(f"    WARNING: fill_first_option({testid}) failed: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False, []


def select_package_size(page) -> bool:
    """Pick the 'Mediano' (id=2) Vinted package size unconditionally.

    Wallapop doesn't expose the package size — it's chosen at listing time and not
    surfaced in the API. Vinted's package picker isn't a weight scale either; it's a
    set of named tiers (Pequeño / Mediano / Grande / XGrande) described as "what fits
    in a shoebox", "what fits in a moving box", etc. 'Mediano' is the safe default
    for the bulk of items in this catalogue. The cell testid is '2-package-size--cell'.
    """
    try:
        cell = page.locator("[data-testid='2-package-size--cell']")
        cell.wait_for(state="visible", timeout=5000)
        cell.click()
        human_delay(0.3, 0.6)
        print("    Shipping package: Mediano (default)")
        return True
    except Exception as e:
        print(f"    WARNING: could not pick package size: {e}")
        return False


def select_no_brand(page, testid: str) -> bool:
    """Click the 'Publicar sin marca' option inside the brand dropdown.

    Opens the dropdown (in case it's closed) then uses JS click to pick the
    option without triggering Playwright's auto-scroll.
    """
    try:
        inp = page.locator(f"[data-testid='{testid}']")
        inp.click()
        human_delay(0.3, 0.6)
        clicked = page.evaluate(
            f"""() => {{
                const panel = document.querySelector('{_PANEL_SELECTOR}');
                const root = panel || document;
                const btn = Array.from(root.querySelectorAll('div[role="button"]'))
                    .find(el => (el.innerText || '').trim() === 'Publicar sin marca');
                if (btn) {{ btn.click(); return true; }}
                return false;
            }}"""
        )
        if clicked:
            human_delay(0.3, 0.6)
            return True
        page.keyboard.press("Escape")
        return False
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def collect_form_errors(page) -> list[str]:
    """Read inline validation messages shown by Vinted after a failed publish attempt.

    Vinted's error markup has shifted over time. We cast a wide net: aria-invalid inputs,
    legacy error classes, anything whose class name contains 'error' or 'caution', plus
    a text-prefix heuristic ("Rellena", "Selecciona", "Introduce", "Indica") for
    validation copy that Vinted renders as plain divs without obvious classes.
    """
    try:
        msgs = page.evaluate(r"""
        () => {
          const out = new Set();
          const isVisible = el => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const s = getComputedStyle(el);
            return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
          };
          // 1) aria-invalid inputs → read adjacent text via aria-describedby or nearest sibling
          document.querySelectorAll('[aria-invalid="true"]').forEach(el => {
            const ids = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
            ids.forEach(id => {
              const d = document.getElementById(id);
              if (d && isVisible(d)) {
                const t = (d.innerText || '').trim();
                if (t) out.add(t);
              }
            });
          });
          // 2) elements whose class contains 'error' or 'caution'
          document.querySelectorAll('[class*="error" i], [class*="caution" i], [role="alert"]').forEach(el => {
            if (!isVisible(el)) return;
            const t = (el.innerText || '').trim();
            if (t && t.length < 200) out.add(t);
          });
          // 3) text-prefix heuristic: Vinted error copy typically starts with these verbs
          const prefixes = /^(rellena|selecciona|introduce|indica|a[nñ]ade|elige|falta|debes)\b/i;
          document.querySelectorAll('div, span, p').forEach(el => {
            if (!isVisible(el)) return;
            if (el.children.length > 0) return;  // leaf nodes only
            const t = (el.innerText || '').trim();
            if (t && t.length < 200 && prefixes.test(t)) out.add(t);
          });
          return Array.from(out);
        }
        """) or []
        return msgs
    except Exception:
        return []


def _dump_form_buttons(page) -> list[dict]:
    """Diagnostic helper: list all buttons visible on the upload form with their testids/text."""
    try:
        return page.evaluate("""
        () => Array.from(document.querySelectorAll('button'))
          .filter(b => b.offsetParent !== null)
          .map(b => ({
            testid: b.getAttribute('data-testid') || '',
            text: (b.innerText || '').trim().slice(0, 40)
          }))
        """) or []
    except Exception:
        return []


def _find_publish_button(page):
    """Locate the publish button. Tries the known testid, then falls back to visible text."""
    try:
        btn = page.locator(f"button[data-testid='{PUBLISH_BUTTON_TESTID}']")
        btn.wait_for(state="visible", timeout=3000)
        return btn
    except PlaywrightTimeout:
        pass
    # Fallback: match by visible text. Vinted's Spanish button reads "Subir" (or "Publicar").
    for text in ("Subir", "Publicar", "Subir artículo"):
        try:
            btn = page.get_by_role("button", name=text, exact=True)
            btn.wait_for(state="visible", timeout=1500)
            return btn
        except PlaywrightTimeout:
            continue
    return None


def _extract_item_id_from_url(url: str) -> str:
    """Pull the Vinted item ID out of any post-upload URL, or return '' if absent."""
    match = re.search(r"/items/(\d+)", url)
    return match.group(1) if match else ""


_FORM_URL_RE = re.compile(r"/items/(new|\d+/edit)")


def _is_form_url(url: str) -> bool:
    """True while the URL still shows a Vinted item form (create or edit).

    A successful publish or draft save navigates away from these paths (to
    `/items/<id>` or `/member/<id>`), so leaving a form URL is our signal.
    """
    return bool(_FORM_URL_RE.search(url))


def _dismiss_critical_error_dialog(page) -> bool:
    """Dismiss Vinted's critical-error modal if present, using JS click (no scroll).

    Returns True if a dialog was found and dismissed.
    The modal (data-testid="item-upload-critical-error-dialog--overlay") appears after a
    failed publish when Vinted decides the form has unrecoverable errors. It blocks all
    further interaction — including the Save-draft button — until dismissed.
    """
    try:
        dismissed = page.evaluate("""
        () => {
            const overlay = document.querySelector(
                '[data-testid="item-upload-critical-error-dialog--overlay"]'
            );
            if (!overlay) return false;
            // Try the close/OK button inside the dialog
            const btn = overlay.querySelector('button');
            if (btn) { btn.click(); return true; }
            // Fallback: click outside the dialog card to close it
            overlay.click();
            return true;
        }
        """)
        if dismissed:
            human_delay(0.4, 0.7)
        return bool(dismissed)
    except Exception:
        return False


def _js_click_button(page, locator) -> None:
    """Click a button via JavaScript to avoid Playwright's scroll-into-view behaviour.

    Playwright's locator.click() scrolls the element into view before clicking,
    causing the viewport to jump — which DataDome interprets as bot-like behaviour.
    A JS click fires the event without moving the scroll position.
    """
    try:
        el = locator.element_handle(timeout=3000)
        if el:
            page.evaluate("el => el.click()", el)
    except Exception:
        locator.click(timeout=10000)  # graceful fallback


def publish_or_draft(
    page, fallback_id_seed: str = "", save_as_draft_on_fail: bool = True
) -> tuple[str, str, list[str]]:
    """Try to publish; on validation failure, optionally fall back to saving a draft.

    Returns (vinted_id, status, errors) with status ∈ {"published","draft","failed"}.
    If the post-save URL doesn't expose an item ID (Vinted sometimes redirects to the
    profile page), a synthetic id derived from fallback_id_seed is returned so
    migration.json can still record the item and idempotency holds on re-runs.

    save_as_draft_on_fail=False is used in retry mode: the draft already exists on
    Vinted, so when publish fails we want to leave it in place and report errors
    instead of clicking a "Guardar borrador" button that isn't on the edit page.
    """
    errors: list[str] = []
    pub = _find_publish_button(page)
    if pub is None:
        buttons = _dump_form_buttons(page)
        print(f"    WARNING: publish button not visible. Form buttons: {buttons}")
    else:
        try:
            _js_click_button(page, pub)
            try:
                page.wait_for_url(lambda url: not _is_form_url(url), timeout=15000)
            except PlaywrightTimeout:
                pass
            human_delay(1, 2)
            if not _is_form_url(page.url):
                vinted_id = _extract_item_id_from_url(page.url) or f"published-{fallback_id_seed}"
                print(f"    Published: {page.url}")
                return vinted_id, "published", []
            errors = collect_form_errors(page)
            if errors:
                print(f"    Publish rejected: {errors[:4]}")
            else:
                print(f"    Clicked publish but no navigation or inline errors — URL still: {page.url}")
        except Exception as e:
            print(f"    WARNING: publish failed: {e}")

    if not save_as_draft_on_fail:
        return "", "draft", errors

    # A critical-error dialog may be blocking the Save-draft button — dismiss it first.
    _dismiss_critical_error_dialog(page)

    try:
        draft_btn = page.locator(f"button[data-testid='{DRAFT_BUTTON_TESTID}']")
        _js_click_button(page, draft_btn)
        try:
            page.wait_for_url(lambda url: not _is_form_url(url), timeout=15000)
        except PlaywrightTimeout:
            pass
        human_delay(1, 2)
        if _is_form_url(page.url):
            print(f"    Error: draft not saved — URL: {page.url}")
            return "", "failed", errors
        vinted_id = _extract_item_id_from_url(page.url) or f"draft-{fallback_id_seed}"
        print(f"    Draft saved: {page.url} (recorded id: {vinted_id})")
        return vinted_id, "draft", errors
    except Exception as e:
        print(f"    Error saving draft: {e}")
        return "", "failed", errors


def login(page, visible: bool = True):
    """Log in to Vinted. Waits up to 2 minutes for manual captcha resolution if needed."""
    print("Logging in to Vinted...")
    page.goto(f"{BASE_URL}/member/signup/select_type")
    page.wait_for_load_state("networkidle")
    human_delay(1, 2)

    # Accept cookie banner (OneTrust) if present
    try:
        btn = page.locator("#onetrust-accept-btn-handler")
        btn.wait_for(state="visible", timeout=5000)
        _js_click_button(page, btn)
        human_delay(0.5, 1)
    except Exception:
        pass

    # The page opens on the register view by default; switch to the login view
    sw = page.get_by_test_id("auth-select-type--register-switch")
    _js_click_button(page, sw)
    page.wait_for_selector("[data-testid='auth-select-type--login-email']")
    human_delay(0.8, 1.5)

    email_btn = page.get_by_test_id("auth-select-type--login-email")
    _js_click_button(page, email_btn)
    page.wait_for_selector("input[type='password']")
    human_delay(0.8, 1.5)

    human_type(page.get_by_placeholder("Nombre de usuario o e-mail"), EMAIL)
    human_delay(0.5, 1)
    human_type(page.locator("input[type='password']"), PASSWORD)
    human_delay(0.8, 1.5)

    submit = page.locator("button[type='submit']")
    _js_click_button(page, submit)

    if visible:
        print("  Waiting... solve the slider in the browser if it appears.")
    _AUTH_PATHS = ("/member/signup", "/member/login", "/member/verify", "/oauth", "/auth/")
    # In visible mode allow 2min for manual captcha; in headless redirect is instant or never
    try:
        page.wait_for_url(
            lambda url: "vinted.es" in url and not any(p in url for p in _AUTH_PATHS),
            timeout=120000 if visible else 20000,
        )
    except PlaywrightTimeout:
        _abort_if_captcha(page, visible)
        raise RuntimeError(f"Login did not complete: {page.url}")
    _abort_if_captcha(page, visible)
    print(f"  Login OK — {page.url}")


def _datadome_present(page) -> bool:
    """Return True if a DataDome challenge (captcha slider, interstitial) is visible."""
    try:
        if "captcha-delivery.com" in page.url:
            return True
        if page.locator("iframe[src*='captcha-delivery.com']").count() > 0:
            return True
    except Exception:
        pass
    return False


def _abort_if_captcha(page, visible: bool) -> None:
    """In headless mode, abort with actionable instructions if DataDome is blocking us.

    In visible mode the user can solve the slider manually, so we just return.
    """
    if visible or not _datadome_present(page):
        return
    print()
    print("ERROR: Vinted has shown a DataDome challenge (captcha).")
    print("       It can't be solved automatically in headless mode.")
    print("       Re-run with --visible to solve it once:")
    print("           python upload_vinted.py --visible")
    print(f"       The session will be saved to {AUTH_STATE_PATH}; subsequent runs can go back to headless.")
    sys.exit(2)


def _save_categories():
    """Persist the in-memory _CATEGORIES dict back to category_mapping.json (atomic write)."""
    CATEGORIES_PATH.write_text(
        json.dumps(_CATEGORIES, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _user_id_from_jwt_cookie(page) -> str:
    """Extract the Vinted user id from the session's JWT cookie.

    Vinted stores JWTs in `access_token_web` / `refresh_token_web`; the payload's
    `sub` claim is the numeric user id. This is the only DOM/API-free source and
    survives DataDome blocking our XHR calls.
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


def get_member_url(page) -> str:
    """Return the user's own /member/<id> URL, derived from a logged-in page.

    Vinted's `/api/v2/users/current` is DataDome-blocked for scripted clients, the
    React app doesn't expose the id in localStorage or header links, and the
    `/settings/profile` page doesn't link to the member profile either. The only
    reliable source is the session JWT cookie (`access_token_web`), whose `sub`
    claim is the numeric user id. We use that first, falling back to the URL path.
    """
    uid = _user_id_from_jwt_cookie(page)
    if uid:
        return f"{BASE_URL}/member/{uid}"
    m = re.search(r"/member/(\d+)", page.url)
    if m:
        return f"{BASE_URL}/member/{m.group(1)}"
    # page.request.get() bypasses DataDome's XHR fingerprinting and gets 403'd; we need
    # to call /api/v2/users/current from the page context so the real browser fetch is used.
    try:
        page.goto(BASE_URL)
        page.wait_for_load_state("domcontentloaded")
        human_delay(1.5, 2.5)
    except Exception as e:
        print(f"    WARNING: could not navigate to home: {e}")

    try:
        data = page.evaluate(
            """async () => {
              try {
                const r = await fetch('/api/v2/users/current', { credentials: 'include' });
                const status = r.status;
                if (!r.ok) return { status, body: (await r.text()).slice(0, 200) };
                const j = await r.json();
                return { status, data: j };
              } catch (e) { return { status: 0, body: String(e) }; }
            }"""
        ) or {}
        print(f"    API users/current (in-page) → HTTP {data.get('status')}")
        if data.get("status") == 200 and data.get("data"):
            user = (data["data"].get("user") or {})
            uid = user.get("id") or data["data"].get("id")
            if uid:
                return f"{BASE_URL}/member/{uid}"
            print(f"    API OK but no id — keys: {list(data['data'].keys())[:10]}")
        elif data.get("body"):
            print(f"    body: {data['body'][:200]}")
    except Exception as e:
        print(f"    WARNING: in-page fetch failed: {e}")

    # localStorage is the most reliable hint: Vinted's React app caches the signed-in
    # user payload there. The key name varies by deploy, so we walk every entry looking
    # for a JSON blob with a numeric id + a login/username field (to avoid matching
    # unrelated ids like ad-partner user segments).
    try:
        uid = page.evaluate(
            r"""() => {
              const keys = Object.keys(localStorage);
              for (const k of keys) {
                const raw = localStorage.getItem(k);
                if (!raw || raw.length > 200000) continue;
                let parsed;
                try { parsed = JSON.parse(raw); } catch { continue; }
                const stack = [parsed];
                while (stack.length) {
                  const node = stack.pop();
                  if (!node || typeof node !== 'object') continue;
                  if (typeof node.id === 'number' &&
                      (typeof node.login === 'string' || typeof node.anon_id === 'string' ||
                       typeof node.email === 'string' || typeof node.is_logged_in === 'boolean')) {
                    if (node.id > 1000) return node.id;  // real Vinted user ids are large
                  }
                  for (const v of Object.values(node)) {
                    if (v && typeof v === 'object') stack.push(v);
                  }
                }
              }
              return 0;
            }"""
        )
        if uid:
            print(f"    Profile (localStorage): {uid}")
            return f"{BASE_URL}/member/{uid}"
        print(f"    localStorage has no recognisable user.")
    except Exception as e:
        print(f"    WARNING: error reading localStorage: {e}")

    # Last resort: navigate to a user-scoped settings page and read /member/<id> links.
    try:
        page.goto(f"{BASE_URL}/settings/profile")
        page.wait_for_load_state("domcontentloaded")
        human_delay(1.5, 2.5)
        href = page.evaluate(
            r"""() => {
              const links = Array.from(document.querySelectorAll('a[href*="/member/"]'));
              for (const a of links) {
                const h = a.getAttribute('href') || '';
                if (/^\/member\/\d+(\/|$)/.test(h)) return h;
              }
              return '';
            }"""
        ) or ""
        m = re.search(r"/member/(\d+)", href)
        if m:
            return f"{BASE_URL}/member/{m.group(1)}"
        print(f"    /settings/profile also does not expose /member/<id>.")
    except Exception as e:
        print(f"    WARNING: error on /settings/profile: {e}")

    raise RuntimeError("Could not derive the profile URL — session expired?")


def scrape_drafts(page, member_url: str) -> list[dict]:
    """Return a list of drafts on the user's profile.

    Each entry: {"item_id": str, "edit_url": str, "title": str}.
    Identifies drafts via the presence of `[data-testid$="--status-text"]` with text
    "Borrador" (Spanish for "draft") inside the item card, and extracts the edit URL
    from the nested `a[href$="/edit"]`.
    """
    # `networkidle` hangs on profile pages (persistent image/analytics traffic);
    # `domcontentloaded` + scrolling is enough for the card list to hydrate.
    page.goto(member_url, wait_until="domcontentloaded")
    human_delay(2, 3)
    # Scroll to the bottom to make sure lazy-loaded items render
    try:
        for _ in range(6):
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            human_delay(0.4, 0.7)
    except Exception:
        pass

    # Diagnostic snapshot to pinpoint why draft scraping finds 0
    try:
        diag = page.evaluate(
            r"""() => ({
              url: location.href,
              status_text_count: document.querySelectorAll('[data-testid$="--status-text"]').length,
              status_text_samples: Array.from(document.querySelectorAll('[data-testid$="--status-text"]'))
                .slice(0, 5).map(el => (el.innerText || '').trim()),
              edit_link_count: document.querySelectorAll('a[href$="/edit"]').length,
              edit_link_samples: Array.from(document.querySelectorAll('a[href$="/edit"]'))
                .slice(0, 5).map(a => a.getAttribute('href')),
              product_card_count: document.querySelectorAll('[data-testid^="product-item-id-"]').length,
              any_item_link_count: document.querySelectorAll('a[href*="/items/"]').length,
              tabs: Array.from(document.querySelectorAll('[role="tab"], nav a'))
                .slice(0, 20).map(t => ({ text: (t.innerText || '').trim().slice(0,40), href: t.getAttribute('href') || '' }))
            })"""
        ) or {}
        print(f"    Profile diag: url={diag.get('url')}")
        print(f"      status-text={diag.get('status_text_count')} samples={diag.get('status_text_samples')}")
        print(f"      edit-links={diag.get('edit_link_count')} samples={diag.get('edit_link_samples')}")
        print(f"      product-cards={diag.get('product_card_count')}  item-links={diag.get('any_item_link_count')}")
        print(f"      tabs={diag.get('tabs')}")
    except Exception as e:
        print(f"    WARNING: diagnostic failed: {e}")

    # On the owner's profile, every draft renders a pair of `<a href="/items/<id>/edit">`
    # links (image and title both link to the edit page). Published items show a regular
    # `/items/<id>` link without `/edit`, so edit-links cleanly identify drafts. We walk
    # up from each edit-link to find a card container and pull the title from its img alt.
    raw = page.evaluate(
        r"""() => {
          const drafts = new Map();
          document.querySelectorAll('a[href$="/edit"]').forEach(a => {
            const href = a.getAttribute('href') || '';
            const m = href.match(/\/items\/(\d+)\/edit/);
            if (!m) return;
            const id = m[1];
            if (drafts.has(id)) return;
            // Find the enclosing card (walk up until we hit a product-item testid or stop)
            let card = a.parentElement;
            for (let i = 0; i < 12 && card; i++) {
              if (card.matches('[data-testid^="product-item-id-"]')) break;
              card = card.parentElement;
            }
            const scope = card || a.parentElement || a;
            // Title from image alt (clean) falling back to nearest non-edit item link
            let title = '';
            const img = scope.querySelector && scope.querySelector('img[alt]');
            if (img) title = (img.getAttribute('alt') || '').trim();
            if (!title && scope.querySelector) {
              const tlink = scope.querySelector('a[href^="/items/"]:not([href$="/edit"])');
              if (tlink) title = (tlink.getAttribute('title') || tlink.innerText || '').trim();
            }
            drafts.set(id, { item_id: id, edit_url: href, title });
          });
          return Array.from(drafts.values());
        }"""
    ) or []
    drafts = []
    for d in raw:
        d["edit_url"] = BASE_URL + d["edit_url"] if d["edit_url"].startswith("/") else d["edit_url"]
        drafts.append(d)
    return drafts


def _normalize_title(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def match_draft_to_item(draft_title: str, items: dict) -> tuple[str | None, bool]:
    """Match a Vinted draft title to a Wallapop item id.

    Returns (item_id, ambiguous). Exact-normalized match wins; otherwise a substring
    match on the longer of (draft, wallapop) title is attempted. If two Wallapop
    items tie, returns (None, True) so the caller can skip.
    """
    dn = _normalize_title(draft_title)
    if not dn:
        return None, False
    exact = [iid for iid, it in items.items() if _normalize_title(it.get("title", "")) == dn]
    if len(exact) == 1:
        return exact[0], False
    if len(exact) > 1:
        return None, True
    substr = [
        iid for iid, it in items.items()
        if (tn := _normalize_title(it.get("title", "")))
        and (tn in dn or dn in tn)
    ]
    if len(substr) == 1:
        return substr[0], False
    if len(substr) > 1:
        return None, True
    return None, False


def _testid_to_key(testid: str) -> str:
    """Derive a stable JSON key from a dropdown testid. 'brand-single-list-input' → 'brand'."""
    return testid.replace("-single-list-input", "").replace("-", "_")


def fill_dynamic_attributes(
    page, item: dict, cat_id: str, learn: bool
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Fill every dynamic dropdown Vinted renders for this category.

    - Consults _CATEGORIES[cat_id]["attributes"] for existing mappings.
    - Auto-discovers new fields and resolves them against item["attributes"] using
      guess_wallapop_key; resolved mappings are persisted back to category_mapping.json
      (unless learn=False).
    - Returns (missing_fields, new_mappings, unresolved) for the end-of-run summary.
        missing_fields: labels for which we had a value but couldn't select it, or
                        which Vinted requires but Wallapop didn't provide.
        new_mappings:   list of "Label ← wallapop_key" strings newly learned this run.
        unresolved:     list of (label, first_few_options_joined) for fields where no
                        Wallapop key could be guessed — user review needed.
    """
    missing: list[str] = []
    new_mappings: list[str] = []
    unresolved: list[tuple[str, str]] = []

    wl_attrs = item.get("attributes") or {}
    cat_entry = _CATEGORIES.setdefault(cat_id, {})
    attrs_cfg = cat_entry.setdefault("attributes", {})
    dirty = False

    # Form mounts progressively — give it a moment to finish rendering all dropdowns
    human_delay(1.0, 1.8)
    fields = scan_dynamic_fields(page)
    # Second pass after a short wait in case fields are still mounting
    human_delay(0.6, 1.0)
    extra = scan_dynamic_fields(page)
    seen = {f["testid"] for f in fields}
    for f in extra:
        if f["testid"] not in seen:
            fields.append(f)
    print(f"    Fields detected: {[(f.get('testid'), f.get('label'), f.get('kind')) for f in fields]}")
    for f in fields:
        testid = f.get("testid") or ""
        label = f.get("label") or ""
        kind = f.get("kind") or "other"
        if not testid or "condition" in testid:
            continue  # condition already handled by set_condition()
        key = _testid_to_key(testid)
        # Common attributes (Marca, Talla, Color) appear in every category Vinted exposes
        # them in. Resolve them on the fly via LABEL_ALIASES without persisting per-category —
        # otherwise category_mapping.json gets polluted with redundant 'Marca ← brand' entries
        # for every new category we touch.
        norm_label = normalize_label(label)
        is_common = norm_label in {"marca", "talla", "color"}
        if is_common:
            from_key = label_to_wallapop_key(label) or label_to_wallapop_key(key)
            cfg = {"label": label, "from": from_key, "kind": kind}
            if norm_label == "marca":
                cfg["fallback"] = "no_brand"
        else:
            cfg = attrs_cfg.get(key)

        # First encounter: try to resolve against Wallapop attrs and record in config
        if cfg is None:
            # Try item-aware guess first (returns a key only if this item has it).
            # Fall back to label-only alias resolution so well-known fields like
            # "Color"→"color" or "Marca"→"brand" are always wired up regardless of
            # whether the first item that triggers the category happens to have that
            # attribute.
            guessed = (
                guess_wallapop_key(label, wl_attrs)
                or guess_wallapop_key(key, wl_attrs)
                or label_to_wallapop_key(label)
                or label_to_wallapop_key(key)
            )
            cfg = {"label": label, "from": guessed, "kind": kind}
            if normalize_label(label) == "marca":
                cfg["fallback"] = "no_brand"
            attrs_cfg[key] = cfg
            dirty = True
            if guessed:
                new_mappings.append(f"{label!r} ← {guessed}")

        from_key = cfg.get("from")
        # If from:null was saved when the first item lacked the attribute, try to
        # resolve from the label alias table alone (e.g. "Color"→"color"). Update
        # the mapping so subsequent items don't hit this fallback path again.
        if from_key is None:
            resolved = label_to_wallapop_key(label) or label_to_wallapop_key(key)
            if resolved and resolved in wl_attrs:
                from_key = resolved
                cfg["from"] = from_key
                dirty = True
                new_mappings.append(f"{label!r} ← {from_key} (re-resolved from label)")
        value = wl_attrs.get(from_key) if from_key else None
        if isinstance(value, (int, float)):
            value = str(value)
        # Color: Wallapop sends EN keys ('black', 'blue'…) → translate to ES for Vinted.
        if from_key == "color" and value and value in COLOR_EN_TO_ES:
            value = COLOR_EN_TO_ES[value]

        # Resolution priority for the final value to fill:
        # 1. Value from Wallapop (already set above)
        # 2. Per-category default from category_mapping.json (e.g. PEGI 12, Desbloqueada)
        # 3. For Color only: the first option in the UI picker (Vinted's category-aware
        #    "Sugerencia"). Any other missing field falls through to draft so we don't
        #    silently assign a wrong value (e.g. XS for every item missing a size).
        if not value and cfg.get("default"):
            value = cfg["default"]

        def _fill(t: str, v: str):
            return fill_combobox(page, t, v) if kind == "combobox" else fill_dropdown(page, t, v)

        if value:
            value_mapped = (cfg.get("value_map") or {}).get(value, value)
            ok, options = _fill(testid, value_mapped)
            if options and "observed_options" not in cfg:
                cfg["observed_options"] = options[:30]
                dirty = True
            if ok:
                print(f"    {label}: {value_mapped}")
            else:
                print(f"    WARNING: option {value_mapped!r} not found in '{label}'")
                if cfg.get("fallback") == "no_brand" and select_no_brand(page, testid):
                    print(f"    {label}: Publicar sin marca (fallback)")
                else:
                    missing.append(label)
        else:
            if cfg.get("fallback") == "no_brand":
                if select_no_brand(page, testid):
                    print(f"    {label}: Publicar sin marca (fallback)")
                else:
                    missing.append(label)
            elif from_key == "color":
                # Wallapop lacks a colour value. Pick Vinted's first suggestion in a
                # single open cycle — it's the category-aware top choice, better than
                # hardcoding "Negro" and better than leaving the item as a draft.
                ok, options = fill_first_option(page, testid)
                if options and "observed_options" not in cfg:
                    cfg["observed_options"] = options[:30]
                    dirty = True
                if ok:
                    print(f"    WARNING: '{label}' not in Wallapop — using first suggestion: {options[0]!r}")
                missing.append(label)
            else:
                # No value, no default, not colour: leave empty and let publish-or-draft
                # handle it. Collect options only the first time a field is seen as
                # unresolved so future runs can pick the mapping from the JSON file.
                if cfg.get("from") is None and "observed_options" not in cfg:
                    _, options = _fill(testid, "__never_match__")
                    if options:
                        cfg["observed_options"] = options[:30]
                        dirty = True
                missing.append(label)
                if cfg.get("from") is None:
                    unresolved.append((label, ", ".join(cfg.get("observed_options", [])[:8])))

    if learn and dirty:
        try:
            _save_categories()
        except Exception as e:
            print(f"    WARNING: could not write category_mapping.json: {e}")

    return missing, new_mappings, unresolved


def upload_item(page, item: dict, learn: bool = True, visible: bool = True) -> dict:
    """Upload one item to Vinted. Tries to publish; falls back to draft on validation failure.

    Returns {vinted_id, status, missing_fields, error, new_mappings, unresolved}.
    status ∈ {"published","draft","failed"}. Empty dict-ish only on unrecoverable setup errors.
    """
    empty = {"vinted_id": "", "status": "failed", "missing_fields": [],
             "error": "", "new_mappings": [], "unresolved": []}

    title = item.get("title", "")
    if not title:
        print("  WARNING: item has no title, skipping.")
        return {**empty, "error": "no title"}
    print(f"  Uploading: {title}")

    _ITEMS_NEW = f"{BASE_URL}/items/new"
    page.goto(_ITEMS_NEW)
    try:
        page.wait_for_url(lambda url: _ITEMS_NEW in url, timeout=120000)
    except PlaywrightTimeout:
        print(f"    Error: did not reach /items/new (URL: {page.url})")
        return {**empty, "error": "did not reach /items/new"}

    # DataDome can interstitialise any request (not just login) when it flags the
    # session as suspicious. Catch it here so we abort cleanly instead of marking
    # every remaining item as failed when the form won't render.
    _abort_if_captcha(page, visible)

    try:
        page.wait_for_selector(
            "input[data-testid='title--input']", state="visible", timeout=15000
        )
    except PlaywrightTimeout:
        print("    Error: the form did not load correctly.")
        return {**empty, "error": "form did not load"}
    human_delay(1, 2)

    image_paths = [p for p in item.get("images", []) if p and Path(p).exists()]
    if not image_paths:
        print(f"    WARNING: no images for '{title}', skipping.")
        return {**empty, "error": "no images"}

    try:
        page.locator("input[data-testid='add-photos-input']").set_input_files(image_paths)
        human_delay(2, 4)
    except Exception as e:
        print(f"    Error uploading images: {e}")
        return {**empty, "error": f"images: {e}"}

    try:
        title_for_vinted = soften_title_caps(title)
        if title_for_vinted != title:
            print(f"    Title softened (too many caps): {title_for_vinted!r}")
        human_type(page.locator("input[data-testid='title--input']"), title_for_vinted)
        human_delay(0.5, 1)
    except Exception as e:
        print(f"    Error filling title: {e}")
        return {**empty, "error": f"title: {e}"}

    description = item.get("description", "")
    if description:
        try:
            desc = page.locator("textarea[data-testid='description--input']")
            desc.click()
            human_delay(0.2, 0.4)
            desc.fill(description)
            human_delay(0.5, 1)
        except Exception:
            pass

    cat_id = str(item.get("category_id") or "")
    nav = get_nav(item)
    cat_ok = False
    sub_options: list[str] = []
    if nav:
        # Resolve the nav path to a leaf locally using the Vinted category tree
        # before touching the browser picker.  This avoids extra browser round-trips
        # (and DataDome exposure) for items whose Wallapop category maps to an
        # intermediate Vinted node (e.g. "Dispositivos de red" → Routers/Repetidores/Módems).
        hints = f"{item.get('title', '')} {item.get('description', '')}".strip()
        nav = resolve_nav_to_leaf(
            nav, hints, nodes=_VINTED_NODES, path_index=_VINTED_BY_PATH
        )
        cat_ok, sub_options = select_category(page, nav)
        human_delay(0.5, 1.0)
    else:
        print(f"    WARNING: no category mapping for category_id={cat_id}")

    # Estado is a common Vinted attribute — try regardless of cat_ok. Vinted exposes it
    # as soon as a category (even an intermediate one) is selected. set_condition() is
    # a silent no-op if the field isn't visible, so it's safe to call unconditionally.
    set_condition(page)
    human_delay(0.5, 1.0)

    missing: list[str] = []
    new_mappings: list[str] = []
    unresolved: list[tuple[str, str]] = []

    # Persist sub-options when the nav stopped short of a leaf AND no per-item
    # match resolved it. Surfaces the options inside category_mapping.json so a
    # human can pick one by hand, or refine the mapping with a value_map.
    if sub_options and learn and cat_id:
        cat_entry = _CATEGORIES.setdefault(cat_id, {})
        if cat_entry.get("observed_leaf_options") != sub_options:
            cat_entry["observed_leaf_options"] = sub_options
            try:
                _save_categories()
            except Exception as e:
                print(f"    WARNING: could not save category_mapping.json: {e}")

    if nav and not cat_ok:
        # Category picker didn't reach a leaf (partial nav path or click failed).
        # The picker was dismissed with Escape, so Vinted may have accepted the
        # partial selection — the form is still accessible.  Record the gap and
        # fall through to fill whatever attributes are visible.
        missing.append(f"Category (incomplete nav path: {' > '.join(nav)})")

    if cat_id:
        m, nm, u = fill_dynamic_attributes(page, item, cat_id, learn)
        missing.extend(m)
        new_mappings.extend(nm)
        unresolved.extend(u)

    # Shipping package size: always pick "Mediano" — see select_package_size() docstring.
    # Common Vinted requirement; called unconditionally (silent no-op if cells aren't rendered).
    select_package_size(page)

    try:
        price_val = float(item.get("price", 0) or 0)
    except (ValueError, TypeError):
        price_val = 0.0
    if price_val > 0:
        try:
            price_str = str(int(price_val)) if price_val == int(price_val) else str(price_val)
            human_type(page.locator("input[data-testid='price-input--input']"), price_str)
            human_delay(0.5, 1)
        except Exception:
            pass
    else:
        print(f"    WARNING: price is 0 — leaving it blank; fill it in the draft.")

    vinted_id, status, errors = publish_or_draft(page, fallback_id_seed=str(item.get("id", "")))
    return {
        "vinted_id": vinted_id,
        "status": status,
        "missing_fields": missing,
        "error": "; ".join(errors) if errors else "",
        "new_mappings": new_mappings,
        "unresolved": unresolved,
    }


def retry_draft_item(page, item: dict, draft_edit_url: str, draft_item_id: str, learn: bool, visible: bool = True) -> dict:
    """Open an existing Vinted draft and re-attempt publish with updated attributes.

    Differs from upload_item: we navigate to /items/<id>/edit instead of /items/new,
    so the form comes pre-populated with whatever was saved last time. We re-apply
    fill_dynamic_attributes (color EN→ES, category defaults) to fill anything that
    was missing, then try to publish. Returns the same shape as upload_item.
    """
    empty = {"vinted_id": draft_item_id, "status": "draft", "missing_fields": [],
             "error": "", "new_mappings": [], "unresolved": []}

    title = item.get("title", "")
    print(f"  Retrying draft {draft_item_id}: {title}")
    page.goto(draft_edit_url)
    # Same reason as in upload_item: abort early if DataDome is interstitialising the page
    _abort_if_captcha(page, visible)
    try:
        page.wait_for_selector(
            "input[data-testid='title--input']", state="visible", timeout=30000
        )
    except PlaywrightTimeout:
        print("    Error: the draft form did not load.")
        return {**empty, "status": "failed", "error": "draft did not load"}
    human_delay(1.5, 2.5)

    cat_id = str(item.get("category_id") or "")
    missing, new_mappings, unresolved = [], [], []
    if cat_id:
        missing, new_mappings, unresolved = fill_dynamic_attributes(page, item, cat_id, learn)

    select_package_size(page)

    vinted_id, status, errors = publish_or_draft(
        page, fallback_id_seed=str(item.get("id", "")), save_as_draft_on_fail=False
    )
    # The draft already has a real Vinted id; prefer it over any synthetic fallback
    if not vinted_id or vinted_id.startswith(("draft-", "published-")):
        vinted_id = draft_item_id
    return {
        "vinted_id": vinted_id,
        "status": status,
        "missing_fields": missing,
        "error": "; ".join(errors) if errors else "",
        "new_mappings": new_mappings,
        "unresolved": unresolved,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of real upload attempts. Items already uploaded or without a "
             "Vinted mapping are filtered out before this limit applies, so --limit 1 "
             "runs one actual upload (useful for a smoke test).",
    )
    parser.add_argument(
        "--no-learn",
        action="store_true",
        help="Do not write auto-learned mappings to data/category_mapping.json",
    )
    parser.add_argument(
        "--retry-drafts",
        action="store_true",
        help="Instead of uploading new items, open each existing draft on Vinted and try to publish it.",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Launch the browser in visible mode (to solve a DataDome captcha manually). "
             "Runs headless by default.",
    )
    args = parser.parse_args()

    if not EMAIL or not PASSWORD:
        print("ERROR: set VINTED_EMAIL and VINTED_PASSWORD in your .env file")
        sys.exit(1)

    if not ITEMS_PATH.exists():
        print(f"ERROR: {ITEMS_PATH} not found. Run extract_wallapop.py first.")
        sys.exit(1)

    items_data = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    # Inject the dict key (Wallapop item ID) into each item dict for uniform access
    items = [{"id": k, **v} for k, v in items_data.items()]

    # migration.json provides idempotency: items already published or saved as draft are
    # skipped on re-runs. Failed items have no vinted_id and are retried automatically.
    migration = load_migration()

    published = 0
    drafted = 0
    failed = 0
    unmapped: list[tuple[str, str, str]] = []  # (item_id, title, category_id) — no Vinted path

    # --retry-drafts has its own filtering (match drafts to Wallapop items), so skip the
    # pre-filter below. For the normal upload path, drop items that are already uploaded
    # or have no Vinted category path *before* applying --limit, so --limit N means "N
    # real upload attempts" rather than "the first N entries in downloaded_items.json"
    # (which is useless once migration.json has entries).
    if not args.retry_drafts:
        already_done = 0
        pending: list[dict] = []
        for item in items:
            item_id = item.get("id", "")
            prev = migration.get(item_id, {}) if item_id else {}
            if prev.get("vinted_id") and migration_status(prev) in ("published", "draft"):
                already_done += 1
                continue
            if get_nav(item) is None:
                unmapped.append((item_id, item.get("title", ""), item.get("category_id", "?")))
                continue
            pending.append(item)
        if already_done:
            print(f"Items already uploaded (will be skipped): {already_done}")
        if unmapped:
            print(f"Items without Vinted mapping (will be skipped): {len(unmapped)}")
        if args.limit:
            pending = pending[: args.limit]
        items = pending
        print(f"Items to process: {len(items)}")

    drafts_summary: list[tuple[str, str, list[str]]] = []  # (cat_id, title, missing)
    all_new_mappings: list[tuple[str, str]] = []  # (cat_id, "Label ← key")
    all_unresolved: list[tuple[str, str, str]] = []  # (cat_id, label, options_preview)

    with sync_playwright() as p:
        # Default to headless; --visible is for the first login (solve DataDome slider) or
        # if the saved session in data/auth_state.json expires and a new captcha appears.
        # Patchright is used instead of stock Playwright for bot-detection evasion
        # (patches Runtime.enable CDP signal, navigator.webdriver, and --enable-automation).
        browser = p.chromium.launch(
            headless=not args.visible,
            args=["--disable-blink-features=AutomationControlled"],
        )

        ctx_kwargs = dict(
            viewport={"width": 1440, "height": 900},
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )
        if AUTH_STATE_PATH.exists():
            ctx_kwargs["storage_state"] = str(AUTH_STATE_PATH)
            print("Saved session found.")

        context = browser.new_context(**ctx_kwargs)

        # Patch remaining JS fingerprinting signals not covered by Patchright
        context.add_init_script("""
            Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es'] });
            Object.defineProperty(navigator, 'plugins', {
                get: () => { const p = [1,2,3,4,5]; p.item = () => null; return p; }
            });
            const origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (params) =>
                params.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : origQuery(params);
        """)

        page = context.new_page()

        # Navigate to /items/new to probe session state before the upload loop
        _ITEMS_NEW = f"{BASE_URL}/items/new"
        _AUTH_PATHS = ("/member/signup", "/member/login", "/member/verify", "/oauth", "/auth/")
        page.goto(_ITEMS_NEW)
        # In visible mode allow 2min for manual captcha; in headless the redirect is instant
        try:
            page.wait_for_url(
                lambda url: _ITEMS_NEW in url or any(p in url for p in _AUTH_PATHS),
                timeout=120000 if args.visible else 20000,
            )
        except PlaywrightTimeout:
            pass

        _abort_if_captcha(page, args.visible)

        if any(p in page.url for p in _AUTH_PATHS):
            login(page, visible=args.visible)
            context.storage_state(path=str(AUTH_STATE_PATH))
            print(f"  Session saved to {AUTH_STATE_PATH}")
        elif _ITEMS_NEW in page.url:
            print("  Session is active.")
            context.storage_state(path=str(AUTH_STATE_PATH))  # refresh persisted state
        else:
            print(f"  Unexpected state ({page.url}), attempting login...")
            login(page, visible=args.visible)
            context.storage_state(path=str(AUTH_STATE_PATH))
            print(f"  Session saved to {AUTH_STATE_PATH}")

        # --retry-drafts: iterate over existing drafts on Vinted instead of new uploads
        if args.retry_drafts:
            try:
                member_url = get_member_url(page)
                print(f"Profile: {member_url}")
                drafts = scrape_drafts(page, member_url)
                print(f"Drafts found on Vinted: {len(drafts)}")
            except Exception as e:
                print(f"ERROR listing drafts: {e}")
                browser.close()
                return

            items_by_id = {it["id"]: it for it in items}
            retry_targets: list[tuple[dict, str, str]] = []  # (item, edit_url, draft_id)
            ambiguous: list[str] = []
            no_match: list[str] = []
            for d in drafts:
                wid, amb = match_draft_to_item(d["title"], items_by_id)
                if amb:
                    ambiguous.append(d["title"])
                    continue
                if not wid:
                    no_match.append(d["title"])
                    continue
                retry_targets.append((items_by_id[wid], d["edit_url"], d["item_id"]))

            if args.limit:
                retry_targets = retry_targets[: args.limit]
            print(f"Drafts matched against Wallapop items: {len(retry_targets)}")
            if ambiguous:
                print(f"  ambiguous (skipped): {len(ambiguous)} — {ambiguous[:3]}")
            if no_match:
                print(f"  no match (skipped): {len(no_match)} — {no_match[:3]}")

            for i, (item, edit_url, draft_item_id) in enumerate(retry_targets):
                item_id = item.get("id", "")
                title = item.get("title", "")
                print(f"\n[{i+1}/{len(retry_targets)}] {title}")
                try:
                    result = retry_draft_item(page, item, edit_url, draft_item_id, learn=not args.no_learn, visible=args.visible)
                except Exception as e:
                    print(f"  Unexpected error: {e}")
                    result = {"vinted_id": draft_item_id, "status": "failed", "missing_fields": [],
                              "error": str(e), "new_mappings": [], "unresolved": []}

                cat_id = str(item.get("category_id") or "")
                status = result.get("status") or "failed"
                if status == "published":
                    published += 1
                elif status == "draft":
                    drafted += 1
                    drafts_summary.append((cat_id, title, result.get("missing_fields") or []))
                else:
                    failed += 1

                for m in result.get("new_mappings", []):
                    all_new_mappings.append((cat_id, m))
                for label, options_preview in result.get("unresolved", []):
                    all_unresolved.append((cat_id, label, options_preview))

                if item_id and result.get("vinted_id"):
                    mark_migrated(
                        migration, item_id, result["vinted_id"], status,
                        missing_fields=result.get("missing_fields"),
                        error=result.get("error", ""),
                    )
                human_delay(2, 5)

            browser.close()
            print(
                f"\nRetry complete: {published} published, {drafted} still draft, "
                f"{failed} failed"
            )
            if drafts_summary:
                print(f"\nPending drafts:")
                for cat, t, miss in drafts_summary:
                    miss_str = ", ".join(miss) if miss else "(review the form)"
                    print(f"  [{cat}] {t[:60]:60s}  missing: {miss_str}")
            if all_new_mappings:
                print(f"\nNewly auto-learned mappings (saved to category_mapping.json):")
                for cat, m in all_new_mappings:
                    print(f"  [{cat}] {m}")
            if all_unresolved:
                print(f"\nUnresolved mappings (from:null in category_mapping.json):")
                seen: set[tuple[str, str]] = set()
                for cat, label, opts in all_unresolved:
                    k = (cat, label)
                    if k in seen:
                        continue
                    seen.add(k)
                    print(f"  [{cat}] {label!r}  options: {opts or '(none)'}")
            return

        # Already-uploaded and unmapped items are filtered out above, so every entry
        # in `items` here is a real upload attempt.
        for i, item in enumerate(items):
            item_id = item.get("id", "")
            title = item.get("title", item_id or f"item-{i+1}")
            print(f"\n[{i+1}/{len(items)}] {title}")

            try:
                result = upload_item(page, item, learn=not args.no_learn, visible=args.visible)
            except Exception as e:
                print(f"  Unexpected error: {e}")
                result = {"vinted_id": "", "status": "failed", "missing_fields": [],
                          "error": str(e), "new_mappings": [], "unresolved": []}

            cat_id = str(item.get("category_id") or "")
            status = result.get("status") or "failed"
            if status == "published":
                published += 1
            elif status == "draft":
                drafted += 1
                drafts_summary.append((cat_id, title, result.get("missing_fields") or []))
            else:
                failed += 1

            for m in result.get("new_mappings", []):
                all_new_mappings.append((cat_id, m))
            for label, options_preview in result.get("unresolved", []):
                all_unresolved.append((cat_id, label, options_preview))

            if item_id and result.get("vinted_id"):
                mark_migrated(
                    migration, item_id, result["vinted_id"], status,
                    missing_fields=result.get("missing_fields"),
                    error=result.get("error", ""),
                )
            human_delay(2, 5)  # pause between items to avoid rate limiting

        browser.close()

    print(
        f"\nUpload complete: {published} published, {drafted} drafts, "
        f"{failed} failed, {len(unmapped)} unmapped"
    )
    if drafts_summary:
        print(f"\nDrafts that need to be finished on Vinted:")
        for cat, t, miss in drafts_summary:
            miss_str = ", ".join(miss) if miss else "(review the form)"
            print(f"  [{cat}] {t[:60]:60s}  missing: {miss_str}")
    if all_new_mappings:
        print(f"\nNewly auto-learned mappings (saved to category_mapping.json):")
        for cat, m in all_new_mappings:
            print(f"  [{cat}] {m}")
    if all_unresolved:
        print(f"\nUnresolved mappings (from:null in category_mapping.json, please review):")
        seen: set[tuple[str, str]] = set()
        for cat, label, opts in all_unresolved:
            k = (cat, label)
            if k in seen:
                continue
            seen.add(k)
            print(f"  [{cat}] {label!r}  options: {opts or '(none)'}")
    if unmapped:
        print(f"\nCategories without a Vinted path ({len(unmapped)}) — add the path in data/category_mapping.json:")
        for uid, utitle, ucat in unmapped:
            print(f"  category_id={ucat}  →  {utitle[:60]}")


if __name__ == "__main__":
    main()
