"""Check durable queue transitions and submission ordering without a real API.

Only invented records are written, always in temporary folders. Every sender
test replaces the HTTP submission function before it calls the real CLI entry.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from record_queue import QueueError, RecordQueue
import send_person
import test_extract_person as fixture


RECORD = fixture.EXPECTED
OTHER_RECORD = {**RECORD, "given_name": "Taylor", "email": "taylor@example.test"}
SUCCESS = {"person": {"identifiers": ["invented:person-id"]}}


class RecordQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.queue_dir = Path(self.temporary.name) / "queue"

    @staticmethod
    def enqueue(queue, record=None, *, family="morgan-organized", source="source-one", resolve=False):
        return queue.enqueue(family, record or RECORD, {source}, ["invented.pdf"], resolve=resolve)

    def test_repeated_source_and_same_record_new_source_are_deduplicated(self):
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(self.enqueue(queue), "queued")
            self.assertEqual(self.enqueue(queue), "unchanged")
            self.assertEqual(self.enqueue(queue, source="source-two"), "unchanged")
            self.assertEqual(len(queue.pending_records()), 1)
            self.assertTrue(queue.known_sources("morgan-organized", {"source-one", "source-two"}))

    def test_record_deduplication_survives_a_new_queue_instance(self):
        with RecordQueue(self.queue_dir) as queue:
            self.enqueue(queue)
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(self.enqueue(queue, source="source-two"), "unchanged")
            self.assertEqual(len(queue.pending_records()), 1)

    def test_changed_pending_record_requires_explicit_resolution(self):
        revised = {**RECORD, "phone": "12025550199"}
        with RecordQueue(self.queue_dir) as queue:
            self.enqueue(queue)
            self.assertEqual(self.enqueue(queue, revised, source="source-two"), "review")
            self.assertEqual(queue.pending_records(), [])
            self.assertEqual(self.enqueue(queue, revised, source="source-two", resolve=True), "queued")
            item = queue.pending_records()[0]
            self.assertEqual(json.loads(item.path.read_text(encoding="utf-8")), revised)

    def test_hold_revokes_pending_until_explicit_resolution(self):
        with RecordQueue(self.queue_dir) as queue:
            self.enqueue(queue)
            queue.hold("morgan-organized", {"source-two"}, ["copy.pdf"], "Copy could not be parsed.")
            self.assertEqual(queue.status_for("morgan-organized"), "review")
            self.assertEqual(queue.pending_records(), [])
            self.assertEqual(self.enqueue(queue), "review")
            self.assertEqual(self.enqueue(queue, resolve=True), "queued")

    def test_deferral_can_restore_unchanged_record_after_download_finishes(self):
        with RecordQueue(self.queue_dir) as queue:
            self.enqueue(queue)
            queue.defer("morgan-organized", ["copy.pdf.part"], "Download incomplete.")
            self.assertEqual(queue.status_for("morgan-organized"), "deferred")
            self.assertFalse(queue.known_sources("morgan-organized", {"source-one"}))
            self.assertEqual(queue.pending_records(), [])
            self.assertEqual(self.enqueue(queue, source="source-two"), "queued")
            self.assertEqual(len(queue.pending_records()), 1)

    def test_resolution_cannot_queue_changed_details_after_a_send_attempt(self):
        with RecordQueue(self.queue_dir) as queue:
            self.enqueue(queue)
            queue.begin_send(queue.pending_records()[0])
            revised = {**RECORD, "phone": "12025550199"}
            self.assertEqual(self.enqueue(queue, revised, source="source-two", resolve=True), "review")
            self.assertEqual(queue.pending_records(), [])

    def test_sending_and_sent_records_are_never_pending_again(self):
        with RecordQueue(self.queue_dir) as queue:
            self.enqueue(queue)
            item = queue.pending_records()[0]
            queue.begin_send(item)
            self.assertEqual(queue.pending_records(), [])
            with self.assertRaises(QueueError):
                queue.item_for_path(item.path)
            queue.finish_send(item, SUCCESS)
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(queue.pending_records(), [])
            self.enqueue(queue, source="source-two")
            self.assertEqual(queue.pending_records(), [])

    def test_uncertain_outcome_is_not_automatically_retried(self):
        with RecordQueue(self.queue_dir) as queue:
            self.enqueue(queue)
            item = queue.pending_records()[0]
            queue.begin_send(item)
            queue.mark_uncertain(item, "Invented timeout after sending.")
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(queue.pending_records(), [])
            self.enqueue(queue, source="source-two")
            self.assertEqual(queue.pending_records(), [])
            with self.assertRaises(QueueError):
                queue.item_for_path(item.path)

    def test_interrupted_send_remains_blocked_after_reopening(self):
        with RecordQueue(self.queue_dir) as queue:
            self.enqueue(queue)
            item = queue.pending_records()[0]
            # This deliberately simulates a crash before either result method.
            queue.begin_send(item)
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(queue.pending_records(), [])
            with self.assertRaises(QueueError):
                queue.item_for_path(item.path)

    def test_second_queue_cannot_open_an_active_queue(self):
        with RecordQueue(self.queue_dir):
            with self.assertRaises(QueueError):
                with RecordQueue(self.queue_dir):
                    self.fail("The second queue must not acquire the active lock.")
        # A rejected second opener must not delete the first opener's lock.
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(queue.pending_records(), [])

    def test_untracked_json_file_requires_review(self):
        with RecordQueue(self.queue_dir) as queue:
            self.enqueue(queue)
        (self.queue_dir / "untracked.json").write_text(json.dumps(RECORD), encoding="utf-8")
        with self.assertRaises(QueueError):
            with RecordQueue(self.queue_dir) as queue:
                queue.pending_records()

    def test_changed_record_bytes_do_not_bypass_manifest(self):
        with RecordQueue(self.queue_dir) as queue:
            self.enqueue(queue)
            path = queue.pending_records()[0].path
        path.write_text(json.dumps(OTHER_RECORD), encoding="utf-8")
        with self.assertRaises(QueueError):
            with RecordQueue(self.queue_dir) as queue:
                queue.pending_records()


class QueuedSenderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.queue_dir = self.folder / "queue"

    def seed_queue(self, records):
        with RecordQueue(self.queue_dir) as queue:
            for number, record in enumerate(records):
                queue.enqueue(f"person-{number}-organized", record,
                              {f"source-{number}"}, [f"person-{number}.pdf"])
            return list(queue.pending_records())

    def run_sender(self, *arguments, submit_effect=None, credential_error=None, sleep_effect=None):
        output = io.StringIO()
        errors = io.StringIO()
        argv = ["send_person.py", *map(str, arguments), "--queue-dir", str(self.queue_dir)]
        # Mock credentials as well as transport so the real .env is not read.
        with patch("sys.argv", argv), patch.object(send_person, "load_dotenv"), \
                patch.object(send_person, "require_environment_variable", return_value="invented",
                             side_effect=credential_error), \
                patch.object(send_person.requests, "post", side_effect=AssertionError("Real HTTP is forbidden in tests")), \
                patch.object(send_person, "submit_to_actionbuilder", side_effect=submit_effect,
                             return_value=SUCCESS) as submit, \
                patch.object(send_person.time, "sleep", side_effect=sleep_effect), \
                redirect_stdout(output), redirect_stderr(errors):
            result = send_person.main()
        return result, submit, output.getvalue(), errors.getvalue()

    def test_default_mode_previews_without_submitting(self):
        self.seed_queue([RECORD, OTHER_RECORD])
        status, submit, _, _ = self.run_sender()
        self.assertEqual(status, 0)
        submit.assert_not_called()
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(len(queue.pending_records()), 2)

    def test_submit_sends_each_pending_record_once(self):
        self.seed_queue([RECORD, OTHER_RECORD])
        status, submit, _, errors = self.run_sender("--submit")
        self.assertEqual(status, 0, errors)
        self.assertEqual(submit.call_count, 2)
        submitted = {call.args[0]["person"]["given_name"] for call in submit.call_args_list}
        self.assertEqual(submitted, {"Morgan", "Taylor"})
        status, submit, _, _ = self.run_sender("--submit")
        self.assertEqual(status, 0)
        submit.assert_not_called()

    def test_each_record_is_claimed_before_its_post(self):
        self.seed_queue([RECORD, OTHER_RECORD])
        events = []
        original_begin_send = RecordQueue.begin_send

        def claim(queue, item):
            original_begin_send(queue, item)
            events.append("claimed")

        def post(payload):
            self.assertEqual(events[-1], "claimed")
            events.append("posted")
            return SUCCESS

        with patch.object(RecordQueue, "begin_send", autospec=True, side_effect=claim):
            status, submit, _, errors = self.run_sender("--submit", submit_effect=post)
        self.assertEqual(status, 0, errors)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(events, ["claimed", "posted", "claimed", "posted"])

    def test_batch_pauses_between_posts_but_not_before_first_or_during_preview(self):
        self.seed_queue([RECORD, OTHER_RECORD])
        events = []

        def pause(seconds):
            events.append(("sleep", seconds))

        def post(payload):
            events.append(("post",))
            return SUCCESS

        status, submit, _, errors = self.run_sender(sleep_effect=pause)
        self.assertEqual(status, 0, errors)
        submit.assert_not_called()
        self.assertEqual(events, [])

        status, submit, _, errors = self.run_sender("--submit", submit_effect=post, sleep_effect=pause)
        self.assertEqual(status, 0, errors)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(events, [("post",), ("sleep", 0.3), ("post",)])

    def test_submission_failure_stops_batch_and_blocks_that_record(self):
        self.seed_queue([RECORD, OTHER_RECORD])
        status, submit, _, _ = self.run_sender("--submit", submit_effect=TimeoutError("Invented timeout"))
        self.assertNotEqual(status, 0)
        self.assertEqual(submit.call_count, 1)
        failed_name = submit.call_args.args[0]["person"]["given_name"]
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(len(queue.pending_records()), 1)
        status, submit, _, errors = self.run_sender("--submit")
        self.assertEqual(status, 0, errors)
        self.assertEqual(submit.call_count, 1)
        self.assertNotEqual(submit.call_args.args[0]["person"]["given_name"], failed_name)

    def test_entire_batch_is_validated_before_first_submission(self):
        items = self.seed_queue([RECORD, OTHER_RECORD])
        # Corrupt the final queued file. A valid earlier record must not be sent.
        items[-1].path.write_text('{"given_name": "Incomplete"}', encoding="utf-8")
        status, submit, _, _ = self.run_sender("--submit")
        self.assertNotEqual(status, 0)
        submit.assert_not_called()

    def test_all_payloads_are_validated_before_claiming_first_record(self):
        # This is valid queue JSON, but its phone cannot form a valid API payload.
        invalid_phone = {**OTHER_RECORD, "phone": "not-digits"}
        self.seed_queue([RECORD, invalid_phone])
        with patch.object(RecordQueue, "begin_send", autospec=True) as claim:
            status, submit, _, _ = self.run_sender("--submit")
        self.assertNotEqual(status, 0)
        submit.assert_not_called()
        claim.assert_not_called()
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(len(queue.pending_records()), 2)

    def test_missing_credentials_leave_entire_batch_pending(self):
        self.seed_queue([RECORD, OTHER_RECORD])
        with patch.object(RecordQueue, "begin_send", autospec=True) as claim:
            status, submit, _, _ = self.run_sender(
                "--submit", credential_error=RuntimeError("Invented missing credential.")
            )
        self.assertNotEqual(status, 0)
        submit.assert_not_called()
        claim.assert_not_called()
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(len(queue.pending_records()), 2)

    def test_explicit_managed_path_cannot_resend_sent_record(self):
        items = self.seed_queue([RECORD])
        self.assertEqual(self.run_sender("--submit")[0], 0)
        status, submit, _, _ = self.run_sender(items[0].path, "--submit")
        self.assertNotEqual(status, 0)
        submit.assert_not_called()

    def test_legacy_explicit_json_still_previews(self):
        path = self.folder / "legacy.json"
        path.write_text(json.dumps(RECORD), encoding="utf-8")
        status, submit, _, errors = self.run_sender(path)
        self.assertEqual(status, 0, errors)
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
