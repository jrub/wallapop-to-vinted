"""Tests for domain.drafts — match Vinted drafts back to Wallapop items."""

from domain.drafts import match_draft_to_item, normalize_title


class TestNormalizeTitle:
    def test_lowercases_and_strips_accents(self):
        assert normalize_title("Cámara CANON EOS") == "camara canon eos"

    def test_collapses_punctuation_to_space(self):
        # Non-alphanumeric runs collapse to a single space; runs of spaces are
        # then collapsed too.
        assert normalize_title("hp / officejet--7110") == "hp officejet 7110"

    def test_strips_leading_trailing_whitespace(self):
        assert normalize_title("  foo bar  ") == "foo bar"

    def test_empty_string(self):
        assert normalize_title("") == ""

    def test_none_safe(self):
        assert normalize_title(None) == ""


class TestMatchDraftToItem:
    def test_exact_match(self):
        items = {
            "abc": {"title": "Cámara Canon EOS"},
            "def": {"title": "Libro Python"},
        }
        item_id, ambiguous = match_draft_to_item("Cámara Canon EOS", items)
        assert item_id == "abc"
        assert ambiguous is False

    def test_exact_match_normalized(self):
        # Different accent / case → should still match.
        items = {"abc": {"title": "CAMARA canon eos"}}
        item_id, ambiguous = match_draft_to_item("Cámara Canon EOS", items)
        assert item_id == "abc"
        assert ambiguous is False

    def test_substring_fallback_draft_in_item(self):
        items = {"abc": {"title": "Libro de Python avanzado y patrones"}}
        item_id, ambiguous = match_draft_to_item("Libro de Python", items)
        assert item_id == "abc"
        assert ambiguous is False

    def test_substring_fallback_item_in_draft(self):
        items = {"abc": {"title": "Cámara"}}
        item_id, ambiguous = match_draft_to_item("Cámara Canon usada", items)
        assert item_id == "abc"
        assert ambiguous is False

    def test_no_match(self):
        items = {"abc": {"title": "Libro Python"}}
        item_id, ambiguous = match_draft_to_item("Nintendo Switch", items)
        assert item_id is None
        assert ambiguous is False

    def test_ambiguous_exact(self):
        items = {
            "abc": {"title": "Cámara Canon"},
            "def": {"title": "cámara canon"},  # normalises to the same
        }
        item_id, ambiguous = match_draft_to_item("Cámara Canon", items)
        assert item_id is None
        assert ambiguous is True

    def test_ambiguous_substring(self):
        items = {
            "abc": {"title": "Libro Python avanzado"},
            "def": {"title": "Libro Python básico"},
        }
        item_id, ambiguous = match_draft_to_item("Libro Python", items)
        assert item_id is None
        assert ambiguous is True

    def test_empty_draft_title(self):
        items = {"abc": {"title": "Foo"}}
        item_id, ambiguous = match_draft_to_item("", items)
        assert item_id is None
        assert ambiguous is False

    def test_empty_items(self):
        item_id, ambiguous = match_draft_to_item("Cámara", {})
        assert item_id is None
        assert ambiguous is False

    def test_items_without_title_skipped(self):
        # Items missing a title or with empty title should not contribute matches.
        items = {
            "abc": {"title": ""},
            "def": {},
            "ghi": {"title": "Cámara Canon"},
        }
        item_id, ambiguous = match_draft_to_item("Cámara Canon", items)
        assert item_id == "ghi"
        assert ambiguous is False
