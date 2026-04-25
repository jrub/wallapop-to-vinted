# 2. Extract Wallapop side into `wallapop/items.py`

Date: 2026-04-25
Status: Proposed

## Context

`extract_wallapop.py` is a single 350-line script that mixes HTTP calls to Wallapop's internal API, image download to disk, the cursor-based pagination loop with circular-detection and `MAX_PAGES` cap, the `__NEXT_DATA__` parser that resolves the numeric user ID, and the in-person filter. With no tests, evolving it (adding the ISBN pre-flight, the interactive leaf resolver, etc., all pending in CLAUDE.md Next Steps) requires hitting the real service and exposes us to changes in Wallapop's internal API at runtime. This is Phase 2 of the refactor described in ADR-0001.

## Decision

Mirror the Phase 1 pattern: write tests first using the `responses` library to mock HTTP, then move code into a new `wallapop/` package (currently `wallapop/items.py`; further modules added if a later phase needs them). Public API takes paths and the `requests.Session` by argument — no module-level state. `process_item` is split into three smaller functions (`build_item_record` pure, `download_item_images` I/O, `process_item` orchestrator) so the bulk of its logic can be tested without nested mocks.

`extract_wallapop.py` keeps its CLI and `.env` loading, becoming a thin entry point that wires the new module.

## Consequences

- **Positive.** Wallapop-side logic becomes test-covered without a live API call. The split of `process_item` makes the output shape explicit and easy to evolve. Establishes the scaffolding for `wallapop/matching.py` (Phase 4) when fuzzy item-to-leaf matching needs its own home.
- **Negative.** One more dev dependency (`responses`). The split functions add slight indirection vs the current single-function shape — paid back the first time we change the output dict.
- **Open.** Whether `download_image` and `fetch_leaf_category_id` need their own tests is deferred — they're covered transitively via `process_item`; if a bug surfaces there we add a focused test.
