"""Tests for domain.mapping — Vinted label ↔ Wallapop attribute resolution."""

from domain.mapping import (
    COLOR_EN_TO_ES,
    LABEL_ALIASES,
    guess_wallapop_key,
    label_to_wallapop_key,
)


class TestGuessWallapopKey:
    def test_direct_match(self):
        # The Vinted label literally matches a key in attributes
        assert guess_wallapop_key("Color", {"color": "rojo"}) == "color"

    def test_alias_match(self):
        # "Marca" → alias "brand", which is the Wallapop key
        assert guess_wallapop_key("Marca", {"brand": "Sony"}) == "brand"

    def test_alias_match_normalises_label(self):
        # Spanish accents and casing in the label get normalised before alias lookup
        assert (
            guess_wallapop_key("SISTEMA OPERATIVO", {"operating_system": "iOS"})
            == "operating_system"
        )

    def test_substring_fallback(self):
        # "Memoria RAM" label, item has "ram" key — substring match wins
        assert guess_wallapop_key("Memoria RAM", {"ram": "8GB"}) == "ram"

    def test_no_match_returns_none(self):
        assert guess_wallapop_key("Talla", {"color": "rojo"}) is None

    def test_empty_attributes(self):
        assert guess_wallapop_key("Color", {}) is None


class TestLabelToWallapopKey:
    def test_known_alias(self):
        assert label_to_wallapop_key("Color") == "color"
        assert label_to_wallapop_key("Marca") == "brand"
        assert label_to_wallapop_key("Talla") == "size"

    def test_normalises_label(self):
        # Accents + uppercase get normalised before lookup
        assert label_to_wallapop_key("SISTEMA OPERATIVO") == "operating_system"

    def test_unknown_label_returns_none(self):
        assert label_to_wallapop_key("Diametro de la rueda") is None

    def test_returns_first_alias_when_multiple(self):
        # "color" maps to ["color", "colour"] — should return the first
        assert label_to_wallapop_key("Color") == "color"

    def test_normalised_label_matches_known_key(self):
        # When the label itself is the canonical Wallapop key (no alias entry needed)
        # — e.g. "ram" is in LABEL_ALIASES["ram"] = ["ram", "memory"]
        assert label_to_wallapop_key("RAM") == "ram"


class TestColorEnToEs:
    def test_basic_colors(self):
        assert COLOR_EN_TO_ES["orange"] == "Naranja"
        assert COLOR_EN_TO_ES["silver"] == "Plateado"
        assert COLOR_EN_TO_ES["black"] == "Negro"

    def test_british_alias(self):
        # 'multicolour' is the British spelling — both keys map to the same Spanish value
        assert COLOR_EN_TO_ES["multicolour"] == "Multicolor"
        assert COLOR_EN_TO_ES["multicolor"] == "Multicolor"


class TestLabelAliasesAlwaysFillRule:
    """The Marca/Talla/Color/Estado rule is documented as 'always-fill'.

    Removing any of these aliases would silently break the rule that those four
    labels resolve even for items that don't have the attribute.
    """

    def test_always_fill_aliases_present(self):
        assert "marca" in LABEL_ALIASES
        assert "talla" in LABEL_ALIASES
        assert "color" in LABEL_ALIASES
        assert "estado" in LABEL_ALIASES

    def test_marca_resolves_without_concrete_item(self):
        # Even if the item has no "brand" key, "Marca" must still resolve to "brand"
        # so the upload flow can fill the field with a sensible default.
        assert label_to_wallapop_key("Marca") == "brand"
