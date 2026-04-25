"""Tests for domain.categories — Vinted category tree navigation helpers."""

import pytest

from domain.categories import (
    build_path_index,
    pick_leaf_from_hints,
    resolve_nav_to_leaf,
)


@pytest.fixture
def tree():
    """Tiny in-memory Vinted-style category tree (3 levels, two branches)."""
    return [
        {"title": "Hobbies", "path": ["Hobbies"], "is_leaf": False},
        {
            "title": "Cartas coleccionables",
            "path": ["Hobbies", "Cartas coleccionables"],
            "is_leaf": False,
        },
        {
            "title": "Pokémon",
            "path": ["Hobbies", "Cartas coleccionables", "Pokémon"],
            "is_leaf": True,
        },
        {
            "title": "Magic",
            "path": ["Hobbies", "Cartas coleccionables", "Magic"],
            "is_leaf": True,
        },
        {"title": "Informática", "path": ["Informática"], "is_leaf": False},
        {
            "title": "Componentes",
            "path": ["Informática", "Componentes"],
            "is_leaf": False,
        },
        {
            "title": "RAM",
            "path": ["Informática", "Componentes", "RAM"],
            "is_leaf": True,
        },
        {
            "title": "Floppy",
            "path": ["Informática", "Componentes", "Floppy"],
            "is_leaf": True,
        },
        {
            "title": "Routers",
            "path": ["Informática", "Componentes", "Routers"],
            "is_leaf": True,
        },
    ]


@pytest.fixture
def index(tree):
    return build_path_index(tree)


class TestBuildPathIndex:
    def test_size_matches_node_count(self, tree, index):
        assert len(index) == len(tree)

    def test_tuple_key_lookup(self, index):
        node = index[("Informática", "Componentes", "RAM")]
        assert node["title"] == "RAM"
        assert node["is_leaf"] is True

    def test_intermediate_node_present(self, index):
        node = index[("Informática", "Componentes")]
        assert node["is_leaf"] is False

    def test_empty_input(self):
        assert build_path_index([]) == {}


class TestPickLeafFromHints:
    def test_unambiguous_match_returns_option(self):
        # "memoria ram ddr4" — only "RAM" matches
        picked = pick_leaf_from_hints(["RAM", "Floppy", "Routers"], "memoria ram ddr4")
        assert picked == "RAM"

    def test_stem_match(self):
        # hint plural "routers" should match option "Router" via stem
        picked = pick_leaf_from_hints(["Router", "Switch", "Hub"], "tengo dos routers en casa")
        assert picked == "Router"

    def test_ambiguous_match_returns_none(self):
        # "ram floppy" matches two options → ambiguous
        picked = pick_leaf_from_hints(["RAM", "Floppy", "Routers"], "ram floppy combo")
        assert picked is None

    def test_no_match_returns_none(self):
        picked = pick_leaf_from_hints(["RAM", "Floppy"], "guitarra eléctrica")
        assert picked is None

    def test_empty_hints_returns_none(self):
        assert pick_leaf_from_hints(["RAM", "Floppy"], "") is None

    def test_empty_options_returns_none(self):
        assert pick_leaf_from_hints([], "memoria ram") is None


class TestResolveNavToLeaf:
    def test_empty_index_returns_nav_unchanged(self, tree):
        nav = ["Informática", "Componentes"]
        assert resolve_nav_to_leaf(nav, "memoria ram", nodes=tree, path_index={}) == nav

    def test_path_not_in_index_returns_unchanged(self, tree, index):
        nav = ["Inexistente"]
        assert resolve_nav_to_leaf(nav, "irrelevant", nodes=tree, path_index=index) == nav

    def test_already_leaf_returns_unchanged(self, tree, index):
        nav = ["Informática", "Componentes", "RAM"]
        assert resolve_nav_to_leaf(nav, "irrelevant", nodes=tree, path_index=index) == nav

    def test_intermediate_unambiguous_hint_extends(self, tree, index):
        nav = ["Informática", "Componentes"]
        result = resolve_nav_to_leaf(nav, "memoria RAM ddr4", nodes=tree, path_index=index)
        assert result == ["Informática", "Componentes", "RAM"]

    def test_intermediate_ambiguous_hint_returns_unchanged(self, tree, index):
        nav = ["Informática", "Componentes"]
        # "ram floppy" matches two leaves → resolver bails out
        result = resolve_nav_to_leaf(nav, "ram floppy", nodes=tree, path_index=index)
        assert result == nav

    def test_recurses_two_levels(self, tree, index):
        # Start at root "Informática" — needs to descend through "Componentes"
        # to a leaf. The hint should pick "Componentes" (only child) and then "RAM".
        nav = ["Informática"]
        result = resolve_nav_to_leaf(
            nav, "memoria componentes ram", nodes=tree, path_index=index
        )
        assert result == ["Informática", "Componentes", "RAM"]
