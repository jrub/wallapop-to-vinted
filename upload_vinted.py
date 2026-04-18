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

# Wallapop attribute keys keyed by normalized Vinted label. Seeded with common cases;
# the auto-learn loop extends this implicitly by writing resolved mappings to
# category_mapping.json, so this table only needs the first-time guess.
LABEL_ALIASES = {
    "marca": ["brand"],
    "talla": ["size"],
    "color": ["color", "colour"],
    "estado": ["condition"],
    "sistema operativo": ["operating_system", "os"],
    "capacidad de almacenamiento": ["storage_capacity", "storage"],
    "ram": ["ram", "memory"],
    "procesador": ["processor", "cpu"],
    "autor": ["author"],
    "editorial": ["publisher"],
    "idioma": ["language"],
    "formato": ["format"],
    "plataforma": ["platform"],
    "género": ["genre"],
    "material": ["material", "composition"],
    "modelo": ["model"],
}

# Wallapop's API returns color values as English canonical keys ('orange', 'black', …)
# even though its UI shows them in Spanish. Vinted's dropdown lists them in Spanish.
COLOR_EN_TO_ES = {
    "black": "Negro",
    "white": "Blanco",
    "gray": "Gris",
    "grey": "Gris",
    "silver": "Plateado",
    "red": "Rojo",
    "blue": "Azul",
    "navy": "Azul marino",
    "light_blue": "Azul claro",
    "turquoise": "Turquesa",
    "green": "Verde",
    "dark_green": "Verde oscuro",
    "mint": "Menta",
    "yellow": "Amarillo",
    "mustard": "Mostaza",
    "orange": "Naranja",
    "coral": "Coral",
    "pink": "Rosa",
    "fuchsia": "Fucsia",
    "purple": "Morado",
    "lilac": "Lila",
    "brown": "Marrón",
    "beige": "Beige",
    "cream": "Crema",
    "khaki": "Caqui",
    "burgundy": "Burdeos",
    "multicolor": "Multicolor",
    "multicolour": "Multicolor",
    "gold": "Dorado",
}


