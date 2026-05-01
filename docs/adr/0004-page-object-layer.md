# 4. Extract the Page Object layer + introduce HTML fixture testing

Date: 2026-04-26
Status: Accepted (2026-04-27, in progress — steps 0-2 landed)

## Research

Reviewed 2026-04-25 (WebFetch + targeted reading) before drafting this ADR. Persisted here so future maintainers don't have to re-investigate.

- **Fowler, "PageObject"** (https://martinfowler.com/bliki/PageObject.html): the canonical write-up describes the pattern's interface and benefits but **does not address unit-testing of Page Objects**. The implicit assumption is that POMs are consumed by tests, not tested as units.
- **Playwright official guidance** (https://playwright.dev/docs/pom): POMs are injected into E2E specs via `test.extend` fixtures. `page.set_content(html)` exists in the API and is mentioned in the docs for component-style testing, but the POM pages explicitly recommend exercising the object through real navigations.
- **Tutorial sources** (Sudolabs, NashTech, BrowserStack guides reviewed): all describe the same pattern — POMs consumed by real-navigation E2E. None propose testing POMs against captured HTML.

**Orthodox conclusion**: test Page Objects only indirectly, through end-to-end runs.

**Why we deviate** (deliberate, scoped):

1. **Live E2E against Vinted is gated by DataDome.** Every navigation can trigger a captcha — non-deterministic, slow, unsuitable for an automated suite. Re-running the same test 50× in 5 seconds against a fixture is feasible; running 50× against Vinted is not.
2. **The 2 known bugs are pure selector issues.** They reproduce deterministically against captured HTML, which makes fixture-driven TDD a strict win for *those specific cases*: write red test → fix selector → verify green → ship. Iterating without a fixture means iterating against `--limit 1 --visible`, paying DataDome risk and minutes per cycle.
3. **ARIA snapshots** (Playwright `expect(...).to_match_aria_snapshot(...)`): a forward-looking technique surfaced during the research. Captures the accessibility tree as YAML — when Vinted reorganizes a form, the snapshot fails loudly. Cheaper than enumerating selectors per verb.

**The deviation does not extend to every verb.** Trivial verbs (`fill_title`, `set_condition` simple) are still validated by `--limit 1 --visible`. The Coverage section below makes the cost/benefit explicit.

## Context

After ADR-0001 (domain layer), ADR-0002 (Wallapop side), and ADR-0003 (Vinted session + error taxonomy), `upload_vinted.py` still concentrates ≈1690 lines mixing DOM selectors with application flow: `login()`, `select_category()`, `set_condition()`, `scan_dynamic_fields()`, `fill_dropdown()`, `fill_combobox()`, `select_package_size()`, `select_no_brand()`, `collect_form_errors()`, `publish_or_draft()`, `retry_draft_item()`, `get_member_url()`, `scrape_drafts()`, plus a handful of helpers (`human_delay`, `human_type`, JS click utilities). This is Phase 4 of the refactor described in ADR-0001: the Page Object layer.

Two concrete bugs are riding along with the refactor and are worth fixing in this phase because they live entirely in the selectors:

1. **Package size**: clicking `[data-testid='2-package-size--cell']` does not propagate to the inner `<input type="radio">`, so Vinted rejects the listing with "Selecciona el tamaño del paquete". Symptom variant: when the category is a non-leaf, the package block is not rendered at all and the current `wait_for(state="visible", timeout=5000)` burns five seconds while Playwright's visibility retry triggers the "scroll absurdo" the user observed in `--visible` mode.
2. **`scan_dynamic_fields` for books**: the current selector (`*-single-list-input`, `*-dropdown-input`) misses the testids Vinted uses for ISBN/Author/Publisher/Language/Format. Books therefore log `Fields detected: []` and fall back to draft. ISBN is the keystone — once filled, Vinted auto-populates the rest.

There is no test infrastructure for the Page Object layer today: the only feedback loop is a real Vinted upload via `python upload_vinted.py --limit 1 --visible`, which is slow, costs a DataDome captcha each time, and is non-deterministic when the upload flow caches state from previous attempts.

## Decision

Extract the Page Object layer into `vinted/pages/` with one module per page (`login.py`, `new_item.py`, `edit_draft.py`, `profile.py`) and a shared helpers module (`_common.py`). Each Page Object is a class that takes a Playwright `page` in its constructor and exposes intent-named verbs (`fill_title`, `select_condition`, `publish_or_draft`) that hide the selectors. Page Objects raise the existing taxonomy from `vinted/errors.py` (`LoginFailed`, `PublishRejected`); the captcha detection wrapper stays in `main()` where the operator-facing exit code translation already lives.

Pure functions still tangled in `upload_vinted.py` (`_extract_item_id_from_url`, `_is_form_url`, `_testid_to_key`, `_normalize_title`, `match_draft_to_item`) move to `domain/` first because they don't depend on the browser and can be tested without `pytest-playwright`.

For testing, this phase **deliberately departs from the orthodox Page Object testing approach** documented in Fowler's bliki and the official Playwright docs. The mainstream guidance is to test Page Objects only indirectly through end-to-end runs that consume them — but in our case every E2E run is a real navigation against Vinted, gated by DataDome, and produces non-deterministic state. We therefore adopt a fixture-HTML approach:

- A new `scripts/capture_vinted_fixtures.py` script, run once per scope by the maintainer in `--visible` mode, navigates to each critical surface (`/items/new`, `/items/<id>/edit`, login error toast, publish error toast, profile draft list) and dumps `page.content()` to `tests/fixtures/vinted_html/<scope>.html`.
- Tests load fixtures with `page.set_content(html)` via `pytest-playwright` and exercise Page Object verbs against the captured DOM. Coverage is **selective**: the two bug-fix scenarios are red→green TDD cases, and a handful of fragile verbs (`scan_dynamic_fields`, combobox strict-match, error parsers) get fixture tests; trivial verbs (`fill_title`, simple `set_condition`) keep being validated by `--limit 1 --visible`.
- One ARIA snapshot test per fixture (`expect(page.locator("body")).to_match_aria_snapshot(...)`) acts as a structural regression detector. When Vinted reorganizes a form, the snapshot fails loudly; the maintainer re-captures, reviews the diff, and updates the snapshot.
- `pytest-playwright>=0.5` is added to `requirements-dev.txt`. Tests that need a browser are tagged `@pytest.mark.playwright` so the existing 149 non-browser tests keep running without Chromium. New `make test-fast` (no browser) and `make test-browser` (browser only) targets sit alongside the existing `make test` (everything).

## Consequences

- **Positive.** The two known bugs (package-size click, books scanner) get reproducible failing tests before the fix lands, which is impossible today. The fixture HTML doubles as a structural regression detector via ARIA snapshots, so a Vinted DOM change announces itself in CI instead of silently breaking uploads. Page Objects become composable for the orchestrator extraction in Phase 5: a fake `page` plus a real `NewItemPage` is enough to dry-run the upload flow end-to-end. The `playwright` marker keeps the fast loop fast — most tests still run without Chromium.
- **Negative.** Fixtures are point-in-time captures. Every meaningful Vinted DOM refresh requires the maintainer to re-run the capture script, review the diff, and re-commit the HTML. ARIA snapshots mitigate this by failing loudly rather than rotting silently, but the manual re-capture step is real ongoing cost. We accept it as cheaper than the alternative (live E2E, captcha-gated, slow). Adding `pytest-playwright` and Chromium to the dev dependency footprint is a non-trivial install cost; the marker split keeps it optional for contributors who only want to run the domain/wallapop/vinted-session tests.
- **Open.** Whether `EditDraftPage` should compose `NewItemPage` (delegate to it for shared verbs like `fill_title` / `publish_or_draft`) or whether both should derive from a shared base mixin is left to the implementation step — the duplication will only become visible when the second module lands. The decision is not load-bearing; either approach removes the same lines from `upload_vinted.py`.

## Progress

**Step 0 — Tooling. ✅ 2026-04-26 (`4a710fa`).** `pytest-playwright>=0.5` added; `scripts/capture_vinted_fixtures.py` skeleton; `make test-fast` / `make test-browser` targets.

**Step 1 — Pure helpers to `domain/`. ✅ 2026-04-26 (`86d8f1b`).** `_extract_item_id_from_url`, `_is_form_url` → `domain/urls.py`. `_normalize_title`, `match_draft_to_item` → `domain/drafts.py`. `_testid_to_key` → `domain/text.py`.

**Step 2 — Fixture infrastructure + first capture. ✅ 2026-04-27 (`ff2960d`, `8b19be3`).** `tests/conftest.py` exposes `load_vinted_fixture(scope)` which loads HTML via `page.set_content(..., wait_until="domcontentloaded")` — the default `"load"` raced against the ~2 MB fixture's external CDN URLs and produced flaky failures. `tests/vinted/pages/test_fixtures.py` parametrised anchor regression test fails loudly when a captured fixture loses its anchor selector. First fixture: `tests/fixtures/vinted_html/new_item.html` (anchor: `input[data-testid='title--input']`).

**Step 2.5 — Login/navigation unblocked (unplanned). ✅ 2026-04-27.** Capturing the fixture broke the live login flow and we burnt 4 commits triangulating DataDome's selectivity:

- `4d35edd` rewrote login with native `locator.click()` + `domcontentloaded` — **broke stealth**. DataDome flagged the CDP-issued mouse events behind `locator.click()`.
- `97e4587` reverted to `_js_click_button` (synthetic `el.click()` via `page.evaluate`) for the 4 login clicks and added `channel="chrome"` to `build_session` (Patchright's full stealth requires real Chrome — bundled "Chrome for Testing" is detectable). Locked the contract with `tests/test_upload_login.py` (2 tests asserting 4× `_js_click_button` and 0× native click in `login()`).
- `6f90b36` propagated `wait_until="domcontentloaded"` to every `page.goto(...)` (the Playwright default `"load"` hung 30 s on Vinted's telemetry-heavy pages) and dropped the post-login `wait_for_url` (after `dcl` the URL is already final). `tests/test_upload_navigation.py` adds an AST check that no future `page.goto` lands without `wait_until=`.
- `d43df90` skips the redundant `/items/new` goto for the first item (bootstrap leaves the browser there).

Lessons that outlive the project:

- Patchright requires `channel="chrome"` for full stealth. Bundled Chromium ("Chrome for Testing") is detectable.
- DataDome distinguishes CDP-driven clicks (`locator.click()`) from synthetic JS clicks (`el.click()` via `page.evaluate`). The latter is stealthier for auth buttons.
- On Vinted, `page.goto(..., wait_until="domcontentloaded")` is the safe default — the `"load"` default hangs on telemetry/analytics requests.

Validated in production: `python upload_vinted.py --limit 1 --visible` publishes 1/1 without DataDome slider. 184 tests green.

**Step 3 — NewItemPage + package-size fix. ✅ DONE (2026-05-01).** `vinted/pages/new_item.py` extracted from `upload_vinted.py`. Package-size bug fixed: now clicks `[data-testid='package_type_selector_2--input']` (radio input) instead of the cell div, with 2 s timeout (was 5 s) so non-leaf fallback returns immediately. Shared utilities (`human_delay`, `human_type`, `js_click_button`, `_PANEL_SELECTOR`) moved to `vinted/pages/_common.py`; `upload_vinted.py` imports them from there. Two new playwright tests (`test_select_package_size_checks_radio_input`, `test_select_package_size_returns_false_when_block_absent`) confirm the fix. Books scanner test (`test_scan_dynamic_fields_finds_isbn_field`) auto-skips until `new_item_books` fixture is captured; capture handler added to `scripts/capture_vinted_fixtures.py`. 186 tests, 1 skipped.

**Step 4+ — remaining Page Objects (`login.py`, `edit_draft.py`, `profile.py`). ⏳ Pending.** `login()`, `get_member_url()`, `scrape_drafts()` remain in `upload_vinted.py` for now. Once `new_item_books` fixture is captured and the books scanner is fixed, then extract those pages.
