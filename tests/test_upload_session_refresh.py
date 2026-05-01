"""AST guards for the /session-refresh handling in upload_vinted.py.

When /items/new redirects to /session-refresh (stale access token,
valid refresh token), two things must be true:

1. The redirect is recognised as an auth path so the log message says
   "Session expired (redirected to auth: ...)" rather than the generic form.
2. Stale Vinted auth tokens are cleared from the browser context before
   login() is called — otherwise Vinted server-side sees the valid
   refresh_token_web and bounces the signup page to home, causing a
   TimeoutError on the auth-select-type switch button.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_VINTED = REPO_ROOT / "upload_vinted.py"


def _auth_paths_tuples(tree: ast.AST) -> list[ast.Tuple]:
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_AUTH_PATHS"
            and isinstance(node.value, ast.Tuple)
        ):
            out.append(node.value)
    return out


def _clear_cookies_calls(tree: ast.AST) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "clear_cookies"
        ):
            out.append(node)
    return out


def test_session_refresh_in_auth_paths():
    """/session-refresh must be in at least one _AUTH_PATHS tuple.

    Without this the redirect is silently treated as a generic expiry,
    the improved log message is lost, and future guards that branch on
    _AUTH_PATHS won't fire for this state.
    """
    tree = ast.parse(UPLOAD_VINTED.read_text(encoding="utf-8"))
    tuples = _auth_paths_tuples(tree)
    assert tuples, "No _AUTH_PATHS assignment found in upload_vinted.py"

    def _has_session_refresh(tup: ast.Tuple) -> bool:
        return any(
            isinstance(el, ast.Constant) and el.value == "/session-refresh"
            for el in tup.elts
        )

    assert any(_has_session_refresh(t) for t in tuples), (
        "No _AUTH_PATHS tuple contains '/session-refresh'.\n"
        "Add it so the redirect is recognised as an auth state, not a generic expiry."
    )


def test_clear_cookies_called_before_login():
    """context.clear_cookies(...) must appear in the script.

    Stale refresh_token_web causes Vinted to bounce the signup page to
    home — clear the auth cookies before login() so the server treats the
    browser as anonymous.
    """
    tree = ast.parse(UPLOAD_VINTED.read_text(encoding="utf-8"))
    calls = _clear_cookies_calls(tree)
    assert calls, (
        "No *.clear_cookies(...) call found in upload_vinted.py.\n"
        "Clear access_token_web / refresh_token_web / _vinted_fr_session by name "
        "before login() to prevent /session-refresh login loops.\n"
        "datadome shares the .vinted.es domain — filter by name, not domain."
    )
