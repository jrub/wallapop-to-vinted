"""Static checks on ``upload_vinted.py`` navigation calls.

Vinted's telemetry keeps XHRs alive long past initial paint, so a
``page.goto(...)`` that defaults to ``wait_until="load"`` reliably times
out at 30s. Every navigation in the script must specify
``wait_until="domcontentloaded"`` instead.

This is a static (AST) check on the source rather than a behavioural
test because the failure mode is a parameter omission — easy to catch
without launching a browser, and the cost of running a real navigation
in CI would be enormous.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_VINTED = REPO_ROOT / "upload_vinted.py"


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


def test_every_page_goto_specifies_wait_until():
    """Every ``page.goto(...)`` in upload_vinted.py must pass ``wait_until=``.

    Default ``wait_until="load"`` times out at 30s on Vinted because
    telemetry XHRs keep the load event pending. Always pass
    ``wait_until="domcontentloaded"``.
    """
    tree = ast.parse(UPLOAD_VINTED.read_text(encoding="utf-8"))
    offenders: list[tuple[int, str]] = []
    for call in _goto_calls(tree):
        kwargs = {kw.arg for kw in call.keywords}
        if "wait_until" not in kwargs:
            # Render the offending call site for the failure message
            try:
                snippet = ast.unparse(call)
            except Exception:
                snippet = "<unrenderable>"
            offenders.append((call.lineno, snippet))

    assert not offenders, (
        "page.goto(...) calls without wait_until=:\n"
        + "\n".join(f"  upload_vinted.py:{lineno}: {snippet}" for lineno, snippet in offenders)
        + "\n\nUse wait_until='domcontentloaded' — see the docstring on this test."
    )
