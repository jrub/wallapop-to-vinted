# 5. Pre-flight prompts for required Vinted fields missing on Wallapop

Date: 2026-05-03
Status: Accepted (2026-05-03); ISBN gather + leaf disambiguation shipped (commits `4e70a7a`, `d8b7091`, `8b4364e`, `7cbd670`)

## Context

Vinted's per-category form requires fields that Wallapop never asks the seller for. Two examples are load-bearing today:

1. **ISBN for books.** Vinted refuses to publish anything under "Libros" without a valid ISBN ("Introduce el ISBN para continuar"). Wallapop treats ISBN as an optional free-text attribute that most sellers leave blank, so the catalogue arrives empty for this field. Filling the ISBN is also high-leverage — Vinted then auto-populates Autor / Editorial / Idioma / Formato, so per-field plumbing for the books category isn't needed *if* we can get the ISBN in.

2. **Leaf disambiguation for vague Wallapop categories.** Wallapop lets a seller stop at any node in its tree (e.g. "Componentes de PC" rather than a leaf like RAM or GPU), and a single Wallapop `category_id` can legitimately span multiple Vinted leaves. The local resolver (`domain.categories.resolve_nav_to_leaf` + `pick_leaf_from_hints`) extends a partial nav path to a leaf when exactly one sub-option matches the item's title/description after stemming. When zero or multiple sub-options match, the resolver returns the partial path and the browser-side picker either (a) makes a wrong guess or (b) bails to draft. The right call needs a human looking at the title.

Until this ADR, both cases were "walk the operator through Vinted's UI for each draft". Operationally that meant 30+ drafts per run for an inventory with books, and an opaque trail of "why did this end up in drafts?" since the draft-fallback path doesn't tell the operator *which* required field was missing.

A third case is in scope for the same machinery but ships as plain CLI rather than as a prompt: **`--item <id>` and interactive `--item`** for retrying a single item that the migration filter would otherwise skip (already published, already saved as draft, or skipped via the pre-flight markers below). Same domain layout, same testability story.

## Decision

Introduce a "pre-flight" pattern: a small set of pure-function modules in `domain/` that, before the browser opens, scan the loaded items for cases that need operator input, prompt the operator, and persist the answers back into `data/downloaded_items.json`. The orchestrator (`upload_vinted.py:main()`) calls these in sequence after items load and before `build_session`. Re-runs read the persisted answers and don't re-prompt.

**Module layout** (one module per pre-flight scope, mirrored 1:1 in tests):

- `domain/preflight_isbn.py` — `find_books_missing_isbn`, `prompt_for_isbns`, `apply_isbns`. Prompts persist as `attributes.isbn` (filled) or `skip_reason: "missing_isbn"` (operator pressed Enter to skip).
- `domain/preflight_leaf.py` — `find_ambiguous`, `prompt_for_overrides`, `apply_overrides`. Prompts persist as `vinted_nav_override` (filled with the picked nav path). Skipped items get no marker — the leaf decision is per-run by design (the next run prompts again so the operator can change their mind).
- `domain/item_select.py` — `select_by_id` (lookup by Wallapop id, returns `None` if not present), `prompt_selection` (numbered menu with injectable `input_fn`/`output_fn`). Used by the `--item` / `--item <id>` flags.

**I/O is dependency-injected.** Every prompt function takes `input_fn=input` and `output_fn=print` as keyword arguments so tests can drive them with stub callables — no `monkeypatch.setattr("builtins.input", ...)`, no captured `capsys.readouterr()` parsing. The orchestrator passes the real builtins; tests pass `lambda _: "1234567890"` and `output.append`.

**Persistence is centralised on `data/downloaded_items.json`.** Pre-flight markers live alongside the rest of the item data in the file the extractor already owns. Three markers exist today:

- `attributes.isbn` — gathered ISBN. Read by `fill_dynamic_attributes` in the same way as any other Wallapop attribute, so once `NewItemPage.scan_dynamic_fields` knows the ISBN testid (gated on the `new_item_books` fixture, see ADR-0004 Step 6), the value lands in the form automatically.
- `skip_reason` — operator opted out of the item. Filtered at the top of `main()` *before* `--item`, `--retry-drafts`, and the default flow. The skip is permanent until the operator removes the marker by hand.
- `vinted_nav_override` — operator-picked nav path that wins over the `category_mapping.json` entry for that one item. `get_nav(item)` consults the override before the category-level mapping.

`extract_wallapop.py` only fetches *new* listings on re-runs (it diffs against the existing `downloaded_items.json`), so the markers survive re-extraction. The trade-off is documented in `README.md` → "Data files".

**Mutual exclusion.** `--item` and `--retry-drafts` are rejected together at the CLI level — different scopes (drafts pre-exist on Vinted; `--item` targets a Wallapop entry). The pre-flights themselves are skipped when `--retry-drafts` is set: the items list there is matched against existing Vinted drafts later in the flow, and the draft data is opaque at the point pre-flights would run, so prompting now would over-prompt.

**Tests** live under `tests/domain/test_preflight_isbn.py`, `test_preflight_leaf.py`, `test_item_select.py` — pure-function suites that exercise every prompt cancel path, every persistence shape, and the argparse contract for `--item`. None launch Chromium; none touch the network.

## Consequences

- **Positive.** Required-field gaps surface *before* the operator commits to a 50-item upload run, not after. Operator decisions persist across runs — typing 30 ISBNs once is a one-time tax, not a per-run tax. The `skip_reason` marker is a single filter point that flows through every entry path (`--item`, `--retry-drafts`, default), so a skip decision is honoured everywhere without per-flag plumbing. The pattern is extensible: any future "Vinted needs X but Wallapop never asked" case lands as `domain/preflight_<x>.py` + a single call from `main()`. Pure-function modules with injected I/O make the test surface trivial — 19 tests for the ISBN module, 25 for `--item`, all running under 5 seconds with no browser.
- **Negative.** We mutate a file the extractor "owns". The contract that `extract_wallapop.py` only fetches new listings (preserving existing entries) is now load-bearing — if that ever changes, the pre-flight markers get clobbered. The operator-by-hand undo for `skip_reason` is friction for the (rare) case of "I changed my mind, let me retry that book" — a deliberate trade against the (frequent) case of "don't re-prompt me about this every run". Pre-flight prompts also rule out a fully unattended `python upload_vinted.py` run for catalogues with books or ambiguous leaves, but that was already true for DataDome captchas.
- **Open.** The leaf pre-flight today is single-level — when the operator picks a sub-option that itself has children, the browser-side picker takes over and the deeper resolution falls back to the existing draft path. A recursive prompt is left for when the gap actually bites. ISBN format validation (10/13 digits, dash handling) is also deferred until a malformed ISBN gets persisted in production. Detection of *semantic* leaf mismatch (e.g. Wallapop seller listed a DVD under "CDs Música" — Vinted resolves the CD leaf correctly because CD *is* a leaf, but the wrong one for that item) requires title-vs-leaf lexical analysis and is out of scope for v1.
