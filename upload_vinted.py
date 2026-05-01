"""
upload_vinted.py
Reads data/downloaded_items.json and uploads each item to Vinted via browser automation.
Run extract_wallapop.py first to populate the items file.
"""

import os
import sys
import json
import argparse
from pathlib import Path
from dotenv import load_dotenv
from patchright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from domain.categories import build_path_index, resolve_nav_to_leaf
from domain.drafts import match_draft_to_item
from domain.mapping import (
    COLOR_EN_TO_ES,
    LABEL_ALIASES,
    guess_wallapop_key,
    label_to_wallapop_key,
)
from domain.migration import (
    filter_pending,
    load_migration as _load_migration,
    mark_migrated as _mark_migrated,
    migration_status,
)
from domain.text import (
    find_option_match,
    normalize_label,
    soften_title_caps,
    stem,
    testid_to_key,
)
from domain.urls import extract_item_id_from_url, is_form_url
from vinted.errors import CaptchaDetected
from vinted.session import (
    abort_if_captcha as _vinted_abort_if_captcha,
    build_session,
)
from vinted.pages._common import human_delay, human_type
from vinted.pages.edit_draft import EditDraftPage
from vinted.pages.login import LoginPage
from vinted.pages.new_item import NewItemPage
from vinted.pages.profile import ProfilePage

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
    """Thin wrapper that binds the canonical MIGRATION_PATH to domain.migration.load_migration."""
    return _load_migration(MIGRATION_PATH)


def mark_migrated(
    migration: dict,
    item_id: str,
    vinted_id: str,
    status: str,
    missing_fields: list[str] | None = None,
    error: str = "",
):
    """Thin wrapper that binds the canonical MIGRATION_PATH to domain.migration.mark_migrated."""
    _mark_migrated(
        migration, MIGRATION_PATH, item_id, vinted_id, status, missing_fields, error
    )


def _abort_if_captcha(page, visible: bool) -> None:
    """Translate ``CaptchaDetected`` into the operator-facing exit code.

    The detector and the headless guard live in ``vinted.session``; this
    wrapper only owns the UI of the abort path (the message printed to the
    user and the ``sys.exit(2)`` call). Future Page Objects can call
    ``vinted.session.abort_if_captcha`` directly and decide for themselves
    how to react to the exception.
    """
    try:
        _vinted_abort_if_captcha(page, visible)
    except CaptchaDetected:
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


