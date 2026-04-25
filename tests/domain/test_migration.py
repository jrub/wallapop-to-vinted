"""Tests for domain.migration — migration.json I/O + the pending-items filter."""

import json

from domain.migration import (
    filter_pending,
    load_migration,
    mark_migrated,
    migration_status,
)


class TestLoadMigration:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert load_migration(tmp_path / "missing.json") == {}

    def test_valid_file(self, tmp_path):
        path = tmp_path / "migration.json"
        path.write_text('{"42": {"vinted_id": "abc", "status": "published"}}')
        assert load_migration(path) == {
            "42": {"vinted_id": "abc", "status": "published"}
        }


class TestMarkMigrated:
    def test_writes_indented_utf8(self, tmp_path):
        path = tmp_path / "migration.json"
        migration: dict = {}
        mark_migrated(migration, path, "42", "abc", "published")
        text = path.read_text(encoding="utf-8")
        assert "\n" in text  # indented (not single-line)
        loaded = json.loads(text)
        assert loaded["42"]["vinted_id"] == "abc"
        assert loaded["42"]["status"] == "published"

    def test_preserves_existing_entries(self, tmp_path):
        path = tmp_path / "migration.json"
        migration = {"99": {"vinted_id": "xyz", "status": "published"}}
        mark_migrated(migration, path, "42", "abc", "draft")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert "99" in loaded
        assert "42" in loaded

    def test_updates_uploaded_at_timestamp(self, tmp_path):
        path = tmp_path / "migration.json"
        migration: dict = {}
        mark_migrated(migration, path, "42", "abc", "published")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert "uploaded_at" in loaded["42"]
        # ISO 8601 starts with a 4-digit year
        assert loaded["42"]["uploaded_at"][:4].isdigit()

    def test_missing_fields_default_empty_list(self, tmp_path):
        path = tmp_path / "migration.json"
        mark_migrated({}, path, "42", "abc", "published")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["42"]["missing_fields"] == []

    def test_preserves_utf8_chars(self, tmp_path):
        path = tmp_path / "migration.json"
        mark_migrated({}, path, "42", "abc", "draft", error="cámara fallida")
        text = path.read_text(encoding="utf-8")
        assert "cámara" in text  # not escaped to \u00e1


class TestMigrationStatus:
    def test_new_status_key(self):
        assert migration_status({"status": "published"}) == "published"

    def test_legacy_vinted_status_key(self):
        # Older migration.json files used "vinted_status"
        assert migration_status({"vinted_status": "draft"}) == "draft"

    def test_new_key_takes_precedence(self):
        assert (
            migration_status({"status": "draft", "vinted_status": "failed"}) == "draft"
        )

    def test_empty_entry(self):
        assert migration_status({}) == ""


class TestFilterPending:
    """`filter_pending` is the rule that justifies the module: --limit applies AFTER
    skipping already-uploaded and unmapped items, so --limit 1 always means
    "one real upload attempt" regardless of migration.json state.
    """

    @staticmethod
    def _items():
        return [
            {"id": "1", "title": "published item", "category_id": "C1"},
            {"id": "2", "title": "drafted item", "category_id": "C1"},
            {"id": "3", "title": "failed item", "category_id": "C1"},
            {"id": "4", "title": "fresh item", "category_id": "C1"},
            {"id": "5", "title": "unmapped item", "category_id": "C2"},
        ]

    @staticmethod
    def _migration():
        return {
            "1": {"vinted_id": "v1", "status": "published"},
            "2": {"vinted_id": "v2", "status": "draft"},
            "3": {"vinted_id": "", "status": "failed"},
        }

    @staticmethod
    def _get_nav(item):
        # C1 has a nav, C2 doesn't
        return ["Cat", "Sub"] if item.get("category_id") == "C1" else None

    def test_published_skipped(self):
        pending, already_done, unmapped = filter_pending(
            self._items(), self._migration(), self._get_nav
        )
        assert "1" not in [i["id"] for i in pending]

    def test_draft_skipped(self):
        pending, _, _ = filter_pending(
            self._items(), self._migration(), self._get_nav
        )
        assert "2" not in [i["id"] for i in pending]

    def test_failed_retried(self):
        pending, _, _ = filter_pending(
            self._items(), self._migration(), self._get_nav
        )
        assert "3" in [i["id"] for i in pending]

    def test_unseen_pending(self):
        pending, _, _ = filter_pending(
            self._items(), self._migration(), self._get_nav
        )
        assert "4" in [i["id"] for i in pending]

    def test_unmapped_separated(self):
        pending, _, unmapped = filter_pending(
            self._items(), self._migration(), self._get_nav
        )
        assert "5" not in [i["id"] for i in pending]
        assert any(u[0] == "5" for u in unmapped)

    def test_already_done_count(self):
        _, already_done, _ = filter_pending(
            self._items(), self._migration(), self._get_nav
        )
        # items 1 (published) and 2 (draft) — failed (3) doesn't count
        assert already_done == 2

    def test_limit_applied_after_skips(self):
        # 5 items, 2 already done, 1 unmapped, 2 pending.  --limit 1 must yield 1 pending,
        # not 0 (which is what `items[:1]` would give if applied before the filters).
        pending, _, _ = filter_pending(
            self._items(), self._migration(), self._get_nav, limit=1
        )
        assert len(pending) == 1

    def test_retry_drafts_returns_all_items_unchanged(self):
        # With retry_drafts the orchestrator does its own filtering against existing
        # drafts on Vinted, so filter_pending must not strip anything.
        pending, already_done, unmapped = filter_pending(
            self._items(), self._migration(), self._get_nav, retry_drafts=True
        )
        assert len(pending) == 5
        assert already_done == 0
        assert unmapped == []

    def test_limit_zero_means_no_limit(self):
        # `--limit 0` is the smoke-test value the user passes to make sure imports work
        # without consuming the queue. Keep "0 = unlimited" so it doesn't regress to
        # "0 = empty list".
        pending, _, _ = filter_pending(
            self._items(), self._migration(), self._get_nav, limit=0
        )
        assert len(pending) == 2  # items 3 and 4
