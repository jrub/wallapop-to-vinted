# Follow-ups

Tactical backlog for known polish, gaps, and improvements outside the immediate work cycle. Pick from here when starting a session and you don't have a specific bug in mind. Each item should be actionable and self-contained — if it isn't, decompose before adding.

When the public repo gathers traction, migrate items from this file to GitHub Issues (each item maps to one issue). Until then, this file is the source of truth.

For larger architectural decisions and roadmap (Phase 5 orchestrator extraction, multi-country support, etc.) see the **Next Steps** section in `CLAUDE.md`.

---

## Pre-launch review (2026-05-03)

Findings from a structured pre-publish sweep. Critical (🔴) findings shipped in the same cycle as the review. The items below were deferred as non-blocking.

### Should-fix-soon

- **CONTRIBUTING.md for category mapping contributions.** First PR adding a new Wallapop→Vinted category mapping is the natural moment to write this. Should cover: how to find a Wallapop `category_id`, how to navigate Vinted's picker and record the nav path, how to resolve `from: null` entries using `observed_options`, conventions for `value_map` when Spanish/English labels diverge. Cross-reference from README's "Auto-learning category mappings" section. *Tracked in CLAUDE.md Next Steps #1.*

- **`upload_vinted.py` line-count drift in docs.** CLAUDE.md `Refactor` section and `docs/adr/0004-page-object-layer.md` Step 4 quote 788 lines as the post-extraction count. Current file is closer to 940 (post-extraction additions for pre-flight wiring). Either update the numbers or drop the per-step counts and summarise as "main script is the orchestration shell; per-page DOM lives under `vinted/pages/`".

- **README install-order note.** The Workflow section mentions `extract_vinted_categories.py` first, but it's only needed when Vinted restructures its tree — the pre-captured `data/vinted_categories.json` is fine for first-time users. Add a one-liner clarifying it's optional on first run.

### Nice-to-have

- **Screenshot or short GIF in the README.** Single biggest lever for "is this real?" on a public repo. One annotated capture of `--visible` mode (browser open, form filled, draft created) after the Usage section. Capture from a clean test account to avoid redaction.

- **GitHub Actions running `make test` on push/PR.** Test suite is fast (255 tests, ~5s, no Chromium for the default target). Catches regressions on contributor PRs automatically. Add a `test` workflow + a status badge to the README.

- **Issue + PR templates under `.github/`.** A `bug_report.md`, `feature_request.md`, and `PULL_REQUEST_TEMPLATE.md`. Keeps incoming issues structured.

- **`make sanity` target that imports every entry-point script.** Would have caught the `scripts/capture_vinted_fixtures.py` broken `from upload_vinted import login` immediately when `LoginPage` was extracted (the test suite didn't, because nothing imports the script). Cheap insurance for one-shot tools that aren't in the regular test path.

- **Delete `NewItemPage.select_package_size` and its tests.** Currently dead code (no call sites since 2026-05-03 walkback). Kept on "future-use" grounds but per project KISS/SRP rules that's premature — delete it; resurrect from git if Vinted ever stops pre-selecting "Mediano".

- **Sort `data/category_mapping.json` by key.** Once contributors start adding mappings via PRs, merge conflicts on this file will be common. Stable key order improves diffability.

---

## Open questions / observations

Things noticed but not yet decided. Promote to "should-fix-soon" / "nice-to-have" once a direction is chosen, or delete once superseded.

- **`extract_vinted_categories.py` parses Vinted's Next.js streaming payload** — fragile to framework changes. If we add CI, a smoke test that runs this script weekly (and opens an issue on failure) would surface category-tree drift early.

- **Sales / deletion sync** — explicitly out of scope today. If issues pile up asking for it, decide: write it, or harden the "Out of Scope" section in README.
