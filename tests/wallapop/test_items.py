"""Tests for ``wallapop.items``.

The module is split into:

- ``build_item_record`` — pure, no I/O. Most tests live here because the
  output dict is the contract every downstream consumer relies on.
- ``download_item_images`` — HTTP + filesystem. Mocked with ``responses``
  and ``tmp_path``; the dedup branch is the interesting case.
- ``process_item`` — orchestrator. Tested as integration with both layers
  mocked, plus the ``ensure_mapped`` callback contract.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
import responses

from wallapop.items import (
    build_item_record,
    download_item_images,
    process_item,
)


@pytest.fixture
def session():
    return requests.Session()


# ---------- build_item_record (pure) ----------


class TestBuildItemRecord:
    def test_canonical_output_shape(self):
        raw = {
            "id": "abc",
            "title": "Test",
            "description": "desc",
            "price": {"amount": 10, "currency": "EUR"},
            "shipping": {"user_allows_shipping": True},
            "type_attributes": {},
            "slug": "test-abc",
        }
        record = build_item_record(raw, leaf_cat_id="123", image_paths=["a.jpg"])
        assert set(record) == {
            "title",
            "description",
            "price",
            "currency",
            "category_id",
            "attributes",
            "shipping_allowed",
            "images",
            "url",
            "extracted_at",
        }
        assert record["title"] == "Test"
        assert record["description"] == "desc"
        assert record["price"] == 10
        assert record["currency"] == "EUR"
        assert record["category_id"] == "123"
        assert record["images"] == ["a.jpg"]
        assert record["shipping_allowed"] is True

    def test_shipping_allowed_false_propagated(self):
        raw = {"shipping": {"user_allows_shipping": False}}
        record = build_item_record(raw, leaf_cat_id="1", image_paths=[])
        assert record["shipping_allowed"] is False

    def test_shipping_allowed_default_true_when_missing(self):
        record = build_item_record({}, leaf_cat_id="1", image_paths=[])
        assert record["shipping_allowed"] is True

    def test_currency_defaults_to_eur(self):
        raw = {"price": {"amount": 5}}
        record = build_item_record(raw, leaf_cat_id="1", image_paths=[])
        assert record["currency"] == "EUR"

    def test_attributes_flatten_type_attributes(self):
        raw = {
            "type_attributes": {
                "color": {"value": "rojo"},
                "size": {"value": "M"},
            }
        }
        record = build_item_record(raw, leaf_cat_id="1", image_paths=[])
        assert record["attributes"] == {"color": "rojo", "size": "M"}

    def test_attributes_drop_empty_and_none_values(self):
        raw = {
            "type_attributes": {
                "color": {"value": "rojo"},
                "size": {"value": ""},
                "brand": {"value": None},
            }
        }
        record = build_item_record(raw, leaf_cat_id="1", image_paths=[])
        assert record["attributes"] == {"color": "rojo"}

    def test_url_uses_slug_when_present(self):
        raw = {"id": "abc", "slug": "my-item-abc"}
        record = build_item_record(raw, leaf_cat_id="1", image_paths=[])
        assert record["url"] == "https://es.wallapop.com/item/my-item-abc"

    def test_url_falls_back_to_id_when_slug_missing(self):
        raw = {"id": "abc"}
        record = build_item_record(raw, leaf_cat_id="1", image_paths=[])
        assert record["url"] == "https://es.wallapop.com/item/abc"

    def test_images_passed_through(self):
        record = build_item_record({}, leaf_cat_id="1", image_paths=["a", "b"])
        assert record["images"] == ["a", "b"]

    def test_extracted_at_is_iso_format(self):
        record = build_item_record({}, leaf_cat_id="1", image_paths=[])
        # round-trips through ``fromisoformat`` only if it's a valid ISO 8601 string
        datetime.fromisoformat(record["extracted_at"])


# ---------- download_item_images (I/O + HTTP) ----------


class TestDownloadItemImages:
    @responses.activate
    def test_downloads_each_image(self, session, tmp_path):
        responses.add(
            responses.GET, "https://example.com/a.jpg", body=b"img1", status=200
        )
        responses.add(
            responses.GET, "https://example.com/b.jpg", body=b"img2", status=200
        )
        raw = {
            "images": [
                {"urls": {"big": "https://example.com/a.jpg"}},
                {"urls": {"big": "https://example.com/b.jpg"}},
            ]
        }
        paths = download_item_images(raw, tmp_path, session=session)
        assert len(paths) == 2
        bytes_on_disk = {Path(p).read_bytes() for p in paths}
        assert bytes_on_disk == {b"img1", b"img2"}

    @responses.activate
    def test_dedup_skips_existing_file(self, session, tmp_path):
        existing = tmp_path / "0.jpg"
        existing.write_bytes(b"already-here")
        # No HTTP mock registered — if the function tried to fetch, ``responses``
        # would raise ConnectionError and the test would fail.
        raw = {"images": [{"urls": {"big": "https://example.com/0.jpg"}}]}
        paths = download_item_images(raw, tmp_path, session=session)
        assert paths == [str(existing)]
        assert existing.read_bytes() == b"already-here"
        assert len(responses.calls) == 0

    @responses.activate
    def test_preserves_order(self, session, tmp_path):
        responses.add(
            responses.GET, "https://example.com/a.jpg", body=b"a", status=200
        )
        responses.add(
            responses.GET, "https://example.com/b.jpg", body=b"b", status=200
        )
        raw = {
            "images": [
                {"urls": {"big": "https://example.com/a.jpg"}},
                {"urls": {"big": "https://example.com/b.jpg"}},
            ]
        }
        paths = download_item_images(raw, tmp_path, session=session)
        assert Path(paths[0]).read_bytes() == b"a"
        assert Path(paths[1]).read_bytes() == b"b"

    @responses.activate
    def test_skips_empty_urls(self, session, tmp_path):
        responses.add(
            responses.GET, "https://example.com/x.jpg", body=b"x", status=200
        )
        raw = {
            "images": [
                {"urls": {"big": ""}},
                {"urls": {"big": "https://example.com/x.jpg"}},
            ]
        }
        paths = download_item_images(raw, tmp_path, session=session)
        assert len(paths) == 1
        assert Path(paths[0]).read_bytes() == b"x"

    @responses.activate
    def test_failed_download_not_in_result(self, session, tmp_path):
        responses.add(
            responses.GET, "https://example.com/fail.jpg", status=500
        )
        raw = {"images": [{"urls": {"big": "https://example.com/fail.jpg"}}]}
        paths = download_item_images(raw, tmp_path, session=session)
        assert paths == []

    def test_creates_dest_dir_if_missing(self, session, tmp_path):
        nested = tmp_path / "nope" / "still-nope"
        # No images so no HTTP needed; we only assert the dir gets created.
        download_item_images({"images": []}, nested, session=session)
        assert nested.is_dir()


# ---------- process_item (orchestrator) ----------


class TestProcessItem:
    def test_returns_none_when_id_empty(self, tmp_path, session):
        record = process_item({}, session=session, images_dir=tmp_path)
        assert record is None

    @responses.activate
    def test_uses_leaf_category_from_taxonomy(self, tmp_path, session):
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/items/abc",
            json={"taxonomy": [{"id": "100"}, {"id": "999"}]},
            status=200,
        )
        raw = {"id": "abc", "category_id": "5", "images": []}
        record = process_item(raw, session=session, images_dir=tmp_path)
        assert record["category_id"] == "999"

    @responses.activate
    def test_falls_back_to_root_category_id_when_taxonomy_empty(
        self, tmp_path, session
    ):
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/items/abc",
            json={"taxonomy": []},
            status=200,
        )
        raw = {"id": "abc", "category_id": 5, "images": []}
        record = process_item(raw, session=session, images_dir=tmp_path)
        assert record["category_id"] == "5"

    @responses.activate
    def test_calls_ensure_mapped_with_leaf_category(self, tmp_path, session):
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/items/abc",
            json={"taxonomy": [{"id": "777"}]},
            status=200,
        )
        ensure = MagicMock()
        raw = {"id": "abc", "images": []}
        process_item(
            raw, session=session, images_dir=tmp_path, ensure_mapped=ensure
        )
        ensure.assert_called_once_with("777")

    @responses.activate
    def test_full_integration(self, tmp_path, session):
        responses.add(
            responses.GET,
            "https://api.wallapop.com/api/v3/items/abc",
            json={"taxonomy": [{"id": "12345"}]},
            status=200,
        )
        responses.add(
            responses.GET,
            "https://example.com/0.jpg",
            body=b"img-bytes",
            status=200,
        )
        raw = {
            "id": "abc",
            "title": "Cool item",
            "description": "Nice",
            "price": {"amount": 50, "currency": "EUR"},
            "shipping": {"user_allows_shipping": True},
            "type_attributes": {"color": {"value": "rojo"}},
            "images": [{"urls": {"big": "https://example.com/0.jpg"}}],
            "slug": "cool-item-abc",
        }
        record = process_item(raw, session=session, images_dir=tmp_path)
        assert record["title"] == "Cool item"
        assert record["category_id"] == "12345"
        assert len(record["images"]) == 1
        assert Path(record["images"][0]).read_bytes() == b"img-bytes"
        assert record["attributes"] == {"color": "rojo"}
        assert record["url"] == "https://es.wallapop.com/item/cool-item-abc"
