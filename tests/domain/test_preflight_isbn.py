"""Tests for ``domain.preflight_isbn``."""

from __future__ import annotations

from domain.preflight_isbn import (
    SKIP_REASON_KEY,
    SKIP_REASON_NO_ISBN,
    apply_isbns,
    find_books_missing_isbn,
    prompt_for_isbns,
)


def _book_nav():
    return ["Entretenimiento", "Libros", "No ficción"]


def _non_book_nav():
    return ["Entretenimiento", "Música", "CD"]


# ---------------------------------------------------------------------------
# find_books_missing_isbn — pure detection
# ---------------------------------------------------------------------------


class TestFindBooksMissingIsbn:
    def test_finds_book_without_isbn(self):
        item = {"id": "a", "category_id": "X"}
        assert find_books_missing_isbn([item], lambda i: _book_nav()) == [item]

    def test_skips_non_book(self):
        item = {"id": "a", "category_id": "X"}
        assert find_books_missing_isbn([item], lambda i: _non_book_nav()) == []

    def test_skips_book_with_isbn(self):
        item = {"id": "a", "attributes": {"isbn": "9781234567890"}}
        assert find_books_missing_isbn([item], lambda i: _book_nav()) == []

    def test_treats_blank_isbn_as_missing(self):
        # Whitespace-only isbn shouldn't pass the gate.
        item = {"id": "a", "attributes": {"isbn": "   "}}
        assert find_books_missing_isbn([item], lambda i: _book_nav()) == [item]

    def test_treats_none_isbn_as_missing(self):
        item = {"id": "a", "attributes": {"isbn": None}}
        assert find_books_missing_isbn([item], lambda i: _book_nav()) == [item]

    def test_skips_already_skipped_items(self):
        item = {"id": "a", SKIP_REASON_KEY: "missing_isbn"}
        assert find_books_missing_isbn([item], lambda i: _book_nav()) == []

    def test_handles_unmapped_item(self):
        item = {"id": "a"}
        assert find_books_missing_isbn([item], lambda i: None) == []

    def test_filters_mixed_list(self):
        items = [
            {"id": "book", "category_id": "B"},
            {"id": "cd", "category_id": "C"},
            {"id": "filled", "attributes": {"isbn": "111"}},
        ]
        nav_lookup = {"book": _book_nav(), "cd": _non_book_nav(), "filled": _book_nav()}
        result = find_books_missing_isbn(items, lambda i: nav_lookup[i["id"]])
        assert [r["id"] for r in result] == ["book"]


# ---------------------------------------------------------------------------
# prompt_for_isbns — IO-injected
# ---------------------------------------------------------------------------


class _IO:
    def __init__(self, inputs):
        self._inputs = list(inputs)
        self.outputs: list[str] = []

    def input_fn(self, prompt: str) -> str:
        self.outputs.append(prompt)
        if not self._inputs:
            raise EOFError
        return self._inputs.pop(0)

    def output_fn(self, line: str) -> None:
        self.outputs.append(line)


class TestPromptForIsbns:
    def test_records_isbn_per_item(self):
        items = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]
        io = _IO(["111", "222"])
        out = prompt_for_isbns(items, input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {"a": "111", "b": "222"}

    def test_empty_input_means_skip(self):
        items = [{"id": "a", "title": "A"}]
        io = _IO([""])
        out = prompt_for_isbns(items, input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {"a": None}

    def test_eof_means_skip_subsequent_items(self):
        items = [{"id": "a"}, {"id": "b"}]
        io = _IO([])  # raises EOFError on every call
        out = prompt_for_isbns(items, input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {"a": None, "b": None}

    def test_strips_whitespace(self):
        items = [{"id": "a"}]
        io = _IO(["  978-1234567890  "])
        out = prompt_for_isbns(items, input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {"a": "978-1234567890"}

    def test_no_items_no_prompts(self):
        io = _IO([])  # would raise if called
        out = prompt_for_isbns([], input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {}
        assert io.outputs == []  # silent when there's nothing to do

    def test_renders_id_and_title(self):
        items = [{"id": "abc123", "title": "Libro de Sushi"}]
        io = _IO(["111"])
        prompt_for_isbns(items, input_fn=io.input_fn, output_fn=io.output_fn)
        rendered = "\n".join(io.outputs)
        assert "abc123" in rendered
        assert "Libro de Sushi" in rendered


# ---------------------------------------------------------------------------
# apply_isbns — mutation + skip-list
# ---------------------------------------------------------------------------


class TestApplyIsbns:
    def test_writes_isbn_to_attributes(self):
        items = [{"id": "a"}]
        skipped = apply_isbns(items, {"a": "111"})
        assert items[0]["attributes"]["isbn"] == "111"
        assert skipped == []

    def test_marks_skipped_items_with_skip_reason(self):
        items = [{"id": "a"}]
        skipped = apply_isbns(items, {"a": None})
        assert items[0][SKIP_REASON_KEY] == SKIP_REASON_NO_ISBN
        assert skipped == ["a"]
        # No isbn written when the user skipped.
        assert "isbn" not in items[0].get("attributes", {})

    def test_preserves_other_attributes(self):
        items = [{"id": "a", "attributes": {"brand": "X"}}]
        apply_isbns(items, {"a": "111"})
        assert items[0]["attributes"] == {"brand": "X", "isbn": "111"}

    def test_ignores_unknown_ids_without_corrupting_items(self):
        items = [{"id": "a"}]
        apply_isbns(items, {"unknown": "999"})
        assert items[0] == {"id": "a"}  # untouched

    def test_handles_mixed_filled_and_skipped(self):
        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        skipped = apply_isbns(items, {"a": "111", "b": None, "c": "333"})
        assert items[0]["attributes"]["isbn"] == "111"
        assert items[1][SKIP_REASON_KEY] == SKIP_REASON_NO_ISBN
        assert items[2]["attributes"]["isbn"] == "333"
        assert skipped == ["b"]
