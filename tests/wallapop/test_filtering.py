"""Tests for the in-person classifier and the category-mapping stub writer.

Both functions live in ``wallapop.items`` and are pulled out of
``extract_wallapop.main`` so the queue-shaping decisions can be unit
tested without HTTP. ``ensure_category_mapped`` keeps its file-based I/O
but takes the path and category-tree dict by argument (DI) so each test
gets its own fresh ``tmp_path``.
"""

import json
from pathlib import Path

import pytest

from wallapop.items import ensure_category_mapped, filter_in_person


# ---------- filter_in_person (pure) ----------


def _shipping_item(id_: str) -> dict:
    return {"id": id_, "shipping": {"user_allows_shipping": True}}


def _in_person_item(id_: str) -> dict:
    return {"id": id_, "shipping": {"user_allows_shipping": False}}


class TestFilterInPerson:
    def test_no_in_person_items(self):
        items = [_shipping_item("a"), _shipping_item("b")]
        kept, in_person = filter_in_person(items, include_in_person=False)
        assert kept == items
        assert in_person == []

    def test_mixed_items_include_false_drops_in_person(self):
        items = [
            _shipping_item("a"),
            _in_person_item("b"),
            _shipping_item("c"),
            _in_person_item("d"),
        ]
        kept, in_person = filter_in_person(items, include_in_person=False)
        assert [it["id"] for it in kept] == ["a", "c"]
        assert [it["id"] for it in in_person] == ["b", "d"]

    def test_mixed_items_include_true_keeps_all(self):
        items = [
            _shipping_item("a"),
            _in_person_item("b"),
            _shipping_item("c"),
        ]
        kept, in_person = filter_in_person(items, include_in_person=True)
        # ``kept`` keeps everything when the flag is on; ``in_person`` is the
        # classification regardless of the flag — caller needs it for the
        # "Including N as shipping" message.
        assert kept == items
        assert [it["id"] for it in in_person] == ["b"]

    def test_missing_shipping_field_treated_as_shipping(self):
        items = [{"id": "a"}, {"id": "b", "shipping": None}]
        kept, in_person = filter_in_person(items, include_in_person=False)
        assert [it["id"] for it in kept] == ["a", "b"]
        assert in_person == []

    def test_empty_list(self):
        assert filter_in_person([], include_in_person=False) == ([], [])
        assert filter_in_person([], include_in_person=True) == ([], [])

    def test_does_not_mutate_input(self):
        items = [_shipping_item("a"), _in_person_item("b")]
        original = [dict(it) for it in items]
        filter_in_person(items, include_in_person=False)
        assert items == original


# ---------- ensure_category_mapped (DI) ----------


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


class TestEnsureCategoryMapped:
    def test_new_category_writes_stub(self, tmp_path):
        cats_path = tmp_path / "category_mapping.json"
        _write_json(cats_path, {})
        wallapop_cats = {"24076": {"name": "No ficción"}}

        ensure_category_mapped(
            "24076", categories_path=cats_path, wallapop_cats=wallapop_cats
        )

        cats = json.loads(cats_path.read_text(encoding="utf-8"))
        assert cats == {"24076": {"name": "No ficción", "vinted": None}}

    def test_existing_category_no_op(self, tmp_path):
        cats_path = tmp_path / "category_mapping.json"
        existing = {
            "24076": {"name": "No ficción", "vinted": ["Libros", "No ficción"]}
        }
        _write_json(cats_path, existing)
        before_mtime = cats_path.stat().st_mtime_ns

        ensure_category_mapped(
            "24076", categories_path=cats_path, wallapop_cats={}
        )

        # Untouched: same content AND same mtime (no rewrite happened)
        assert json.loads(cats_path.read_text(encoding="utf-8")) == existing
        assert cats_path.stat().st_mtime_ns == before_mtime

    def test_wallapop_cats_missing_id_falls_back_to_id_as_name(self, tmp_path):
        cats_path = tmp_path / "category_mapping.json"
        _write_json(cats_path, {})

        ensure_category_mapped(
            "99999", categories_path=cats_path, wallapop_cats={}
        )

        cats = json.loads(cats_path.read_text(encoding="utf-8"))
        # Preserves the existing fallback: when wallapop_cats has no entry,
        # the stub's name is the id itself so the human reviewer at least
        # knows what to look up. Empty string would be uglier here.
        assert cats == {"99999": {"name": "99999", "vinted": None}}

    def test_empty_category_id_no_op(self, tmp_path):
        cats_path = tmp_path / "category_mapping.json"
        _write_json(cats_path, {})

        ensure_category_mapped(
            "", categories_path=cats_path, wallapop_cats={"x": {"name": "X"}}
        )

        assert json.loads(cats_path.read_text(encoding="utf-8")) == {}

    def test_categories_path_missing_no_op(self, tmp_path):
        # File doesn't exist — the function silently returns without
        # creating it. Matches the original guard.
        cats_path = tmp_path / "does_not_exist.json"
        ensure_category_mapped(
            "24076",
            categories_path=cats_path,
            wallapop_cats={"24076": {"name": "X"}},
        )
        assert not cats_path.exists()

    def test_preserves_unrelated_entries(self, tmp_path):
        cats_path = tmp_path / "category_mapping.json"
        _write_json(
            cats_path,
            {
                "10135": {"name": "Componentes", "vinted": ["Electrónica"]},
                "24076": {"name": "Libros", "vinted": None},
            },
        )

        ensure_category_mapped(
            "18000",
            categories_path=cats_path,
            wallapop_cats={"18000": {"name": "Cromos"}},
        )

        cats = json.loads(cats_path.read_text(encoding="utf-8"))
        assert "10135" in cats and cats["10135"]["vinted"] == ["Electrónica"]
        assert "24076" in cats
        assert cats["18000"] == {"name": "Cromos", "vinted": None}
