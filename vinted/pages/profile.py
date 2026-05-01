"""Page Object for the user's own Vinted profile (/member/<id>).

Two responsibilities:

- :meth:`ProfilePage.get_member_url` derives the logged-in user's
  ``/member/<id>`` URL from any source the runtime exposes (JWT cookie,
  current URL, an in-page ``/api/v2/users/current`` fetch, localStorage,
  finally ``/settings/profile``). The four fallbacks exist because each
  source can be missing or DataDome-blocked depending on session state.
- :meth:`ProfilePage.scrape_drafts` walks the owner's profile, scrolls
  to hydrate lazy cards, and returns the list of drafts as
  ``{"item_id", "edit_url", "title"}`` dicts. Drafts are identified by
  the presence of an ``a[href$="/edit"]`` link inside the card; published
  items don't render those.

Both methods print operator-facing diagnostics (``page.goto`` URL,
counts of selectors found, etc.) — keeping the print-line behaviour
identical to the original ``get_member_url`` / ``scrape_drafts`` so the
post-extraction logs match the pre-extraction logs.
"""

from __future__ import annotations

import re

from vinted.pages._common import human_delay
from vinted.session import user_id_from_jwt_cookie


class ProfilePage:
    def __init__(self, page, base_url: str = "https://www.vinted.es"):
        self.page = page
        self.base_url = base_url

    def get_member_url(self) -> str:
        """Return the user's own ``/member/<id>`` URL.

        Vinted's ``/api/v2/users/current`` is DataDome-blocked for
        scripted clients, the React app doesn't expose the id in
        localStorage or header links, and ``/settings/profile`` doesn't
        link to the member profile either. The only reliable source is
        the session JWT cookie (``access_token_web``), whose ``sub``
        claim is the numeric user id. We use that first, falling back
        to the URL path, an in-page fetch, localStorage, and finally
        a navigation to ``/settings/profile``.

        Raises ``RuntimeError`` when no source yields an id (typically
        means the session is not authenticated).
        """
        page = self.page
        base_url = self.base_url

        uid = user_id_from_jwt_cookie(page)
        if uid:
            return f"{base_url}/member/{uid}"
        m = re.search(r"/member/(\d+)", page.url)
        if m:
            return f"{base_url}/member/{m.group(1)}"
        # page.request.get() bypasses DataDome's XHR fingerprinting and gets 403'd; we need
        # to call /api/v2/users/current from the page context so the real browser fetch is used.
        try:
            page.goto(base_url, wait_until="domcontentloaded")
            human_delay(1.5, 2.5)
        except Exception as e:
            print(f"    WARNING: could not navigate to home: {e}")

        try:
            data = page.evaluate(
                """async () => {
                  try {
                    const r = await fetch('/api/v2/users/current', { credentials: 'include' });
                    const status = r.status;
                    if (!r.ok) return { status, body: (await r.text()).slice(0, 200) };
                    const j = await r.json();
                    return { status, data: j };
                  } catch (e) { return { status: 0, body: String(e) }; }
                }"""
            ) or {}
            print(f"    API users/current (in-page) → HTTP {data.get('status')}")
            if data.get("status") == 200 and data.get("data"):
                user = (data["data"].get("user") or {})
                uid = user.get("id") or data["data"].get("id")
                if uid:
                    return f"{base_url}/member/{uid}"
                print(f"    API OK but no id — keys: {list(data['data'].keys())[:10]}")
            elif data.get("body"):
                print(f"    body: {data['body'][:200]}")
        except Exception as e:
            print(f"    WARNING: in-page fetch failed: {e}")

        # localStorage is the most reliable hint: Vinted's React app caches the signed-in
        # user payload there. The key name varies by deploy, so we walk every entry looking
        # for a JSON blob with a numeric id + a login/username field (to avoid matching
        # unrelated ids like ad-partner user segments).
        try:
            uid = page.evaluate(
                r"""() => {
                  const keys = Object.keys(localStorage);
                  for (const k of keys) {
                    const raw = localStorage.getItem(k);
                    if (!raw || raw.length > 200000) continue;
                    let parsed;
                    try { parsed = JSON.parse(raw); } catch { continue; }
                    const stack = [parsed];
                    while (stack.length) {
                      const node = stack.pop();
                      if (!node || typeof node !== 'object') continue;
                      if (typeof node.id === 'number' &&
                          (typeof node.login === 'string' || typeof node.anon_id === 'string' ||
                           typeof node.email === 'string' || typeof node.is_logged_in === 'boolean')) {
                        if (node.id > 1000) return node.id;  // real Vinted user ids are large
                      }
                      for (const v of Object.values(node)) {
                        if (v && typeof v === 'object') stack.push(v);
                      }
                    }
                  }
                  return 0;
                }"""
            )
            if uid:
                print(f"    Profile (localStorage): {uid}")
                return f"{base_url}/member/{uid}"
            print(f"    localStorage has no recognisable user.")
        except Exception as e:
            print(f"    WARNING: error reading localStorage: {e}")

        # Last resort: navigate to a user-scoped settings page and read /member/<id> links.
        try:
            page.goto(f"{base_url}/settings/profile", wait_until="domcontentloaded")
            human_delay(1.5, 2.5)
            href = page.evaluate(
                r"""() => {
                  const links = Array.from(document.querySelectorAll('a[href*="/member/"]'));
                  for (const a of links) {
                    const h = a.getAttribute('href') || '';
                    if (/^\/member\/\d+(\/|$)/.test(h)) return h;
                  }
                  return '';
                }"""
            ) or ""
            m = re.search(r"/member/(\d+)", href)
            if m:
                return f"{base_url}/member/{m.group(1)}"
            print(f"    /settings/profile also does not expose /member/<id>.")
        except Exception as e:
            print(f"    WARNING: error on /settings/profile: {e}")

        raise RuntimeError("Could not derive the profile URL — session expired?")

    def scrape_drafts(self, member_url: str) -> list[dict]:
        """Return the list of drafts on the owner's profile.

        Each entry: ``{"item_id": str, "edit_url": str, "title": str}``.
        Drafts are identified by an ``a[href$="/edit"]`` link inside the
        card; published items render plain ``/items/<id>`` links without
        ``/edit``.
        """
        page = self.page
        base_url = self.base_url

        # ``networkidle`` hangs on profile pages (persistent image/analytics traffic);
        # ``domcontentloaded`` + scrolling is enough for the card list to hydrate.
        page.goto(member_url, wait_until="domcontentloaded")
        human_delay(2, 3)
        # Scroll to the bottom to make sure lazy-loaded items render
        try:
            for _ in range(6):
                page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                human_delay(0.4, 0.7)
        except Exception:
            pass

        # Diagnostic snapshot to pinpoint why draft scraping finds 0
        try:
            diag = page.evaluate(
                r"""() => ({
                  url: location.href,
                  status_text_count: document.querySelectorAll('[data-testid$="--status-text"]').length,
                  status_text_samples: Array.from(document.querySelectorAll('[data-testid$="--status-text"]'))
                    .slice(0, 5).map(el => (el.innerText || '').trim()),
                  edit_link_count: document.querySelectorAll('a[href$="/edit"]').length,
                  edit_link_samples: Array.from(document.querySelectorAll('a[href$="/edit"]'))
                    .slice(0, 5).map(a => a.getAttribute('href')),
                  product_card_count: document.querySelectorAll('[data-testid^="product-item-id-"]').length,
                  any_item_link_count: document.querySelectorAll('a[href*="/items/"]').length,
                  tabs: Array.from(document.querySelectorAll('[role="tab"], nav a'))
                    .slice(0, 20).map(t => ({ text: (t.innerText || '').trim().slice(0,40), href: t.getAttribute('href') || '' }))
                })"""
            ) or {}
            print(f"    Profile diag: url={diag.get('url')}")
            print(f"      status-text={diag.get('status_text_count')} samples={diag.get('status_text_samples')}")
            print(f"      edit-links={diag.get('edit_link_count')} samples={diag.get('edit_link_samples')}")
            print(f"      product-cards={diag.get('product_card_count')}  item-links={diag.get('any_item_link_count')}")
            print(f"      tabs={diag.get('tabs')}")
        except Exception as e:
            print(f"    WARNING: diagnostic failed: {e}")

        # On the owner's profile, every draft renders a pair of ``<a href="/items/<id>/edit">``
        # links (image and title both link to the edit page). Published items show a regular
        # ``/items/<id>`` link without ``/edit``, so edit-links cleanly identify drafts. We walk
        # up from each edit-link to find a card container and pull the title from its img alt.
        raw = page.evaluate(
            r"""() => {
              const drafts = new Map();
              document.querySelectorAll('a[href$="/edit"]').forEach(a => {
                const href = a.getAttribute('href') || '';
                const m = href.match(/\/items\/(\d+)\/edit/);
                if (!m) return;
                const id = m[1];
                if (drafts.has(id)) return;
                // Find the enclosing card (walk up until we hit a product-item testid or stop)
                let card = a.parentElement;
                for (let i = 0; i < 12 && card; i++) {
                  if (card.matches('[data-testid^="product-item-id-"]')) break;
                  card = card.parentElement;
                }
                const scope = card || a.parentElement || a;
                // Title from image alt (clean) falling back to nearest non-edit item link
                let title = '';
                const img = scope.querySelector && scope.querySelector('img[alt]');
                if (img) title = (img.getAttribute('alt') || '').trim();
                if (!title && scope.querySelector) {
                  const tlink = scope.querySelector('a[href^="/items/"]:not([href$="/edit"])');
                  if (tlink) title = (tlink.getAttribute('title') || tlink.innerText || '').trim();
                }
                drafts.set(id, { item_id: id, edit_url: href, title });
              });
              return Array.from(drafts.values());
            }"""
        ) or []
        drafts = []
        for d in raw:
            d["edit_url"] = base_url + d["edit_url"] if d["edit_url"].startswith("/") else d["edit_url"]
            drafts.append(d)
        return drafts
