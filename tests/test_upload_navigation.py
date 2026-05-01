"""Static checks on Vinted-side navigation calls.

Vinted's telemetry keeps XHRs alive long past initial paint, so a
``page.goto(...)`` that defaults to ``wait_until="load"`` reliably times
out at 30s. Every navigation in the upload flow must specify
``wait_until="domcontentloaded"`` instead.

This is a static (AST) check on the sources rather than a behavioural
test because the failure mode is a parameter omission — easy to catch
without launching a browser, and the cost of running a real navigation
in CI would be enormous.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every Python file that drives Vinted navigation. New page objects that
# call ``page.goto(...)`` must be added here.
NAVIGATION_SOURCES = [
    REPO_ROOT / "upload_vinted.py",
    REPO_ROOT / "vinted" / "pages" / "login.py",
    REPO_ROOT / "vinted" / "pages" / "profile.py",
    REPO_ROOT / "vinted" / "pages" / "edit_draft.py",
]


def _is_playwright_page_target(target: ast.AST) -> bool:
    """Return True when ``target`` is a Playwright ``Page`` reference.

    By project convention, the Playwright page is bound either to the
    local name ``page`` or to ``self.page`` (inside page-object methods).
    Domain-level page objects (``EditDraftPage``, ``ProfilePage``, …)
    expose their own ``goto(url)`` methods that internally wrap
    ``self.page.goto(..., wait_until=...)`` — those calls must NOT be
    flagged, since the wait-until is encoded one level deeper.
    """
    if isinstance(target, ast.Name) and target.id == "page":
        return True
    if (
        isinstance(target, ast.Attribute)
        and target.attr == "page"
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    ):
        return True
    return False


def _goto_calls(tree: ast.AST) -> list[ast.Call]:
    """Return every ``page.goto(...)`` / ``self.page.goto(...)`` call in ``tree``.

    Calls to wrapper methods like ``edit_page.goto(...)`` are intentionally
    skipped — those are domain page-object methods, not the raw Playwright
    ``Page.goto`` that needs ``wait_until=``.
    """
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "goto"
            and _is_playwright_page_target(node.func.value)
        ):
            out.append(node)
    return out


@pytest.mark.parametrize("source_path", NAVIGATION_SOURCES, ids=lambda p: p.name)
def test_every_page_goto_specifies_wait_until(source_path: Path):
    """Every ``page.goto(...)`` must pass ``wait_until=``.

    Default ``wait_until="load"`` times out at 30s on Vinted because
    telemetry XHRs keep the load event pending. Always pass
    ``wait_until="domcontentloaded"``.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    offenders: list[tuple[int, str]] = []
    for call in _goto_calls(tree):
        kwargs = {kw.arg for kw in call.keywords}
        if "wait_until" not in kwargs:
            try:
                snippet = ast.unparse(call)
            except Exception:
                snippet = "<unrenderable>"
            offenders.append((call.lineno, snippet))

    rel = source_path.relative_to(REPO_ROOT)
    assert not offenders, (
        f"page.goto(...) calls without wait_until= in {rel}:\n"
        + "\n".join(f"  {rel}:{lineno}: {snippet}" for lineno, snippet in offenders)
        + "\n\nUse wait_until='domcontentloaded' — see the docstring on this test."
    )
