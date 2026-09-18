"""Verify status folders, durable moves, and migration with invented records.

The manifest decides whether a record can be sent. Moving a JSON file by hand
must never erase its sending history. All filesystem changes here are temporary.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import record_queue
from record_queue import QueueError, RecordQueue
import test_extract_person as fixture


RECORD = fixture.EXPECTED
SUCCESS = {"person": {"identifiers": ["invented:receipt"]}}
FOLDERS = ("pending", "sent", "review")


class QueueLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.queue_dir = self.folder / "composed_info"

    def add_record(self, queue):
        queue.enqueue("morgan-organized", RECORD, {"invented-source"}, ["invented.pdf"])
        return queue.pending_records()[0]

    def assert_location(self, filename, expected_folder):
        expected = self.queue_dir / expected_folder / filename
        self.assertTrue(expected.is_file(), f"Expected {expected_folder}/{filename}")
        self.assertFalse((self.queue_dir / filename).exists())
        for folder in FOLDERS:
            if folder != expected_folder:
                self.assertFalse((self.queue_dir / folder / filename).exists())
        return expected

    def seed_flat_v1(self, statuses):
        """Write the previous public on-disk format independently of queue code."""
        self.queue_dir.mkdir()
        state = {"version": 1, "families": {}, "records": {}}
        filenames = {}
        for status in statuses:
            record = {**RECORD, "given_name": status.capitalize(), "email": f"{status}@example.test"}
            canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            identifier = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            filename = f"person-{identifier}.json"
            (self.queue_dir / filename).write_text(json.dumps(record), encoding="utf-8")
            state["records"][identifier] = {
                "status": status,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
            state["families"][f"{status}-organized"] = {
                "current_id": identifier, "record_ids": [identifier],
                "source_hashes": [f"source-{status}"], "source_names": [f"{status}.pdf"],
                "review": status == "review", "deferred": False, "reason": "",
            }
            filenames[status] = filename
        (self.queue_dir / ".queue-state.json").write_text(json.dumps(state), encoding="utf-8")
        return filenames

    def test_pending_claim_and_success_move_same_record_between_status_folders(self):
        with RecordQueue(self.queue_dir) as queue:
            self.assertTrue(all((self.queue_dir / name).is_dir() for name in FOLDERS))
            item = self.add_record(queue)
            self.assertEqual(item.path.parent, (self.queue_dir / "pending").resolve())
            self.assert_location(item.path.name, "pending")
            queue.begin_send(item)
            self.assert_location(item.path.name, "review")
            self.assertEqual(queue.status_for("morgan-organized"), "sending")
            # The original QueueItem remains usable after its file has moved.
            queue.finish_send(item, SUCCESS)
            sent = self.assert_location(item.path.name, "sent")
            self.assertEqual(json.loads(sent.read_text(encoding="utf-8")), RECORD)
            self.assertEqual(queue.pending_records(), [])

    def test_deferred_held_and_uncertain_records_live_in_review(self):
        with RecordQueue(self.queue_dir) as queue:
            item = self.add_record(queue)
            queue.defer("morgan-organized", ["copy.pdf.part"], "Still downloading.")
            self.assert_location(item.path.name, "review")
            queue.enqueue("morgan-organized", RECORD, {"finished-source"}, ["copy.pdf"])
            self.assert_location(item.path.name, "pending")
            queue.hold("morgan-organized", {"bad-source"}, ["bad.pdf"], "Needs checking.")
            self.assert_location(item.path.name, "review")
            queue.enqueue("morgan-organized", RECORD, {"checked-source"}, ["checked.pdf"], resolve=True)
            self.assert_location(item.path.name, "pending")
            queue.begin_send(queue.pending_records()[0])
            queue.mark_uncertain(item, "Invented timeout.")
            self.assert_location(item.path.name, "review")
            self.assertEqual(queue.pending_records(), [])

    def test_manually_moving_sent_record_to_pending_restores_sent_location(self):
        with RecordQueue(self.queue_dir) as queue:
            item = self.add_record(queue)
            queue.begin_send(item)
            queue.finish_send(item, SUCCESS)
        (self.queue_dir / "sent" / item.path.name).rename(item.path)
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(queue.pending_records(), [])
            self.assertEqual(queue.status_for("morgan-organized"), "sent")
            self.assert_location(item.path.name, "sent")

    def test_flat_v1_migration_keeps_every_attempted_record_out_of_pending(self):
        filenames = self.seed_flat_v1(("pending", "review", "sending", "sent", "uncertain"))
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(len(queue.pending_records()), 1)
            for status, filename in filenames.items():
                expected = status if status in {"pending", "sent"} else "review"
                self.assert_location(filename, expected)
                self.assertEqual(queue.status_for(f"{status}-organized"),
                                 "uncertain" if status == "sending" else status)
                self.assertTrue(queue.known_sources(f"{status}-organized", {f"source-{status}"}))
        state = json.loads((self.queue_dir / ".queue-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["version"], 2)

    def test_partially_moved_legacy_queue_finishes_migration(self):
        filenames = self.seed_flat_v1(("pending", "sent"))
        for name in FOLDERS:
            (self.queue_dir / name).mkdir()
        filename = filenames["sent"]
        (self.queue_dir / filename).rename(self.queue_dir / "sent" / filename)
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(len(queue.pending_records()), 1)
            self.assert_location(filenames["pending"], "pending")
            self.assert_location(filenames["sent"], "sent")

    def test_committed_status_survives_interrupted_file_move(self):
        for interrupted_status in ("sending", "sent"):
            with self.subTest(status=interrupted_status):
                self.queue_dir = self.folder / f"interrupted-{interrupted_status}"
                with RecordQueue(self.queue_dir) as queue:
                    item = self.add_record(queue)
                    if interrupted_status == "sent":
                        queue.begin_send(item)
                    original_reconcile = queue._reconcile_records

                    def fail_after_durable_commit(*args, **kwargs):
                        state = json.loads((self.queue_dir / ".queue-state.json").read_text(encoding="utf-8"))
                        if state["records"][item.id]["status"] == interrupted_status:
                            raise OSError("Invented interruption after the history commit.")
                        return original_reconcile(*args, **kwargs)

                    with patch.object(queue, "_reconcile_records", side_effect=fail_after_durable_commit):
                        with self.assertRaises(OSError):
                            if interrupted_status == "sending":
                                queue.begin_send(item)
                            else:
                                queue.finish_send(item, SUCCESS)
                with RecordQueue(self.queue_dir) as queue:
                    self.assertEqual(queue.pending_records(), [])
                    expected = "review" if interrupted_status == "sending" else "sent"
                    self.assert_location(item.path.name, expected)
                    self.assertEqual(queue.status_for("morgan-organized"),
                                     "uncertain" if interrupted_status == "sending" else "sent")

    def test_unknown_json_in_each_status_folder_stops_queue(self):
        for folder in FOLDERS:
            with self.subTest(folder=folder):
                self.queue_dir = self.folder / f"unknown-{folder}"
                with RecordQueue(self.queue_dir) as queue:
                    self.add_record(queue)
                (self.queue_dir / folder / "untracked.json").write_text(json.dumps(RECORD), encoding="utf-8")
                with self.assertRaises(QueueError):
                    with RecordQueue(self.queue_dir):
                        pass

    def test_malformed_or_duplicate_key_record_stops_queue(self):
        for corruption in ('{"given_name":', '{"given_name":"One","given_name":"Two"}'):
            with self.subTest(corruption=corruption):
                self.queue_dir = self.folder / f"bad-{len(corruption)}"
                with RecordQueue(self.queue_dir) as queue:
                    item = self.add_record(queue)
                item.path.write_text(corruption, encoding="utf-8")
                with self.assertRaises(QueueError):
                    with RecordQueue(self.queue_dir):
                        pass

    def test_duplicate_record_copies_stop_before_reconciliation(self):
        with RecordQueue(self.queue_dir) as queue:
            item = self.add_record(queue)
        duplicate = self.queue_dir / "sent" / item.path.name
        duplicate.write_bytes(item.path.read_bytes())
        with self.assertRaises(QueueError):
            with RecordQueue(self.queue_dir):
                pass
        self.assertTrue(item.path.is_file())
        self.assertTrue(duplicate.is_file())

    def test_symlinked_records_and_status_folders_are_rejected(self):
        for kind in ("record", "folder"):
            with self.subTest(kind=kind):
                self.queue_dir = self.folder / f"symlink-{kind}"
                with RecordQueue(self.queue_dir) as queue:
                    item = self.add_record(queue)
                if kind == "record":
                    outside = self.folder / "outside-record.json"
                    item.path.rename(outside)
                    item.path.symlink_to(outside)
                else:
                    outside = self.folder / "outside-sent"
                    (self.queue_dir / "sent").rename(outside)
                    (self.queue_dir / "sent").symlink_to(outside, target_is_directory=True)
                with self.assertRaises(QueueError):
                    with RecordQueue(self.queue_dir):
                        pass

    def test_nested_folder_inside_status_folder_is_rejected(self):
        with RecordQueue(self.queue_dir) as queue:
            self.add_record(queue)
        (self.queue_dir / "pending" / "nested").mkdir()
        with self.assertRaises(QueueError):
            with RecordQueue(self.queue_dir):
                pass

    def test_managed_status_folder_cannot_become_an_independent_queue(self):
        with RecordQueue(self.queue_dir) as queue:
            self.add_record(queue)
        for folder in FOLDERS:
            with self.subTest(folder=folder):
                with self.assertRaises(QueueError):
                    with RecordQueue(self.queue_dir / folder):
                        pass
                self.assertFalse((self.queue_dir / folder / ".queue-state.json").exists())

    def test_repository_placeholder_layout_is_accepted(self):
        self.queue_dir.mkdir()
        placeholders = [self.queue_dir / ".gitkeep"]
        for folder in FOLDERS:
            (self.queue_dir / folder).mkdir()
            placeholders.append(self.queue_dir / folder / ".gitkeep")
        for path in placeholders:
            path.write_text("", encoding="utf-8")
        with RecordQueue(self.queue_dir) as queue:
            item = self.add_record(queue)
            self.assert_location(item.path.name, "pending")
        self.assertTrue(all(path.is_file() for path in placeholders))

    def test_precreated_default_layout_cannot_ignore_legacy_sending_history(self):
        default = self.queue_dir
        legacy = self.folder / "senders_pdfs"
        self.queue_dir = legacy
        filenames = self.seed_flat_v1(("sent",))
        old_history = (legacy / ".queue-state.json").read_bytes()
        for folder in FOLDERS:
            (default / folder).mkdir(parents=True)
            (default / folder / ".gitkeep").write_text("", encoding="utf-8")
        # Patch both constants: this regression never examines the real queue.
        with patch.object(record_queue, "DEFAULT_QUEUE", default), \
                patch.object(record_queue, "LEGACY_QUEUE", legacy):
            with self.assertRaises(QueueError):
                with RecordQueue(default):
                    pass
        self.assertFalse((default / ".queue-state.json").exists())
        self.assertEqual((legacy / ".queue-state.json").read_bytes(), old_history)
        self.assertTrue((legacy / filenames["sent"]).is_file())

    def test_placeholder_only_legacy_folder_does_not_block_new_default(self):
        legacy = self.folder / "senders_pdfs"
        legacy.mkdir()
        (legacy / ".gitkeep").write_text("", encoding="utf-8")
        with patch.object(record_queue, "DEFAULT_QUEUE", self.queue_dir), \
                patch.object(record_queue, "LEGACY_QUEUE", legacy):
            with RecordQueue(self.queue_dir) as queue:
                self.assertEqual(queue.pending_records(), [])
        self.assertTrue((legacy / ".gitkeep").is_file())


if __name__ == "__main__":
    unittest.main()
