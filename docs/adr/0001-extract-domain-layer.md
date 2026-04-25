# 1. Extract domain layer from `upload_vinted.py`

Date: 2026-04-25
Status: Accepted

## Context

`upload_vinted.py` had grown past 1500 lines and mixed three concerns: Patchright DOM interaction, pure logic on JSON/strings (text normalisation, category tree resolution, attribute mapping, migration state), and orchestration. Every change to the pure logic required reading the whole file and ran the risk of breaking the browser flow, because there were no tests — the only feedback loop was a real Vinted upload, which is slow, captcha-prone, and only validates the happy path of whichever item happens to be next in the queue.

This is the first phase of the 5-phase TDD-driven refactor documented in `CLAUDE.md` (Next Steps #2). It targets the cheapest layer to cover with tests — pure functions — and establishes the pattern (write tests first, then move code) for subsequent phases.

## Decision

Extract pure logic into a `domain/` package with one module per concern:

- `domain/text.py` — label normalisation, stemmer, title softening, option matching.
- `domain/categories.py` — Vinted category tree builder and leaf resolver. Public API takes the category nodes by argument (DI), no module-level state.
- `domain/mapping.py` — Wallapop ↔ Vinted attribute key mapping, label aliases, colour translations.
- `domain/migration.py` — `migration.json` load/save and queue filter.

Public functions follow PEP 8 (no leading underscore). `upload_vinted.py` imports from these modules and keeps only the browser/orchestration code. Tests live under `tests/domain/` and exercise the modules with in-memory fixtures — no browser, no network.

## Consequences

- **Positive.** Pure logic is now covered by 75 fast tests; future changes to text matching, leaf resolution, mapping aliases, or migration state run in milliseconds and don't require a captcha-solving Vinted session. The DI shape (e.g. `migration.json` path passed in) makes testing trivial. Establishes the layout (`domain/`, `tests/domain/`) for `wallapop/` and `vinted/` packages in later phases.
- **Negative.** Two-step refactor for any change touching the boundary (edit the domain module *and* its caller). The domain modules now carry their own docstrings/comments, slightly increasing surface area. Acceptable trade-off given the test coverage gained.
- **Open.** Phases 2-5 (Wallapop side, Vinted session, Page Objects, orchestrator) will follow the same pattern; if the DI shape proves awkward at a phase boundary it gets revisited then, not now.
