"""Page Object for Vinted's /items/new upload form.

Encapsulates every DOM interaction for creating a new listing: category
selection, condition, dynamic attribute dropdowns, package size, brand
fallback, and the publish/draft flow. Raises ``vinted.errors`` exceptions
for flow-level failures; per-field misses are returned as (ok, options)
so the orchestrator can decide whether to draft.

Usage::

    page_obj = NewItemPage(page)
    cat_ok, sub_opts = page_obj.select_category(nav)
    page_obj.set_condition()
    fields = page_obj.scan_dynamic_fields()
    vinted_id, status, errors = page_obj.publish_or_draft(fallback_id_seed="abc")
"""

from __future__ import annotations

from domain.text import find_option_match
from domain.urls import extract_item_id_from_url, is_form_url
from vinted.pages._common import _PANEL_SELECTOR, human_delay, js_click_button

from patchright.sync_api import TimeoutError as PlaywrightTimeout

_CONDITION_DEFAULT = "Nuevo sin etiquetas"
_PUBLISH_BUTTON_TESTID = "upload-form-post-button"
_DRAFT_BUTTON_TESTID = "upload-form-save-draft-button"


class NewItemPage:
    """DOM interaction layer for Vinted's item-upload form (/items/new)."""

    def __init__(self, page):
        self.page = page

    # ------------------------------------------------------------------
    # Category
    # ------------------------------------------------------------------

    def select_category(self, nav: list) -> tuple[bool, list[str]]:
        """Navigate Vinted's category picker by clicking through each tree level.

        Returns (leaf_reached, sub_options).
          - leaf_reached=True when the picker auto-closes after the last click.
          - leaf_reached=False + non-empty sub_options when the path stopped
            short of a leaf.
          - leaf_reached=False + empty sub_options on exception.
        """
        if not nav:
            return False, []
        try:
            self.page.click("input[data-testid='catalog-select-dropdown-input']")
            human_delay(0.5, 1.0)
            for step in nav:
                btn = self.page.locator('div[role="button"]').filter(
                    has=self.page.locator(f':text-is("{step}")')
                ).first
                btn.wait_for(state="visible", timeout=5000)
                btn.click()
                human_delay(0.4, 0.8)

            human_delay(0.6, 1.0)
            sub_options = self.page.evaluate(
                """() => Array.from(document.querySelectorAll('[id^="catalog-"]'))
                         .filter(el => el.offsetParent !== null)
                         .map(el => (el.innerText || '').trim().split('\\n')[0])
                         .filter(t => t.length > 0 && t.length < 80)
                         .slice(0, 10)"""
            ) or []
            if sub_options:
                print(
                    f"    WARNING: nav path {' > '.join(nav)!r} is not a leaf "
                    f"(sub-options: {sub_options}) — falling back to draft."
                )
                try:
                    self.page.keyboard.press("Escape")
                    human_delay(0.3, 0.6)
                except Exception:
                    pass
                return False, sub_options

            print(f"    Category: {' > '.join(nav)}")
            return True, []
        except Exception as e:
            print(f"    WARNING: could not select category {nav}: {e}")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False, []

    # ------------------------------------------------------------------
    # Condition
    # ------------------------------------------------------------------

    def set_condition(self, condition: str = _CONDITION_DEFAULT) -> bool:
        """Select item condition. Silently skips if the field isn't visible."""
        try:
            cond = self.page.locator("[data-testid='category-condition-single-list-input']")
            cond.wait_for(state="visible", timeout=3000)
            cond.click()
            human_delay(0.3, 0.6)
            self.page.locator('div[role="button"]').filter(
                has=self.page.locator(f':text-is("{condition}")')
            ).first.click()
            print(f"    Condition: {condition}")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Dynamic attribute scanner
    # ------------------------------------------------------------------

    def scan_dynamic_fields(self) -> list[dict]:
        """Enumerate visible attribute dropdowns currently rendered on the upload form.

        Returns a list of {testid, label, kind} for every input-like element
        exposed by Vinted for the selected category, skipping the fixed fields
        (category picker, title, price, description, photos). 'kind' is
        'dropdown' for single-list pickers, 'combobox' for autocomplete
        inputs, and 'other' for anything else.
        """
        try:
            return self.page.evaluate("""
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

    # ------------------------------------------------------------------
    # Dropdown helpers (private)
    # ------------------------------------------------------------------

    def _collect_visible_options(self) -> list[str]:
        """Read all option labels from the currently open Vinted dropdown panel."""
        try:
            return self.page.evaluate(f"""
            () => {{
                const panel = document.querySelector('{_PANEL_SELECTOR}');
                const root = panel || document;
                const seen = new Set();
                const out = [];
                root.querySelectorAll('[data-testid$="--title"]').forEach(el => {{
                    const text = (el.innerText || '').trim();
                    if (text.length > 0 && text.length < 80 && !seen.has(text)) {{
                        seen.add(text); out.push(text);
                    }}
                }});
                if (out.length) return out;
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

    def _js_click_option(self, label: str) -> None:
        """Click an option in the open dropdown panel by its label text."""
        self.page.evaluate(
            f"""(label) => {{
                const panel = document.querySelector('{_PANEL_SELECTOR}');
                const root = panel || document;
                const titleEl = Array.from(root.querySelectorAll('[data-testid$="--title"]'))
                    .find(el => (el.innerText || '').trim() === label);
                if (titleEl) {{
                    const btn = titleEl.closest('div[role="button"]');
                    if (btn) {{ btn.click(); return; }}
                }}
                const btn = Array.from(root.querySelectorAll('div[role="button"]'))
                    .find(el => (el.innerText || '').trim().split('\\n')[0].trim() === label);
                if (btn) btn.click();
            }}""",
            label,
        )

    # ------------------------------------------------------------------
    # Dropdown and combobox fill
    # ------------------------------------------------------------------

    def fill_dropdown(self, testid: str, value: str) -> tuple[bool, list[str]]:
        """Open the dropdown, try to select an option matching ``value``.

        Returns (selected, options_seen). Uses JS click to avoid
        Playwright's auto-scroll which can trigger DataDome.
        """
        options: list[str] = []
        try:
            inp = self.page.locator(f"[data-testid='{testid}']")
            inp.wait_for(state="visible", timeout=3000)
            inp.click()
            human_delay(0.3, 0.6)
            options = self._collect_visible_options()

            matched = find_option_match(options, value)
            if matched is not None:
                self._js_click_option(matched)
                human_delay(0.3, 0.6)
                return True, options
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            human_delay(0.2, 0.4)
            return False, options
        except Exception as e:
            print(f"    WARNING: fill_dropdown({testid}, {value!r}) failed: {e}")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False, options

    def fill_combobox(self, testid: str, value: str) -> tuple[bool, list[str]]:
        """For Vinted's brand/color pickers (input + dropdown suggestions).

        Two-phase: check immediate options on open, then type to filter if
        the input is not readonly. Uses JS click to avoid DataDome.
        Returns (selected, options_seen).
        """
        import time as _time
        options: list[str] = []
        try:
            inp = self.page.locator(f"[data-testid='{testid}']")
            inp.wait_for(state="visible", timeout=3000)
            inp.click()
            human_delay(0.4, 0.7)

            options = self._collect_visible_options()
            matched = find_option_match(options, value)
            if matched is not None:
                self._js_click_option(matched)
                human_delay(0.3, 0.5)
                return True, options

            is_readonly = inp.get_attribute("readonly") is not None
            if is_readonly:
                try:
                    self.page.keyboard.press("Escape")
                except Exception:
                    pass
                human_delay(0.2, 0.3)
                return False, options

            for ch in value:
                inp.press_sequentially(ch)
                _time.sleep(0.04)
            human_delay(0.4, 0.7)
            options = self._collect_visible_options()
            matched = find_option_match(options, value, strict=True)
            if matched is not None:
                self._js_click_option(matched)
                human_delay(0.3, 0.5)
                return True, options

            try:
                inp.fill("")
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            human_delay(0.2, 0.3)
            return False, options
        except Exception as e:
            print(f"    WARNING: fill_combobox({testid}, {value!r}) failed: {e}")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False, options

    def fill_first_option(self, testid: str) -> tuple[bool, list[str]]:
        """Open the dropdown, click the first available option, close.

        Used as a last-resort fill for Color when Wallapop has no colour
        value. Vinted's "Sugerencias" (category-aware guesses) appear at the
        top, so the first option is the best automatic choice.
        """
        try:
            inp = self.page.locator(f"[data-testid='{testid}']")
            inp.wait_for(state="visible", timeout=3000)
            inp.click()
            human_delay(0.3, 0.6)
            options = self._collect_visible_options()
            if not options:
                try:
                    self.page.keyboard.press("Escape")
                except Exception:
                    pass
                return False, []
            self._js_click_option(options[0])
            human_delay(0.3, 0.5)
            return True, options
        except Exception as e:
            print(f"    WARNING: fill_first_option({testid}) failed: {e}")
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False, []

    # ------------------------------------------------------------------
    # Package size
    # ------------------------------------------------------------------

    def select_package_size(self) -> bool:
        """Pick the 'Mediano' (id=2) package size by clicking its radio input.

        Always picks Mediano — the safe default for the bulk of the catalogue.
        Returns False immediately (no wait, no scroll) when the package block
        is absent, which happens when the category is a non-leaf (draft path).

        Bug fixed here vs. the original upload_vinted.py: the previous code
        clicked ``[data-testid='2-package-size--cell']`` (a div wrapper) which
        does not propagate to the inner ``<input type="radio">``, causing Vinted
        to reject with "Selecciona el tamaño del paquete". This method clicks
        the radio input directly. The timeout is also shortened to 2 s so the
        non-leaf fallback doesn't burn 5 s on Playwright's visibility retries.
        """
        try:
            cell = self.page.locator("[data-testid='2-package-size--cell']")
            cell.wait_for(state="visible", timeout=2000)
        except Exception:
            return False  # block not rendered — non-leaf category or already on edit page

        try:
            radio = self.page.locator("[data-testid='package_type_selector_2--input']")
            radio.click()
            human_delay(0.3, 0.6)
            print("    Shipping package: Mediano (default)")
            return True
        except Exception as e:
            print(f"    WARNING: could not pick package size: {e}")
            return False

    # ------------------------------------------------------------------
    # Brand fallback
    # ------------------------------------------------------------------

    def select_no_brand(self, testid: str) -> bool:
        """Click 'Publicar sin marca' inside the brand dropdown."""
        try:
            inp = self.page.locator(f"[data-testid='{testid}']")
            inp.click()
            human_delay(0.3, 0.6)
            clicked = self.page.evaluate(
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
            self.page.keyboard.press("Escape")
            return False
        except Exception:
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------
    # Error collection
    # ------------------------------------------------------------------

    def collect_form_errors(self) -> list[str]:
        """Read inline validation messages shown after a failed publish attempt.

        Casts a wide net: aria-invalid inputs, legacy error classes, elements
        whose class name contains 'error'/'caution', and a text-prefix heuristic
        for Vinted copy rendered as plain divs without obvious error classes.
        """
        try:
            msgs = self.page.evaluate(r"""
            () => {
              const out = new Set();
              const isVisible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return false;
                const s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
              };
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
              document.querySelectorAll('[class*="error" i], [class*="caution" i], [role="alert"]').forEach(el => {
                if (!isVisible(el)) return;
                const t = (el.innerText || '').trim();
                if (t && t.length < 200) out.add(t);
              });
              const prefixes = /^(rellena|selecciona|introduce|indica|a[nñ]ade|elige|falta|debes)\b/i;
              document.querySelectorAll('div, span, p').forEach(el => {
                if (!isVisible(el)) return;
                if (el.children.length > 0) return;
                const t = (el.innerText || '').trim();
                if (t && t.length < 200 && prefixes.test(t)) out.add(t);
              });
              return Array.from(out);
            }
            """) or []
            return msgs
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Publish / draft flow (private helpers)
    # ------------------------------------------------------------------

    def _dump_form_buttons(self) -> list[dict]:
        try:
            return self.page.evaluate("""
            () => Array.from(document.querySelectorAll('button'))
              .filter(b => b.offsetParent !== null)
              .map(b => ({
                testid: b.getAttribute('data-testid') || '',
                text: (b.innerText || '').trim().slice(0, 40)
              }))
            """) or []
        except Exception:
            return []

    def _find_publish_button(self):
        try:
            btn = self.page.locator(f"button[data-testid='{_PUBLISH_BUTTON_TESTID}']")
            btn.wait_for(state="visible", timeout=3000)
            return btn
        except PlaywrightTimeout:
            pass
        for text in ("Subir", "Publicar", "Subir artículo"):
            try:
                btn = self.page.get_by_role("button", name=text, exact=True)
                btn.wait_for(state="visible", timeout=1500)
                return btn
            except PlaywrightTimeout:
                continue
        return None

    def _dismiss_critical_error_dialog(self) -> bool:
        try:
            dismissed = self.page.evaluate("""
            () => {
                const overlay = document.querySelector(
                    '[data-testid="item-upload-critical-error-dialog--overlay"]'
                );
                if (!overlay) return false;
                const btn = overlay.querySelector('button');
                if (btn) { btn.click(); return true; }
                overlay.click();
                return true;
            }
            """)
            if dismissed:
                human_delay(0.4, 0.7)
            return bool(dismissed)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Publish / draft flow (public)
    # ------------------------------------------------------------------

    def publish_or_draft(
        self,
        fallback_id_seed: str = "",
        save_as_draft_on_fail: bool = True,
    ) -> tuple[str, str, list[str]]:
        """Try to publish; on failure optionally save as draft.

        Returns (vinted_id, status, errors) with status ∈
        {"published", "draft", "failed"}.

        ``save_as_draft_on_fail=False`` is used in retry mode: the draft
        already exists, so when publish fails we leave it and report errors.
        """
        errors: list[str] = []
        pub = self._find_publish_button()
        if pub is None:
            buttons = self._dump_form_buttons()
            print(f"    WARNING: publish button not visible. Form buttons: {buttons}")
        else:
            try:
                js_click_button(self.page, pub)
                try:
                    self.page.wait_for_url(
                        lambda url: not is_form_url(url), timeout=15000
                    )
                except PlaywrightTimeout:
                    pass
                human_delay(1, 2)
                if not is_form_url(self.page.url):
                    vinted_id = (
                        extract_item_id_from_url(self.page.url)
                        or f"published-{fallback_id_seed}"
                    )
                    print(f"    Published: {self.page.url}")
                    return vinted_id, "published", []
                errors = self.collect_form_errors()
                if errors:
                    print(f"    Publish rejected: {errors[:4]}")
                else:
                    print(
                        f"    Clicked publish but no navigation or inline errors "
                        f"— URL still: {self.page.url}"
                    )
            except Exception as e:
                print(f"    WARNING: publish failed: {e}")

        if not save_as_draft_on_fail:
            return "", "draft", errors

        self._dismiss_critical_error_dialog()

        try:
            draft_btn = self.page.locator(f"button[data-testid='{_DRAFT_BUTTON_TESTID}']")
            js_click_button(self.page, draft_btn)
            try:
                self.page.wait_for_url(
                    lambda url: not is_form_url(url), timeout=15000
                )
            except PlaywrightTimeout:
                pass
            human_delay(1, 2)
            if is_form_url(self.page.url):
                print(f"    Error: draft not saved — URL: {self.page.url}")
                return "", "failed", errors
            vinted_id = (
                extract_item_id_from_url(self.page.url)
                or f"draft-{fallback_id_seed}"
            )
            print(f"    Draft saved: {self.page.url} (recorded id: {vinted_id})")
            return vinted_id, "draft", errors
        except Exception as e:
            print(f"    Error saving draft: {e}")
            return "", "failed", errors