def fill_dynamic_attributes(
    new_item: NewItemPage, item: dict, cat_id: str, learn: bool
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
    fields = new_item.scan_dynamic_fields()
    # Second pass after a short wait in case fields are still mounting
    human_delay(0.6, 1.0)
    extra = new_item.scan_dynamic_fields()
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
        key = testid_to_key(testid)
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
            return (
                new_item.fill_combobox(t, v)
                if kind == "combobox"
                else new_item.fill_dropdown(t, v)
            )

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
                if cfg.get("fallback") == "no_brand" and new_item.select_no_brand(testid):
                    print(f"    {label}: Publicar sin marca (fallback)")
                else:
                    missing.append(label)
        else:
            if cfg.get("fallback") == "no_brand":
                if new_item.select_no_brand(testid):
                    print(f"    {label}: Publicar sin marca (fallback)")
                else:
                    missing.append(label)
            elif from_key == "color":
                # Wallapop lacks a colour value. Pick Vinted's first suggestion in a
                # single open cycle — it's the category-aware top choice, better than
                # hardcoding "Negro" and better than leaving the item as a draft.
                ok, options = new_item.fill_first_option(testid)
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
    # Skip the goto when we're already on /items/new — typically true for the
    # first item, since main()'s bootstrap probe already landed here. After
    # publishing an item Vinted redirects to the profile, so item 2+ falls
    # through to the goto. Saves one round-trip + dcl wait per run.
    if _ITEMS_NEW not in page.url:
        page.goto(_ITEMS_NEW, wait_until="domcontentloaded")
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

    new_item = NewItemPage(page)

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
        cat_ok, sub_options = new_item.select_category(nav)
        human_delay(0.5, 1.0)
    else:
        print(f"    WARNING: no category mapping for category_id={cat_id}")

    # Estado is a common Vinted attribute — try regardless of cat_ok. Vinted exposes it
    # as soon as a category (even an intermediate one) is selected. set_condition() is
    # a silent no-op if the field isn't visible, so it's safe to call unconditionally.
    new_item.set_condition()
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
        m, nm, u = fill_dynamic_attributes(new_item, item, cat_id, learn)
        missing.extend(m)
        new_mappings.extend(nm)
        unresolved.extend(u)

    # Shipping package size: always pick "Mediano" — see NewItemPage.select_package_size().
    # Called unconditionally (silent no-op if cells aren't rendered — non-leaf category).
    new_item.select_package_size()

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

    vinted_id, status, errors = new_item.publish_or_draft(fallback_id_seed=str(item.get("id", "")))
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
    edit_page = EditDraftPage(page)
    edit_page.goto(draft_edit_url)
    # Captcha check has to fit between the goto and the form-loaded wait
    # so we can abort early if DataDome interstitialises the edit page.
    _abort_if_captcha(page, visible)
    if not edit_page.wait_for_form_loaded():
        print("    Error: the draft form did not load.")
        return {**empty, "status": "failed", "error": "draft did not load"}
    human_delay(1.5, 2.5)

    cat_id = str(item.get("category_id") or "")
    missing, new_mappings, unresolved = [], [], []
    if cat_id:
        missing, new_mappings, unresolved = fill_dynamic_attributes(edit_page, item, cat_id, learn)

    edit_page.select_package_size()

    vinted_id, status, errors = edit_page.publish_or_draft(
        fallback_id_seed=str(item.get("id", "")), save_as_draft_on_fail=False
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

    # --retry-drafts has its own filtering (match drafts to Wallapop items) — filter_pending
    # honours the flag and returns items unchanged in that case. For the normal upload path,
    # drop items that are already uploaded or have no Vinted category path *before* applying
    # --limit, so --limit N means "N real upload attempts" rather than "the first N entries
    # in downloaded_items.json" (which is useless once migration.json has entries).
    pending, already_done, unmapped = filter_pending(
        items,
        migration,
        get_nav,
        retry_drafts=args.retry_drafts,
        limit=args.limit or 0,
    )
    if not args.retry_drafts:
        if already_done:
            print(f"Items already uploaded (will be skipped): {already_done}")
        if unmapped:
            print(f"Items without Vinted mapping (will be skipped): {len(unmapped)}")
        items = pending
        print(f"Items to process: {len(items)}")

    drafts_summary: list[tuple[str, str, list[str]]] = []  # (cat_id, title, missing)
    all_new_mappings: list[tuple[str, str]] = []  # (cat_id, "Label ← key")
    all_unresolved: list[tuple[str, str, str]] = []  # (cat_id, label, options_preview)

    with sync_playwright() as p:
        # --visible is for the first login (solve DataDome slider) or if the saved session
        # in data/auth_state.json expires and a new captcha appears; default is headless.
        if AUTH_STATE_PATH.exists():
            print("Saved session found.")
        browser, context, page = build_session(
            p, visible=args.visible, auth_state_path=AUTH_STATE_PATH
        )

        # Navigate to /items/new to probe session state before the upload loop.
        # ``wait_until="domcontentloaded"`` is critical: Vinted's telemetry keeps
        # XHRs alive past initial paint, so the default ``wait_until="load"``
        # times out at 30s on every navigation. After dcl, ``page.url`` is the
        # final post-redirect URL — Vinted does the auth-check redirect server-
        # side, so we don't need ``wait_for_url`` to settle anything.
        _ITEMS_NEW = f"{BASE_URL}/items/new"
        _AUTH_PATHS = ("/member/signup", "/member/login", "/member/verify",
                       "/oauth", "/auth/", "/session-refresh")
        page.goto(_ITEMS_NEW, wait_until="domcontentloaded")

        _abort_if_captcha(page, args.visible)

        if _ITEMS_NEW in page.url:
            print("  Session is active.")
            context.storage_state(path=str(AUTH_STATE_PATH))  # refresh persisted state
        else:
            # Either an auth path or the home page (Vinted redirects expired
            # sessions to ``/``). Both mean: log in. No need to wait — the URL
            # is already final after dcl, so going straight to login() avoids
            # the 20-30s "stuck on home" pause the user reported.
            if any(p in page.url for p in _AUTH_PATHS):
                print(f"  Session expired (redirected to auth: {page.url}), logging in...")
            else:
                print(f"  Session expired (redirected to {page.url}), logging in...")
            # Stale refresh_token_web makes Vinted bounce the signup page to
            # home — the server sees a valid refresh token and decides the user
            # is already logged in. Clear auth cookies first so the server treats
            # the browser as anonymous. datadome lives on .vinted.es (same root
            # domain as access_token_web) so we filter by name, not domain.
            for _tok in ("access_token_web", "refresh_token_web", "_vinted_fr_session"):
                context.clear_cookies(name=_tok)
            LoginPage(page, base_url=BASE_URL).login(
                email=EMAIL, password=PASSWORD, visible=args.visible
            )
            context.storage_state(path=str(AUTH_STATE_PATH))
            print(f"  Session saved to {AUTH_STATE_PATH}")

        # --retry-drafts: iterate over existing drafts on Vinted instead of new uploads
        if args.retry_drafts:
            try:
                profile = ProfilePage(page, base_url=BASE_URL)
                member_url = profile.get_member_url()
                print(f"Profile: {member_url}")
                drafts = profile.scrape_drafts(member_url)
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
