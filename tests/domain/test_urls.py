"""Tests for domain.urls — pure URL parsing helpers."""

from domain.urls import extract_item_id_from_url, is_form_url


class TestExtractItemIdFromUrl:
    def test_extracts_from_edit_url(self):
        assert extract_item_id_from_url("https://www.vinted.es/items/12345/edit") == "12345"

    def test_extracts_from_view_url(self):
        assert extract_item_id_from_url("https://www.vinted.es/items/98765") == "98765"

    def test_extracts_with_query_string(self):
        assert extract_item_id_from_url("https://www.vinted.es/items/55/?ref=share") == "55"

    def test_returns_empty_when_no_id(self):
        assert extract_item_id_from_url("https://www.vinted.es/items/new") == ""

    def test_returns_empty_for_non_item_url(self):
        assert extract_item_id_from_url("https://www.vinted.es/member/42") == ""

    def test_returns_empty_for_garbage(self):
        assert extract_item_id_from_url("not a url") == ""

    def test_returns_empty_for_empty_string(self):
        assert extract_item_id_from_url("") == ""


class TestIsFormUrl:
    def test_new_item_url(self):
        assert is_form_url("https://www.vinted.es/items/new") is True

    def test_edit_url(self):
        assert is_form_url("https://www.vinted.es/items/12345/edit") is True

    def test_item_view_url_is_not_form(self):
        # /items/12345 (no /edit suffix) is the post-publish view, not a form.
        assert is_form_url("https://www.vinted.es/items/12345") is False

    def test_member_page_is_not_form(self):
        assert is_form_url("https://www.vinted.es/member/42") is False

    def test_empty_string(self):
        assert is_form_url("") is False
