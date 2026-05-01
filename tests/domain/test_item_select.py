"""Tests for ``domain.item_select`` (single-item scoping helpers)."""

from __future__ import annotations

from domain.item_select import prompt_selection, select_by_id


# ---------------------------------------------------------------------------
# select_by_id — pure lookup
# ---------------------------------------------------------------------------


class TestSelectById:
    def test_returns_match(self):
        items = [{"id": "abc", "title": "A"}, {"id": "def", "title": "B"}]
        assert select_by_id(items, "def") == {"id": "def", "title": "B"}

    def test_returns_none_when_missing(self):
        assert select_by_id([{"id": "abc"}], "xyz") is None

    def test_handles_numeric_ids(self):
        # Wallapop ids are alphanumeric strings, but if a caller passes a
        # number it should still match against a stringified id in the dict.
        items = [{"id": 12345, "title": "X"}]
        assert select_by_id(items, "12345") == {"id": 12345, "title": "X"}

    def test_handles_empty_list(self):
        assert select_by_id([], "abc") is None

    def test_returns_first_on_duplicate_ids(self):
        # Defensive: downloaded_items.json is keyed by id so duplicates are
        # impossible in practice, but make the contract explicit.
        items = [{"id": "x", "title": "first"}, {"id": "x", "title": "second"}]
        assert select_by_id(items, "x") == {"id": "x", "title": "first"}


# ---------------------------------------------------------------------------
# prompt_selection — IO-injected interactive picker
# ---------------------------------------------------------------------------


class _IO:
    """Stand-in for ``input`` / ``print``.

    Replays a queue of canned responses to ``input_fn`` and records
    everything written to ``output_fn`` (plus the prompts ``input_fn``
    receives) so tests can assert on the rendered menu.
    """

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


class TestPromptSelection:
    def test_returns_picked_item(self):
        items = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]
        io = _IO(["2"])
        picked = prompt_selection(items, input_fn=io.input_fn, output_fn=io.output_fn)
        assert picked == {"id": "b", "title": "B"}

    def test_returns_none_on_quit_keyword(self):
        io = _IO(["q"])
        assert prompt_selection([{"id": "a"}], input_fn=io.input_fn, output_fn=io.output_fn) is None

    def test_returns_none_on_quit_word(self):
        io = _IO(["quit"])
        assert prompt_selection([{"id": "a"}], input_fn=io.input_fn, output_fn=io.output_fn) is None

    def test_returns_none_on_eof(self):
        io = _IO([])  # input_fn raises EOFError on first call
        assert prompt_selection([{"id": "a"}], input_fn=io.input_fn, output_fn=io.output_fn) is None

    def test_returns_none_on_empty_input(self):
        io = _IO([""])
        assert prompt_selection([{"id": "a"}], input_fn=io.input_fn, output_fn=io.output_fn) is None

    def test_reprompts_on_non_integer(self):
        items = [{"id": "a"}, {"id": "b"}]
        io = _IO(["abc", "1"])
        assert prompt_selection(items, input_fn=io.input_fn, output_fn=io.output_fn) == items[0]

    def test_reprompts_on_out_of_range(self):
        items = [{"id": "a"}, {"id": "b"}]
        io = _IO(["99", "0", "2"])
        assert prompt_selection(items, input_fn=io.input_fn, output_fn=io.output_fn) == items[1]

    def test_handles_empty_items_list(self):
        io = _IO([])  # would raise EOFError, but we never call input_fn
        assert prompt_selection([], input_fn=io.input_fn, output_fn=io.output_fn) is None
        # And the user still gets a hint.
        assert any("No items" in line for line in io.outputs)

    def test_renders_id_title_and_category(self):
        items = [{"id": "abc123", "title": "Vintage hat", "category_id": "10234"}]
        io = _IO(["1"])
        prompt_selection(items, input_fn=io.input_fn, output_fn=io.output_fn)
        rendered = "\n".join(io.outputs)
        assert "abc123" in rendered
        assert "Vintage hat" in rendered
        assert "10234" in rendered

    def test_handles_missing_title_and_category(self):
        items = [{"id": "abc"}]
        io = _IO(["1"])
        picked = prompt_selection(items, input_fn=io.input_fn, output_fn=io.output_fn)
        assert picked == {"id": "abc"}
        rendered = "\n".join(io.outputs)
        assert "(no title)" in rendered