def _normalize_label(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def _guess_wallapop_key(label: str, attributes: dict) -> str | None:
    """Best-effort mapping Vinted label → key in item.attributes. Returns None if no match."""
    norm = _normalize_label(label)
    # 1) Direct match against the label itself
    for k in attributes:
        if _normalize_label(k) == norm:
            return k
    # 2) Alias table
    for k in LABEL_ALIASES.get(norm, []):
        if k in attributes:
            return k
    # 3) Substring fallback: Wallapop key is contained in (or contains) the normalized label
    for k in attributes:
        nk = _normalize_label(k)
        if nk and (nk in norm or norm in nk):
            return k
    return None

if not CATEGORIES_PATH.exists():
    print(f"ERROR: No se encuentra {CATEGORIES_PATH}. Ejecuta primero extract_wallapop.py")
    sys.exit(1)
with open(CATEGORIES_PATH, encoding="utf-8") as _f:
    _CATEGORIES = json.load(_f)  # wallapop category_id → {name, vinted: [...nav path]}


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


def select_category(page, nav: list) -> bool:
    """Navigate Vinted's category picker by clicking through each tree level.

    The picker renders one level at a time as div[role="button"] elements.
    :text-is() matches the exact label without substring false positives.
    """
    if not nav:
        return False
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
        print(f"    Categoría: {' > '.join(nav)}")
        return True
    except Exception as e:
        print(f"    AVISO: no se pudo seleccionar categoría {nav}: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


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
        print(f"    Estado: {VINTED_CONDITION}")
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
        print(f"    AVISO: no se pudo escanear campos dinámicos: {e}")
        return []


def _collect_visible_options(page) -> list[str]:
    """After opening a dropdown, read all option labels visible in the panel."""
    try:
        return page.evaluate("""
        () => Array.from(document.querySelectorAll('div[role="button"]'))
          .filter(n => n.offsetParent !== null)
          .map(n => (n.innerText || '').trim())
          .filter(t => t.length > 0 && t.length < 80)
        """) or []
    except Exception:
        return []


def fill_dropdown(page, testid: str, value: str) -> tuple[bool, list[str]]:
    """Open the dropdown identified by testid, try to select an option matching `value`.

    Returns (selected, options_seen). options_seen is populated on both hit and miss
    so the caller can persist them in category_mapping.json for the user to review.
    """
    options: list[str] = []
    try:
        inp = page.locator(f"[data-testid='{testid}']")
        inp.wait_for(state="visible", timeout=3000)
        inp.click()
        human_delay(0.3, 0.6)
        options = _collect_visible_options(page)

        norm_value = _normalize_label(value)
        matched = None
        for opt in options:
            if _normalize_label(opt) == norm_value:
                matched = opt
                break
        if matched is None and norm_value:
            for opt in options:
                no = _normalize_label(opt)
                if no and (no in norm_value or norm_value in no):
                    matched = opt
                    break
        if matched is not None:
            page.locator('div[role="button"]').filter(
                has=page.locator(f':text-is("{matched}")')
            ).first.click()
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
        print(f"    AVISO: fill_dropdown({testid}, {value!r}) falló: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False, options


def fill_combobox(page, testid: str, value: str) -> tuple[bool, list[str]]:
    """For Vinted's brand/color pickers (input + dropdown suggestions).

    Clicks the input, types the value, and selects the first matching suggestion.
    Returns (selected, options_seen).
    """
    options: list[str] = []
    try:
        inp = page.locator(f"[data-testid='{testid}']")
        inp.wait_for(state="visible", timeout=3000)
        inp.click()
        human_delay(0.3, 0.6)
        # Type progressively so React fires keyboard events
        for ch in value:
            inp.press_sequentially(ch)
            time.sleep(random.uniform(0.04, 0.12))
        human_delay(0.6, 1.2)
        options = _collect_visible_options(page)

        norm_value = _normalize_label(value)
        matched = None
        for opt in options:
            if _normalize_label(opt) == norm_value:
                matched = opt
                break
        if matched is None and norm_value:
            for opt in options:
                no = _normalize_label(opt)
                if no and (no in norm_value or norm_value in no):
                    matched = opt
                    break
        if matched is not None:
            page.locator('div[role="button"]').filter(
                has=page.locator(f':text-is("{matched}")')
            ).first.click()
            human_delay(0.3, 0.6)
            return True, options
        # No match — clear the input so typed value doesn't remain as free text
        try:
            inp.fill("")
            page.keyboard.press("Escape")
        except Exception:
            pass
        human_delay(0.2, 0.4)
        return False, options
    except Exception as e:
        print(f"    AVISO: fill_combobox({testid}, {value!r}) falló: {e}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False, options


def select_package_size(page, up_to_kg: str | float | None) -> bool:
    """Pick the shipping package size cell matching the given weight in kg.

    Vinted renders rows with testids like '<internal_id>-package-size--cell' whose
    text reads '5 kg', '10 kg', '20 kg', '30 kg'. We pick the smallest row whose
    weight is greater than or equal to up_to_kg; otherwise the heaviest.
    """
    if up_to_kg is None:
        return False
    try:
        weight = float(str(up_to_kg).replace(",", "."))
    except (TypeError, ValueError):
        return False
    try:
        cells = page.evaluate("""
        () => Array.from(document.querySelectorAll("[data-testid$='-package-size--cell']"))
          .filter(el => el.offsetParent !== null)
          .map(el => ({
            testid: el.getAttribute('data-testid'),
            text: (el.innerText || '').trim()
          }))
        """) or []
        parsed: list[tuple[float, str]] = []
        for c in cells:
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*kg", c.get("text", ""))
            if not m:
                continue
            parsed.append((float(m.group(1).replace(",", ".")), c["testid"]))
        if not parsed:
            return False
        parsed.sort()
        chosen = next((t for kg, t in parsed if kg >= weight), parsed[-1][1])
        page.locator(f"[data-testid='{chosen}']").click()
        human_delay(0.3, 0.6)
        chosen_kg = next(kg for kg, t in parsed if t == chosen)
        print(f"    Paquete envío: {chosen_kg:g} kg (para {weight:g} kg)")
        return True
    except Exception as e:
        print(f"    AVISO: no se pudo elegir tamaño de paquete: {e}")
        return False


def select_no_brand(page, testid: str) -> bool:
    """Click the 'Publicar sin marca' option inside the brand dropdown."""
    try:
        inp = page.locator(f"[data-testid='{testid}']")
        inp.click()
        human_delay(0.3, 0.6)
        opt = page.locator('div[role="button"]').filter(
            has=page.locator(':text-is("Publicar sin marca")')
        ).first
        opt.wait_for(state="visible", timeout=3000)
        opt.click()
        human_delay(0.3, 0.6)
        return True
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
        print(f"    AVISO: botón 'Publicar' no visible. Botones del formulario: {buttons}")
    else:
        try:
            pub.hover()
            human_delay(0.3, 0.6)
            pub.click(timeout=10000)
            try:
                page.wait_for_url(lambda url: not _is_form_url(url), timeout=15000)
            except PlaywrightTimeout:
                pass
            human_delay(1, 2)
            if not _is_form_url(page.url):
                vinted_id = _extract_item_id_from_url(page.url) or f"published-{fallback_id_seed}"
                print(f"    Publicado: {page.url}")
                return vinted_id, "published", []
            errors = collect_form_errors(page)
            if errors:
                print(f"    Publicación rechazada: {errors[:4]}")
            else:
                print(f"    Click en 'Subir' sin navegación ni errores inline — URL sigue: {page.url}")
        except Exception as e:
            print(f"    AVISO: fallo al publicar: {e}")

    if not save_as_draft_on_fail:
        # Retry mode: the draft already exists; leave it as-is and report errors.
        return "", "draft", errors

    try:
        draft_btn = page.locator(f"button[data-testid='{DRAFT_BUTTON_TESTID}']")
        draft_btn.hover()
        human_delay(0.3, 0.6)
        draft_btn.click(timeout=10000)
        try:
            page.wait_for_url(lambda url: not _is_form_url(url), timeout=15000)
        except PlaywrightTimeout:
            pass
        human_delay(1, 2)
        if _is_form_url(page.url):
            print(f"    Error: borrador no guardado — URL: {page.url}")
            return "", "failed", errors
        vinted_id = _extract_item_id_from_url(page.url) or f"draft-{fallback_id_seed}"
        print(f"    Borrador guardado: {page.url} (id registrado: {vinted_id})")
        return vinted_id, "draft", errors
    except Exception as e:
        print(f"    Error al guardar borrador: {e}")
        return "", "failed", errors


def login(page):
    """Log in to Vinted. Waits up to 2 minutes for manual captcha resolution if needed."""
    print("Haciendo login en Vinted...")
    page.goto(f"{BASE_URL}/member/signup/select_type")
    page.wait_for_load_state("networkidle")
    human_delay(1, 2)

    # Accept cookie banner (OneTrust) if present
    try:
        btn = page.locator("#onetrust-accept-btn-handler")
        btn.hover(timeout=5000)
        human_delay(0.2, 0.5)
        btn.click(timeout=5000)
        human_delay(0.5, 1)
    except Exception:
        pass

    # The page opens on the register view by default; switch to the login view
    sw = page.get_by_test_id("auth-select-type--register-switch")
    sw.hover()
    human_delay(0.3, 0.6)
    sw.click()
    page.wait_for_selector("[data-testid='auth-select-type--login-email']")
    human_delay(0.8, 1.5)

    email_btn = page.get_by_test_id("auth-select-type--login-email")
    email_btn.hover()
    human_delay(0.3, 0.6)
    email_btn.click()
    page.wait_for_selector("input[type='password']")
    human_delay(0.8, 1.5)

    human_type(page.get_by_placeholder("Nombre de usuario o e-mail"), EMAIL)
    human_delay(0.5, 1)
    human_type(page.locator("input[type='password']"), PASSWORD)
    human_delay(0.8, 1.5)

    submit = page.locator("button[type='submit']")
    submit.hover()
    human_delay(0.3, 0.6)
    submit.click()

    print("  Esperando... resuelve el slider si aparece en el navegador.")
    _AUTH_PATHS = ("/member/signup", "/member/login", "/member/verify", "/oauth", "/auth/")
    try:
        page.wait_for_url(
            lambda url: "vinted.es" in url and not any(p in url for p in _AUTH_PATHS),
            timeout=120000
        )
    except PlaywrightTimeout:
        raise RuntimeError(f"Login no completado en 2 minutos: {page.url}")
    print(f"  Login OK — {page.url}")


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
        print(f"    AVISO: no se pudo navegar a home: {e}")

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
            print(f"    API OK pero sin id — claves: {list(data['data'].keys())[:10]}")
        elif data.get("body"):
            print(f"    body: {data['body'][:200]}")
    except Exception as e:
        print(f"    AVISO: fetch in-page falló: {e}")

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
            print(f"    Perfil (localStorage): {uid}")
            return f"{BASE_URL}/member/{uid}"
        print(f"    localStorage sin usuario reconocible.")
    except Exception as e:
        print(f"    AVISO: error leyendo localStorage: {e}")

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
        print(f"    /settings/profile tampoco expone /member/<id>.")
    except Exception as e:
        print(f"    AVISO: error en /settings/profile: {e}")

    raise RuntimeError("No se pudo derivar la URL del perfil — ¿sesión expirada?")


def scrape_drafts(page, member_url: str) -> list[dict]:
    """Return a list of drafts on the user's profile.

    Each entry: {"item_id": str, "edit_url": str, "title": str}.
    Identifies drafts via the presence of `[data-testid$="--status-text"]` with text
    "Borrador" inside the item card, and extracts the edit URL from the nested
    `a[href$="/edit"]`.
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
        print(f"    Diag perfil: url={diag.get('url')}")
        print(f"      status-text={diag.get('status_text_count')} samples={diag.get('status_text_samples')}")
        print(f"      edit-links={diag.get('edit_link_count')} samples={diag.get('edit_link_samples')}")
        print(f"      product-cards={diag.get('product_card_count')}  item-links={diag.get('any_item_link_count')}")
        print(f"      tabs={diag.get('tabs')}")
    except Exception as e:
        print(f"    AVISO: diag falló: {e}")

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
      _guess_wallapop_key; resolved mappings are persisted back to category_mapping.json
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
    print(f"    Campos detectados: {[(f.get('testid'), f.get('label'), f.get('kind')) for f in fields]}")
    for f in fields:
        testid = f.get("testid") or ""
        label = f.get("label") or ""
        kind = f.get("kind") or "other"
        if not testid or "condition" in testid:
            continue  # condition already handled by set_condition()
        key = _testid_to_key(testid)
        cfg = attrs_cfg.get(key)

        # First encounter: try to resolve against Wallapop attrs and record in config
        if cfg is None:
            guessed = _guess_wallapop_key(label, wl_attrs) or _guess_wallapop_key(key, wl_attrs)
            cfg = {"label": label, "from": guessed, "kind": kind}
            if _normalize_label(label) == "marca":
                cfg["fallback"] = "no_brand"
            attrs_cfg[key] = cfg
            dirty = True
            if guessed:
                new_mappings.append(f"{label!r} ← {guessed}")

        from_key = cfg.get("from")
        value = wl_attrs.get(from_key) if from_key else None
        if isinstance(value, (int, float)):
            value = str(value)
        # If Wallapop had no value (or the field is unmapped), use the category default
        # when one is configured. Useful for fields whose value is stable per category
        # (SIM lock: "Desbloqueada"; game rating: "PEGI 12").
        if not value and cfg.get("default"):
            value = cfg["default"]

        def _fill(t: str, v: str):
            return fill_combobox(page, t, v) if kind == "combobox" else fill_dropdown(page, t, v)

        if value:
            # Color values come from Wallapop as canonical English keys (orange, black…).
            # Translate to Spanish first; per-attribute value_map still wins if set.
            if from_key == "color" and value in COLOR_EN_TO_ES:
                value = COLOR_EN_TO_ES[value]
            value_mapped = (cfg.get("value_map") or {}).get(value, value)
            ok, options = _fill(testid, value_mapped)
            if ok:
                print(f"    {label}: {value_mapped}")
            else:
                print(f"    AVISO: no se encontró opción {value_mapped!r} en {label!r}")
                # Brand combobox: fall back to "Publicar sin marca" if we can find it
                if cfg.get("fallback") == "no_brand" and select_no_brand(page, testid):
                    print(f"    {label}: Publicar sin marca (fallback)")
                missing.append(label)
                if options and "observed_options" not in cfg:
                    cfg["observed_options"] = options[:30]
                    dirty = True
        else:
            if cfg.get("fallback") == "no_brand":
                if select_no_brand(page, testid):
                    print(f"    {label}: Publicar sin marca (fallback)")
                missing.append(label)
            else:
                missing.append(label)
                if cfg.get("from") is None:
                    if "observed_options" not in cfg:
                        _, options = _fill(testid, "__never_match__")
                        if options:
                            cfg["observed_options"] = options[:30]
                            dirty = True
                    unresolved.append((label, ", ".join(cfg.get("observed_options", [])[:8])))

    if learn and dirty:
        try:
            _save_categories()
        except Exception as e:
            print(f"    AVISO: no se pudo escribir category_mapping.json: {e}")

    return missing, new_mappings, unresolved


def upload_item(page, item: dict, learn: bool = True) -> dict:
    """Upload one item to Vinted. Tries to publish; falls back to draft on validation failure.

    Returns {vinted_id, status, missing_fields, error, new_mappings, unresolved}.
    status ∈ {"published","draft","failed"}. Empty dict-ish only on unrecoverable setup errors.
    """
    empty = {"vinted_id": "", "status": "failed", "missing_fields": [],
             "error": "", "new_mappings": [], "unresolved": []}

    title = item.get("title", "")
    if not title:
        print("  AVISO: artículo sin título, saltando.")
        return {**empty, "error": "sin título"}
    print(f"  Subiendo: {title}")

    _ITEMS_NEW = f"{BASE_URL}/items/new"
    page.goto(_ITEMS_NEW)
    try:
        page.wait_for_url(lambda url: _ITEMS_NEW in url, timeout=120000)
    except PlaywrightTimeout:
        print(f"    Error: no se llegó a /items/new (URL: {page.url})")
        return {**empty, "error": "no /items/new"}

    try:
        page.wait_for_selector(
            "input[data-testid='title--input']", state="visible", timeout=15000
        )
    except PlaywrightTimeout:
        print("    Error: el formulario no se cargó correctamente.")
        return {**empty, "error": "formulario no cargado"}
    human_delay(1, 2)

    image_paths = [p for p in item.get("images", []) if p and Path(p).exists()]
    if not image_paths:
        print(f"    AVISO: No hay imágenes para '{title}', saltando.")
        return {**empty, "error": "sin imágenes"}

    try:
        page.locator("input[data-testid='add-photos-input']").set_input_files(image_paths)
        human_delay(2, 4)
    except Exception as e:
        print(f"    Error subiendo imágenes: {e}")
        return {**empty, "error": f"imágenes: {e}"}

    try:
        human_type(page.locator("input[data-testid='title--input']"), title)
        human_delay(0.5, 1)
    except Exception as e:
        print(f"    Error rellenando título: {e}")
        return {**empty, "error": f"título: {e}"}

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
    if nav:
        select_category(page, nav)
        human_delay(0.5, 1.0)
        set_condition(page)
        human_delay(0.5, 1.0)
    else:
        print(f"    AVISO: sin categoría para category_id={cat_id}")

    missing, new_mappings, unresolved = [], [], []
    if cat_id and nav:
        missing, new_mappings, unresolved = fill_dynamic_attributes(page, item, cat_id, learn)

    # Shipping package size: Wallapop provides 'up_to_kg' for every item, Vinted requires
    # selecting one of the weight tiers (5/10/20/30 kg) before publishing.
    wl_attrs = item.get("attributes") or {}
    select_package_size(page, wl_attrs.get("up_to_kg"))

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
        print(f"    AVISO: precio 0 — déjalo vacío y rellénalo en el borrador.")

    vinted_id, status, errors = publish_or_draft(page, fallback_id_seed=str(item.get("id", "")))
    return {
        "vinted_id": vinted_id,
        "status": status,
        "missing_fields": missing,
        "error": "; ".join(errors) if errors else "",
        "new_mappings": new_mappings,
        "unresolved": unresolved,
    }


def retry_draft_item(page, item: dict, draft_edit_url: str, draft_item_id: str, learn: bool) -> dict:
    """Open an existing Vinted draft and re-attempt publish with updated attributes.

    Differs from upload_item: we navigate to /items/<id>/edit instead of /items/new,
    so the form comes pre-populated with whatever was saved last time. We re-apply
    fill_dynamic_attributes (color EN→ES, category defaults) to fill anything that
    was missing, then try to publish. Returns the same shape as upload_item.
    """
    empty = {"vinted_id": draft_item_id, "status": "draft", "missing_fields": [],
             "error": "", "new_mappings": [], "unresolved": []}

    title = item.get("title", "")
    print(f"  Reintentando draft {draft_item_id}: {title}")
    page.goto(draft_edit_url)
    try:
        page.wait_for_selector(
            "input[data-testid='title--input']", state="visible", timeout=30000
        )
    except PlaywrightTimeout:
        print("    Error: el formulario del draft no se cargó.")
        return {**empty, "status": "failed", "error": "draft no cargó"}
    human_delay(1.5, 2.5)

    cat_id = str(item.get("category_id") or "")
    missing, new_mappings, unresolved = [], [], []
    if cat_id:
        missing, new_mappings, unresolved = fill_dynamic_attributes(page, item, cat_id, learn)

    wl_attrs = item.get("attributes") or {}
    select_package_size(page, wl_attrs.get("up_to_kg"))

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
    parser.add_argument("--limit", type=int, default=None, help="Máximo de artículos a procesar")
    parser.add_argument(
        "--no-learn",
        action="store_true",
        help="No escribir mapeos auto-aprendidos en data/category_mapping.json",
    )
    parser.add_argument(
        "--retry-drafts",
        action="store_true",
        help="En vez de subir items nuevos, abre cada borrador existente en Vinted e intenta publicarlo.",
    )
    args = parser.parse_args()

    if not EMAIL or not PASSWORD:
        print("ERROR: Define VINTED_EMAIL y VINTED_PASSWORD en tu archivo .env")
        sys.exit(1)

    if not ITEMS_PATH.exists():
        print(f"ERROR: No se encuentra {ITEMS_PATH}. Ejecuta primero extract_wallapop.py")
        sys.exit(1)

    items_data = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    # Inject the dict key (Wallapop item ID) into each item dict for uniform access
    items = [{"id": k, **v} for k, v in items_data.items()]

    # In retry mode, --limit applies to matched drafts, not to the full Wallapop list
    if args.limit and not args.retry_drafts:
        items = items[: args.limit]

    # migration.json provides idempotency: items with a vinted_id are skipped on re-runs
    migration = load_migration()
    already_done = sum(1 for v in migration.values() if v.get("vinted_id"))
    if already_done:
        print(f"Artículos ya subidos (se saltarán): {already_done}")
    print(f"Artículos a procesar: {len(items)}")

    published = 0
    drafted = 0
    failed = 0
    unmapped: list[tuple[str, str, str]] = []  # (item_id, title, category_id) — no Vinted path
    drafts_summary: list[tuple[str, str, list[str]]] = []  # (cat_id, title, missing)
    all_new_mappings: list[tuple[str, str]] = []  # (cat_id, "Label ← key")
    all_unresolved: list[tuple[str, str, str]] = []  # (cat_id, label, options_preview)

    with sync_playwright() as p:
        # headless=False is intentional: allows manual intervention for DataDome captchas
        # Patchright is used instead of stock Playwright for bot-detection evasion
        # (patches Runtime.enable CDP signal, navigator.webdriver, and --enable-automation)
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        ctx_kwargs = dict(
            viewport={"width": 1440, "height": 900},
            locale="es-ES",
            timezone_id="Europe/Madrid",
        )
        if AUTH_STATE_PATH.exists():
            ctx_kwargs["storage_state"] = str(AUTH_STATE_PATH)
            print("Sesión guardada encontrada.")

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
        try:
            page.wait_for_url(
                lambda url: _ITEMS_NEW in url or any(p in url for p in _AUTH_PATHS),
                timeout=120000  # allow time to solve DataDome manually if it appears
            )
        except PlaywrightTimeout:
            pass

        if any(p in page.url for p in _AUTH_PATHS):
            login(page)
            context.storage_state(path=str(AUTH_STATE_PATH))
            print(f"  Sesión guardada en {AUTH_STATE_PATH}")
        elif _ITEMS_NEW in page.url:
            print("  Sesión activa.")
            context.storage_state(path=str(AUTH_STATE_PATH))  # refresh persisted state
        else:
            print(f"  Estado inesperado ({page.url}), intentando login...")
            login(page)
            context.storage_state(path=str(AUTH_STATE_PATH))
            print(f"  Sesión guardada en {AUTH_STATE_PATH}")

        # --retry-drafts: iterate over existing drafts on Vinted instead of new uploads
        if args.retry_drafts:
            try:
                member_url = get_member_url(page)
                print(f"Perfil: {member_url}")
                drafts = scrape_drafts(page, member_url)
                print(f"Borradores encontrados en Vinted: {len(drafts)}")
            except Exception as e:
                print(f"ERROR al listar borradores: {e}")
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
            print(f"Drafts matcheados contra items de Wallapop: {len(retry_targets)}")
            if ambiguous:
                print(f"  ambiguos (saltados): {len(ambiguous)} — {ambiguous[:3]}")
            if no_match:
                print(f"  sin match (saltados): {len(no_match)} — {no_match[:3]}")

            for i, (item, edit_url, draft_item_id) in enumerate(retry_targets):
                item_id = item.get("id", "")
                title = item.get("title", "")
                print(f"\n[{i+1}/{len(retry_targets)}] {title}")
                try:
                    result = retry_draft_item(page, item, edit_url, draft_item_id, learn=not args.no_learn)
                except Exception as e:
                    print(f"  Error inesperado: {e}")
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
                f"\nRetry completado: {published} publicados, {drafted} siguen en borrador, "
                f"{failed} fallidos"
            )
            if drafts_summary:
                print(f"\nBorradores pendientes:")
                for cat, t, miss in drafts_summary:
                    miss_str = ", ".join(miss) if miss else "(revisa el formulario)"
                    print(f"  [{cat}] {t[:60]:60s}  falta: {miss_str}")
            if all_new_mappings:
                print(f"\nNuevos mapeos auto-aprendidos (guardados en category_mapping.json):")
                for cat, m in all_new_mappings:
                    print(f"  [{cat}] {m}")
            if all_unresolved:
                print(f"\nMapeos sin resolver (from:null en category_mapping.json):")
                seen: set[tuple[str, str]] = set()
                for cat, label, opts in all_unresolved:
                    k = (cat, label)
                    if k in seen:
                        continue
                    seen.add(k)
                    print(f"  [{cat}] {label!r}  opciones: {opts or '(ninguna)'}")
            return

        for i, item in enumerate(items):
            item_id = item.get("id", "")
            title = item.get("title", item_id or f"item-{i+1}")
            print(f"\n[{i+1}/{len(items)}] {title}")

            prev = migration.get(item_id, {}) if item_id else {}
            if prev.get("vinted_id") and migration_status(prev) in ("published", "draft"):
                print("  Ya subido, saltando.")
                continue

            # Skip items whose category has no Vinted path yet (vinted=null in category_mapping.json)
            nav = get_nav(item)
            if nav is None:
                cat_id = item.get("category_id", "?")
                print(f"  Sin mapeo Vinted para category_id={cat_id} — saltando.")
                unmapped.append((item_id, title, cat_id))
                continue

            try:
                result = upload_item(page, item, learn=not args.no_learn)
            except Exception as e:
                print(f"  Error inesperado: {e}")
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
        f"\nSubida completada: {published} publicados, {drafted} borradores, "
        f"{failed} fallidos, {len(unmapped)} sin mapeo"
    )
    if drafts_summary:
        print(f"\nBorradores que necesitan completarse en Vinted:")
        for cat, t, miss in drafts_summary:
            miss_str = ", ".join(miss) if miss else "(revisa el formulario)"
            print(f"  [{cat}] {t[:60]:60s}  falta: {miss_str}")
    if all_new_mappings:
        print(f"\nNuevos mapeos auto-aprendidos (guardados en category_mapping.json):")
        for cat, m in all_new_mappings:
            print(f"  [{cat}] {m}")
    if all_unresolved:
        print(f"\nMapeos sin resolver (from:null en category_mapping.json, revísalos):")
        seen: set[tuple[str, str]] = set()
        for cat, label, opts in all_unresolved:
            k = (cat, label)
            if k in seen:
                continue
            seen.add(k)
            print(f"  [{cat}] {label!r}  opciones: {opts or '(ninguna)'}")
    if unmapped:
        print(f"\nCategorías sin ruta Vinted ({len(unmapped)}) — añade la ruta en data/category_mapping.json:")
        for uid, utitle, ucat in unmapped:
            print(f"  category_id={ucat}  →  {utitle[:60]}")


if __name__ == "__main__":
    main()
