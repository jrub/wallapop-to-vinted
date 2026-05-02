"""Tests for ``domain.preflight_leaf``."""

from __future__ import annotations

from domain.categories import build_path_index
from domain.preflight_leaf import (
    NAV_OVERRIDE_KEY,
    apply_overrides,
    find_ambiguous,
    prompt_for_overrides,
)


# ---------------------------------------------------------------------------
# Fixtures: a tiny Vinted-style category tree
# ---------------------------------------------------------------------------


def _tree():
    """Three sibling leaves under a non-leaf parent, plus an unrelated leaf.

    Parent: ['Dispositivos de red'] (non-leaf)
      ├─ Routers (leaf)
      ├─ Repetidores (leaf)
      └─ Módems (leaf)

    Sibling: ['CD'] (leaf, no children)
    """
    return [
        {"path": ["Dispositivos de red"], "title": "Dispositivos de red", "is_leaf": False},
        {"path": ["Dispositivos de red", "Routers"], "title": "Routers", "is_leaf": True},
        {"path": ["Dispositivos de red", "Repetidores"], "title": "Repetidores", "is_leaf": True},
        {"path": ["Dispositivos de red", "Módems"], "title": "Módems", "is_leaf": True},
        {"path": ["CD"], "title": "CD", "is_leaf": True},
    ]


# ---------------------------------------------------------------------------
# find_ambiguous — pure detection
# ---------------------------------------------------------------------------


class TestFindAmbiguous:
    def setup_method(self):
        self.nodes = _tree()
        self.path_index = build_path_index(self.nodes)

    def test_flags_intermediate_resolution(self):
        # Item with empty hints → resolver can't pick a child → returns
        # ['Dispositivos de red'] which is non-leaf in the fixture.
        item = {"id": "a", "title": "Aparato", "description": ""}
        result = find_ambiguous(
            [item], lambda i: ["Dispositivos de red"],
            nodes=self.nodes, path_index=self.path_index,
        )
        assert len(result) == 1
        it, resolved, candidates = result[0]
        assert it is item
        assert resolved == ["Dispositivos de red"]
        assert set(candidates) == {"Routers", "Repetidores", "Módems"}

    def test_skips_leaf_resolutions(self):
        # CD is already a leaf — no decision needed.
        item = {"id": "a", "title": "Disco", "description": ""}
        result = find_ambiguous(
            [item], lambda i: ["CD"], nodes=self.nodes, path_index=self.path_index,
        )
        assert result == []

    def test_skips_unmapped_items(self):
        item = {"id": "a"}
        result = find_ambiguous(
            [item], lambda i: None, nodes=self.nodes, path_index=self.path_index,
        )
        assert result == []

    def test_skips_items_with_existing_override(self):
        item = {"id": "a", NAV_OVERRIDE_KEY: ["Dispositivos de red", "Routers"]}
        result = find_ambiguous(
            [item], lambda i: ["Dispositivos de red"],
            nodes=self.nodes, path_index=self.path_index,
        )
        assert result == []

    def test_resolver_picks_leaf_via_hints(self):
        # If the description gives a stem hint, the resolver disambiguates
        # locally and we don't bother the user.
        item = {"id": "a", "title": "Router Xiaomi", "description": "router gigabit"}
        result = find_ambiguous(
            [item], lambda i: ["Dispositivos de red"],
            nodes=self.nodes, path_index=self.path_index,
        )
        assert result == []  # auto-resolved to Routers


# ---------------------------------------------------------------------------
# prompt_for_overrides — IO-injected interactive picker
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


