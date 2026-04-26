"""Tests for domain.text — pure string helpers used across the upload flow."""

from domain.text import (
    find_option_match,
    normalize_label,
    soften_title_caps,
    stem,
    testid_to_key,
)


class TestNormalizeLabel:
    def test_strips_accents(self):
        assert normalize_label("Cámara") == "camara"
        assert normalize_label("Móvil") == "movil"

    def test_lowercases(self):
        assert normalize_label("MARCA") == "marca"

    def test_replaces_underscores_and_hyphens_with_spaces(self):
        assert normalize_label("sistema_operativo") == "sistema operativo"
        assert normalize_label("azul-marino") == "azul marino"

    def test_collapses_whitespace(self):
        assert normalize_label("  foo   bar\tbaz ") == "foo bar baz"

    def test_empty_string(self):
        assert normalize_label("") == ""

    def test_none_safe(self):
        # upload_vinted calls this with values that may be None-ish;
        # original helper guards with `s or ""` — preserve that contract.
        assert normalize_label(None) == ""


class TestSoftenTitleCaps:
    def test_all_uppercase(self):
        assert soften_title_caps("LIBROS SUSE LINUX") == "Libros suse linux"

    def test_mixed_case(self):
        assert soften_title_caps("Libros SUSE LINUX oficial") == "Libros suse linux oficial"

    def test_already_lowercase(self):
        assert soften_title_caps("cámara canon") == "Cámara canon"

    def test_empty_string(self):
        assert soften_title_caps("") == ""

    def test_single_char(self):
        assert soften_title_caps("x") == "X"


class TestFindOptionMatch:
    def test_exact_normalized_match_wins(self):
        options = ["Azul Marino", "Azul", "Azul Claro"]
        assert find_option_match(options, "azul") == "Azul"

    def test_exact_match_wins_over_substring(self):
        # "azul" appears in "Azul Marino" too, but the exact match should be picked.
        options = ["Azul Marino", "Azul"]
        assert find_option_match(options, "Azul") == "Azul"

    def test_substring_fallback(self):
        options = ["Azul Marino", "Rojo"]
        assert find_option_match(options, "marino") == "Azul Marino"

    def test_strict_mode_skips_substring(self):
        options = ["Azul Marino"]
        assert find_option_match(options, "marino", strict=True) is None

    def test_strict_mode_still_accepts_exact(self):
        options = ["Nike", "Adidas"]
        assert find_option_match(options, "nike", strict=True) == "Nike"

    def test_empty_options(self):
        assert find_option_match([], "azul") is None

    def test_empty_value(self):
        assert find_option_match(["Azul"], "") is None


class TestStem:
    def test_plural_s(self):
        assert stem("routers") == "router"

    def test_plural_es(self):
        assert stem("buses") == "bus"

    def test_normalizes_accents_first(self):
        # "módems" → normalize removes accent → "modems" → strip 's' → "modem"
        assert stem("módems") == "modem"

    def test_short_word_unchanged(self):
        # len("ra") == 2 < 3, so 'm' should not be chopped
        assert stem("ram") == "ram"

    def test_already_singular(self):
        assert stem("router") == "router"

    def test_empty_string(self):
        assert stem("") == ""


class TestTestidToKey:
    def test_strips_single_list_input_suffix(self):
        assert testid_to_key("brand-single-list-input") == "brand"

    def test_replaces_hyphens_with_underscores(self):
        assert testid_to_key("operating-system-single-list-input") == "operating_system"

    def test_preserves_unknown_suffix(self):
        # Unknown suffix is not stripped — only converted hyphens-to-underscores.
        assert testid_to_key("color-dropdown-input") == "color_dropdown_input"

    def test_empty_string(self):
        assert testid_to_key("") == ""
