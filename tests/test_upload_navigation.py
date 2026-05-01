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
]


def _goto_calls(tree: ast.AST) -> list[ast.Call]:
    """Return every ``<something>.goto(...)`` call in ``tree``."""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "goto"
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
