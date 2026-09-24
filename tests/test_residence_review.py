"""Residence review data survives restarts without changing contact records."""

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from residence_review import ADDRESS_FIELDS, ResidenceReviews


ROW = {"id": "record-a", "person": {"given_name": "Example", "family_name": "Member",
       "address_line_1": "10 Example Street", "locality": "Newark", "region": "OH", "postal_code": "43055"}}
VALUES = {"county": "Licking", "region": "OH", "source": "manual"}


class ResidenceReviewsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.queue_dir = Path(temporary.name) / "queue"
        self.reviews = ResidenceReviews(self.queue_dir)

    def test_restart_persists_without_changing_contact_or_using_caller_timestamp(self):
        row = copy.deepcopy(ROW)
        before = copy.deepcopy(row)
        self.assertEqual(self.reviews.get(row), {})
        self.assertFalse(self.queue_dir.exists())
        start = datetime.now(timezone.utc)
        saved = self.reviews.save(row, {**VALUES, "checked_at": "not-a-timestamp"})
        self.assertEqual(row, before)
        self.assertEqual(ResidenceReviews(self.queue_dir).get(row), saved)
        self.assertGreaterEqual(datetime.fromisoformat(saved["checked_at"]), start)
        self.assertEqual(datetime.fromisoformat(saved["checked_at"]).utcoffset().total_seconds(), 0)
        saved["county"] = "Changed"
        self.assertEqual(self.reviews.get(row)["county"], "Licking")

    def test_each_address_field_or_record_id_change_invalidates_review(self):
        self.reviews.save(ROW, VALUES)
        for field in ADDRESS_FIELDS:
            changed = copy.deepcopy(ROW)
            changed["person"][field] += " changed"
            with self.subTest(field=field):
                self.assertEqual(self.reviews.get(changed), {})
        self.assertEqual(self.reviews.get({**ROW, "id": "record-b"}), {})
        non_address_change = copy.deepcopy(ROW)
        non_address_change["person"]["given_name"] = "Corrected"
        self.assertEqual(self.reviews.get(non_address_change)["county"], "Licking")

    def test_invalid_values_are_rejected_and_prior_review_is_preserved(self):
        self.reviews.save(ROW, VALUES)
        before = self.reviews.path.read_bytes()
        invalid = ({"source": "unknown"}, {"source": []}, {"county": ""}, {"county": " "},
                   {"county": "a" * 101}, {"county": "Licking\nprivate"}, {"county": "a\x00b"},
                   {"region": "oh"}, {"region": "Ohio"}, {"matched_address": "line\rbreak"})
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.reviews.save(ROW, {**VALUES, **value})
            self.assertEqual(self.reviews.path.read_bytes(), before)

    def test_census_match_is_saved_and_replaced_once_per_record(self):
        self.reviews.save(ROW, {**VALUES, "source": "census", "matched_address": "10 EXAMPLE ST, NEWARK OH 43055"})
        self.assertIn("matched_address", self.reviews.get(ROW))
        self.reviews.save(ROW, VALUES)
        other = {**ROW, "id": "record-b"}
        self.reviews.save(other, {**VALUES, "county": "Perry"})
        stored = json.loads(self.reviews.path.read_text())
        self.assertEqual(len(stored["records"]), 2)
        self.assertNotIn("matched_address", self.reviews.get(ROW))
        self.assertEqual(self.reviews.get(other)["county"], "Perry")

    def test_corrupt_or_unsupported_history_is_preserved(self):
        self.queue_dir.mkdir()
        bad_documents = [b"private invalid JSON", b'{"version":2,"records":{}}',
                         b'{"version":true,"records":{}}', b'{"version":1,"version":1,"records":{}}',
                         b'{"version":1,"records":{"private-record":{"address_digest":"bad"}}}', b"\xff"]
        for contents in bad_documents:
            self.reviews.path.write_bytes(contents)
            with self.subTest(contents=contents):
                for action in (lambda: self.reviews.get(ROW), lambda: self.reviews.save(ROW, VALUES)):
                    with self.assertRaises(ValueError) as caught:
                        action()
                    self.assertNotIn("private", str(caught.exception))
                    self.assertEqual(self.reviews.path.read_bytes(), contents)

    def test_invalid_saved_annotation_is_not_silently_overwritten(self):
        self.reviews.save(ROW, VALUES)
        saved = json.loads(self.reviews.path.read_text())
        saved["records"][ROW["id"]]["review"]["checked_at"] = "2026-01-01T12:00:00"
        self.reviews.path.write_text(json.dumps(saved))
        before = self.reviews.path.read_bytes()
        with self.assertRaises(ValueError):
            self.reviews.save(ROW, VALUES)
        self.assertEqual(self.reviews.path.read_bytes(), before)

    def test_failed_atomic_replace_preserves_history_and_removes_temporary_file(self):
        self.reviews.save(ROW, VALUES)
        before = self.reviews.path.read_bytes()
        with patch("residence_review.os.replace", side_effect=OSError("replacement failed")):
            with self.assertRaises(OSError):
                self.reviews.save(ROW, {**VALUES, "county": "Perry"})
        self.assertEqual(self.reviews.path.read_bytes(), before)
        self.assertEqual(list(self.queue_dir.iterdir()), [self.reviews.path])

    @unittest.skipUnless(os.name == "posix", "POSIX private file mode")
    def test_file_is_private_after_create_and_replace(self):
        self.reviews.save(ROW, VALUES)
        self.assertEqual(self.reviews.path.stat().st_mode & 0o777, 0o600)
        self.reviews.path.chmod(0o644)
        self.reviews.save(ROW, VALUES)
        self.assertEqual(self.reviews.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
