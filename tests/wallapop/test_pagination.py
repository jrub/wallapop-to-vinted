"""Tests for Wallapop pagination + user-id resolution.

``fetch_items`` is the trickiest function in the module: cursor-based
paging, circular-pagination guard, ``stop_when_known`` early-stop, and the
``max_pages`` safety cap. Each branch is covered with a focused mock.

``parse_user_id_from_html`` is factored out as a pure helper so the
"missing ``__NEXT_DATA__``" case doesn't need ``responses``.
"""

import pytest
import requests
import responses

from wallapop.items import (
    fetch_items,
    parse_user_id_from_html,
    resolve_internal_id,
)


@pytest.fixture
def session():
    return requests.Session()


@pytest.fixture(autouse=True)
def no_sleep(mocker):
    """Strip the inter-page rate-limit sleep so multi-page tests stay fast."""
    mocker.patch("time.sleep")


def _batch(ids):
    """Synthesize a list of items with the given ids."""
    return [{"id": id_, "title": f"item-{id_}"} for id_ in ids]


# ---------- fetch_items ----------


class TestFetchItems:
    @responses.activate
    def test_two_pages_concatenated(self, session):
        page1_ids = [f"a{i}" for i in range(40)]
        page2_ids = [f"b{i}" for i in range(5)]
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/users/u1/items",
            json={"data": _batch(page1_ids), "meta": {"next": "CURSOR1"}},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/users/u1/items",
            json={"data": _batch(page2_ids), "meta": {}},
            status=200,
        )
        items = fetch_items("u1", session=session)
        assert [it["id"] for it in items] == page1_ids + page2_ids

    @responses.activate
    def test_stop_when_known_short_circuits(self, session):
        page1_ids = [f"x{i}" for i in range(40)]
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/users/u1/items",
            json={"data": _batch(page1_ids), "meta": {"next": "CURSOR1"}},
            status=200,
        )
        items = fetch_items(
            "u1", session=session, stop_when_known=set(page1_ids)
        )
        assert [it["id"] for it in items] == page1_ids
        # Only one HTTP call — the short-circuit kicked in before page 2
        assert len(responses.calls) == 1

    @responses.activate
    def test_circular_pagination_detected(self, session):
        page1_ids = [f"y{i}" for i in range(40)]
        # Page 2 returns ids already seen on page 1 → circular
        page2_ids = ["y0", "y1"] + [f"z{i}" for i in range(38)]
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/users/u1/items",
            json={"data": _batch(page1_ids), "meta": {"next": "CURSOR1"}},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/users/u1/items",
            json={"data": _batch(page2_ids), "meta": {"next": "CURSOR2"}},
            status=200,
        )
        items = fetch_items("u1", session=session)
        # Page 2 was rejected wholesale — only page 1 made it in
        assert [it["id"] for it in items] == page1_ids

    @responses.activate
    def test_max_pages_cap(self, session):
        # 21 pages registered — ``max_pages=20`` must stop at 20
        for page in range(21):
            page_ids = [f"p{page}_i{i}" for i in range(40)]
            responses.add(
                responses.GET,
                "https://api.wallapop.com/api/v3/users/u1/items",
                json={
                    "data": _batch(page_ids),
                    "meta": {"next": f"CURSOR_{page}"},
                },
                status=200,
            )
        items = fetch_items("u1", session=session, max_pages=20)
        assert len(items) == 800  # 20 pages × 40 items

    @responses.activate
    def test_empty_cursor_stops_cleanly(self, session):
        page1_ids = [f"a{i}" for i in range(40)]
        page2_ids = [f"b{i}" for i in range(40)]
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/users/u1/items",
            json={"data": _batch(page1_ids), "meta": {"next": "CURSOR1"}},
            status=200,
        )
        # Empty cursor on page 2 → loop must stop, not request a 3rd page
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/users/u1/items",
            json={"data": _batch(page2_ids), "meta": {"next": ""}},
            status=200,
        )
        items = fetch_items("u1", session=session)
        assert len(items) == 80
        assert len(responses.calls) == 2

    @responses.activate
    def test_max_items_caps_result(self, session):
        page_ids = [f"a{i}" for i in range(40)]
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/users/u1/items",
            json={"data": _batch(page_ids), "meta": {"next": "CURSOR1"}},
            status=200,
        )
        items = fetch_items("u1", session=session, max_items=5)
        assert [it["id"] for it in items] == page_ids[:5]

    @responses.activate
    def test_empty_first_batch_stops(self, session):
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/users/u1/items",
            json={"data": [], "meta": {}},
            status=200,
        )
        assert fetch_items("u1", session=session) == []

    @responses.activate
    def test_non_200_returns_partial_results(self, session):
        page1_ids = [f"a{i}" for i in range(40)]
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/users/u1/items",
            json={"data": _batch(page1_ids), "meta": {"next": "CURSOR1"}},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/users/u1/items",
            status=500,
        )
        items = fetch_items("u1", session=session)
        # Page 1 was kept; the 500 stopped the loop without raising
        assert [it["id"] for it in items] == page1_ids


# ---------- parse_user_id_from_html (pure) ----------


class TestParseUserIdFromHtml:
    def test_extracts_user_id(self):
        html = (
            "<html>...<script id=\"__NEXT_DATA__\" type=\"application/json\">"
            '{"props":{"pageProps":{"user":{"id":"123456"}}}}'
            "</script>...</html>"
        )
        assert parse_user_id_from_html(html) == "123456"

    def test_missing_script_raises(self):
        with pytest.raises(ValueError, match="__NEXT_DATA__"):
            parse_user_id_from_html("<html>no script</html>")

    def test_handles_extra_script_attributes(self):
        # Real Wallapop adds attributes (type, nonce, etc.) to the tag
        html = (
            '<script id="__NEXT_DATA__" type="application/json" nonce="x">'
            '{"props":{"pageProps":{"user":{"id":"abc"}}}}'
            "</script>"
        )
        assert parse_user_id_from_html(html) == "abc"

    def test_handles_multiline_body(self):
        html = (
            '<script id="__NEXT_DATA__">\n'
            '{"props":\n{"pageProps":\n{"user":{"id":"xyz"}}}}\n'
            "</script>"
        )
        assert parse_user_id_from_html(html) == "xyz"


# ---------- resolve_internal_id ----------


class TestResolveInternalId:
    @responses.activate
    def test_returns_user_id_from_profile(self, session):
        responses.add(
            responses.GET,
            "https://es.wallapop.com/user/javier-x",
            body=(
                "<html>"
                '<script id="__NEXT_DATA__" type="application/json">'
                '{"props":{"pageProps":{"user":{"id":"55"}}}}'
                "</script>"
                "</html>"
            ),
            status=200,
        )
        assert resolve_internal_id("javier-x", session=session) == "55"

    @responses.activate
    def test_raises_on_http_error(self, session):
        responses.add(
            responses.GET,
            "https://es.wallapop.com/user/notfound",
            status=404,
        )
        with pytest.raises(requests.HTTPError):
            resolve_internal_id("notfound", session=session)