class TestPromptForOverrides:
    def test_returns_full_path_for_picked_child(self):
        ambiguous = [
            ({"id": "a", "title": "Aparato"}, ["Dispositivos de red"],
             ["Routers", "Repetidores", "Módems"])
        ]
        io = _IO(["2"])
        out = prompt_for_overrides(ambiguous, input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {"a": ["Dispositivos de red", "Repetidores"]}

    def test_empty_input_means_skip(self):
        ambiguous = [
            ({"id": "a"}, ["Dispositivos de red"], ["Routers", "Repetidores"]),
        ]
        io = _IO([""])
        out = prompt_for_overrides(ambiguous, input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {"a": None}

    def test_quit_means_skip(self):
        ambiguous = [
            ({"id": "a"}, ["X"], ["a", "b"]),
        ]
        io = _IO(["q"])
        out = prompt_for_overrides(ambiguous, input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {"a": None}

    def test_eof_means_skip_remaining(self):
        ambiguous = [
            ({"id": "a"}, ["X"], ["a"]),
            ({"id": "b"}, ["Y"], ["c"]),
        ]
        io = _IO([])  # EOF on every prompt
        out = prompt_for_overrides(ambiguous, input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {"a": None, "b": None}

    def test_non_integer_means_skip(self):
        ambiguous = [({"id": "a"}, ["X"], ["a", "b"])]
        io = _IO(["abc"])
        out = prompt_for_overrides(ambiguous, input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {"a": None}

    def test_out_of_range_means_skip(self):
        ambiguous = [({"id": "a"}, ["X"], ["a", "b"])]
        io = _IO(["99"])
        out = prompt_for_overrides(ambiguous, input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {"a": None}

    def test_no_ambiguous_no_prompts(self):
        io = _IO([])  # would raise if called
        out = prompt_for_overrides([], input_fn=io.input_fn, output_fn=io.output_fn)
        assert out == {}
        assert io.outputs == []  # silent

    def test_renders_id_title_and_candidates(self):
        ambiguous = [
            ({"id": "abc", "title": "Foo", "description": "bar"},
             ["X"], ["a", "b"]),
        ]
        io = _IO(["1"])
        prompt_for_overrides(ambiguous, input_fn=io.input_fn, output_fn=io.output_fn)
        rendered = "\n".join(io.outputs)
        assert "abc" in rendered
        assert "Foo" in rendered
        assert "bar" in rendered
        assert "1. a" in rendered
        assert "2. b" in rendered

    def test_truncates_long_descriptions(self):
        long_desc = "x" * 500
        ambiguous = [
            ({"id": "a", "description": long_desc}, ["X"], ["c"]),
        ]
        io = _IO(["1"])
        prompt_for_overrides(ambiguous, input_fn=io.input_fn, output_fn=io.output_fn)
        rendered = "\n".join(io.outputs)
        # Truncation marker should appear; the full 500-char string should not.
        assert "..." in rendered
        assert long_desc not in rendered


# ---------------------------------------------------------------------------
# apply_overrides — mutation
# ---------------------------------------------------------------------------


class TestApplyOverrides:
    def test_writes_override(self):
        items = [{"id": "a"}]
        applied = apply_overrides(items, {"a": ["X", "Y"]})
        assert items[0][NAV_OVERRIDE_KEY] == ["X", "Y"]
        assert applied == 1

    def test_skipped_items_are_not_marked(self):
        items = [{"id": "a"}]
        applied = apply_overrides(items, {"a": None})
        assert NAV_OVERRIDE_KEY not in items[0]
        assert applied == 0

    def test_ignores_unknown_ids(self):
        items = [{"id": "a"}]
        apply_overrides(items, {"unknown": ["X"]})
        assert items[0] == {"id": "a"}

    def test_stores_copy_not_reference(self):
        # Defensive: callers shouldn't be able to mutate the override
        # afterwards via the original list reference.
        path = ["X", "Y"]
        items = [{"id": "a"}]
        apply_overrides(items, {"a": path})
        path.append("Z")
        assert items[0][NAV_OVERRIDE_KEY] == ["X", "Y"]

    def test_handles_mixed_picks_and_skips(self):
        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        applied = apply_overrides(items, {"a": ["X"], "b": None, "c": ["Y"]})
        assert items[0][NAV_OVERRIDE_KEY] == ["X"]
        assert NAV_OVERRIDE_KEY not in items[1]
        assert items[2][NAV_OVERRIDE_KEY] == ["Y"]
        assert applied == 2
