"""CLI parsing contract for ``upload_vinted.py``.

Exercises ``_build_parser`` directly so flag combinations can be verified
without invoking ``main()`` end-to-end (which would load ``.env``, parse
``data/downloaded_items.json`` and try to launch a browser).

The flag we mostly care about is ``--item``, which has unusual argparse
semantics (``nargs="?"`` + ``const`` sentinel): ``--item ABC`` produces
``args.item == "ABC"``, bare ``--item`` produces a sentinel string, and
no flag produces ``None``. The downstream routing branches on those
three states, so freeze the parser's behaviour here.
"""

from __future__ import annotations

import pytest

from upload_vinted import _INTERACTIVE_ITEM, _build_parser


@pytest.fixture
def parser():
    return _build_parser()


class TestItemFlag:
    def test_no_item_flag_yields_none(self, parser):
        args = parser.parse_args([])
        assert args.item is None

    def test_item_with_value(self, parser):
        args = parser.parse_args(["--item", "abc123"])
        assert args.item == "abc123"

    def test_bare_item_yields_interactive_sentinel(self, parser):
        args = parser.parse_args(["--item"])
        assert args.item == _INTERACTIVE_ITEM

    def test_item_does_not_consume_next_flag(self, parser):
        # --item with no value followed by another flag must not eat that flag.
        # nargs="?" is greedy by default but argparse correctly recognises
        # known flag prefixes.
        args = parser.parse_args(["--item", "--visible"])
        assert args.item == _INTERACTIVE_ITEM
        assert args.visible is True


class TestOtherFlagsStillWork:
    def test_limit_int(self, parser):
        args = parser.parse_args(["--limit", "5"])
        assert args.limit == 5

    def test_visible_default_false(self, parser):
        args = parser.parse_args([])
        assert args.visible is False

    def test_retry_drafts_default_false(self, parser):
        args = parser.parse_args([])
        assert args.retry_drafts is False

    def test_no_learn_default_false(self, parser):
        args = parser.parse_args([])
        assert args.no_learn is False


class TestFlagCombinations:
    def test_item_and_visible_are_independent(self, parser):
        args = parser.parse_args(["--item", "xyz", "--visible"])
        assert args.item == "xyz"
        assert args.visible is True

    def test_item_id_starting_with_digits(self, parser):
        # Wallapop ids look like "pzpr7nq13563" — alphanumeric, but defensive
        # against pure-numeric ids being parsed as ints somewhere.
        args = parser.parse_args(["--item", "12345"])
        assert args.item == "12345"
        assert isinstance(args.item, str)
