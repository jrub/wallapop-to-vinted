# 3. Extract the Vinted session layer + introduce error taxonomy

Date: 2026-04-25
Status: Proposed

## Context

After ADR-0001 (domain layer) and ADR-0002 (Wallapop side), `upload_vinted.py` still mixes the Patchright bootstrap, the DataDome captcha detector, the JWT-cookie user-id helper, the login flow, the page object DOM scanners, and the publish/draft loop in a single ≈1500-line module. The only feedback loop for any of these is a real Vinted upload, which is slow and captcha-prone. This is Phase 3 of the refactor described in ADR-0001: the smallest layer that can be moved without dragging in the page objects (Phase 4).

There is also no error taxonomy. The captcha detector calls `sys.exit(2)` directly, the login function raises a generic `RuntimeError`, and the publish loop signals draft fallback through return values. That makes it impossible for a future page object to react to a specific failure mode without grepping the log line.

## Decision

Create two new modules:

- `vinted/session.py` — Patchright launch + context (`build_session`), DataDome captcha detector (`is_captcha_present`, `abort_if_captcha`), and the JWT-cookie user-id helper (`user_id_from_jwt_cookie`). Public API takes the `playwright` instance, the `auth_state.json` path, and the `visible` flag by argument — no module-level state, no `os.getenv`.
- `vinted/errors.py` — `VintedError` root + `CaptchaDetected`, `LoginFailed`, `PublishRejected`. The session raises `CaptchaDetected` instead of calling `sys.exit(2)`; `main()` in `upload_vinted.py` catches it and translates to the same exit code with the same operator-facing message. `LoginFailed` and `PublishRejected` are introduced now so the taxonomy is complete; their final call sites land in Phase 4 with the page objects.

Tests live under `tests/vinted/test_session.py` and `tests/vinted/test_errors.py`, mirroring the `tests/wallapop/` layout. The captcha detector and JWT helper are tested with stub `page` objects (no Patchright Chromium); the bootstrap (`build_session`) is verified by `make smoke` and a real `--limit 1 --visible` run, since launching a headed Chromium under pytest would couple the unit suite to a binary install and add seconds to every CI run for no extra coverage.

The captcha detector also drops the dead `"captcha-delivery.com" in page.url` branch — that scenario has never fired in production (DataDome is always served as an iframe over the Vinted page, not as a top-level redirect), so the only check kept is `iframe[src*='captcha-delivery.com']`.

`login()` and the publish/draft loop stay in `upload_vinted.py` for now; they belong to the page object layer (Phase 4) and moving them earlier would mix two phases.

## Consequences

- **Positive.** The captcha detector, `abort_if_captcha`, and the JWT helper become testable without a browser, locking in the surprisingly small contract each one has. The exception classes give Phase 4 a fixed taxonomy to raise from page objects — no need to revisit the API later. `build_session` makes the launch-args/init-script combo a single function, which is the part most likely to need surgery if Vinted/DataDome tightens fingerprinting. Captcha detection is no longer dead-coded against a redirect path that never happens.
- **Negative.** `main()` gains a small `try/except CaptchaDetected: print + sys.exit(2)` block — the same logic that lived in `_abort_if_captcha`, now at the orchestrator boundary. The cost is one indirection vs. the readability win of testable exceptions.
- **Open.** Whether `get_member_url` (and the JWT scrape it wraps) belongs in `vinted/session.py` or in a `vinted/profile.py` to be created in Phase 4 is left to Phase 4 — not load-bearing now since the session module already exposes `user_id_from_jwt_cookie`.
