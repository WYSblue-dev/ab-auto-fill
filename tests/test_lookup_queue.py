"""Use invented people and mocked GETs to verify the existing-person gate.

These tests never read real credentials, use the real queue, or contact an API.
"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from action_builder_lookup import LookupError, LookupResult
from record_queue import QueueError, RecordQueue, STATE_NAME
import send_person
import test_send_queue as sender_fixture


RECORD = sender_fixture.RECORD
CONFIG = sender_fixture.CONFIG
CLEAR = sender_fixture.CLEAR
IDENTIFIER = "action_builder:11111111-1111-4111-8111-111111111111"
EXISTING = LookupResult("existing", "This person appears to exist already.", ({
    "identifiers": [IDENTIFIER], "matching_fields": sorted(RECORD), "differing_fields": [],
},))
AMBIGUOUS = LookupResult("needs_review", "A matching person has different details.", ({
    "identifiers": [IDENTIFIER], "matching_fields": ["email"], "differing_fields": ["phone"],
},))


class LookupQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.queue_dir = self.folder / "composed_info"

    seed_queue = sender_fixture.QueuedSenderTests.seed_queue
    run_sender = sender_fixture.QueuedSenderTests.run_sender

    def run_check(self, result=CLEAR):
        # Guard both HTTP verbs even though the lookup itself is mocked.
        with patch.object(send_person.ActionBuilderConfig, "from_environment", return_value=CONFIG), \
                patch.object(send_person.ActionBuilderLookup, "check", return_value=result) as lookup, \
                patch.object(send_person.requests, "get", side_effect=AssertionError("No real GET allowed")), \
                patch.object(send_person.requests, "post", side_effect=AssertionError("No real POST allowed")), \
                patch.object(send_person, "submit_to_actionbuilder") as submit, \
                redirect_stdout(io.StringIO()):
            status = send_person.check_queue(self.queue_dir)
        submit.assert_not_called()
        return status, lookup

    def test_preview_never_performs_lookup(self):
        self.seed_queue([RECORD])
        status, submit, _, _ = self.run_sender()
        self.assertEqual(status, 0)
        self.last_lookup.assert_not_called()
        submit.assert_not_called()

    def test_get_only_no_match_stays_pending_and_is_checked_again_before_post(self):
        item = self.seed_queue([RECORD])[0]
        status, lookup = self.run_check()
        self.assertEqual(status, 0)
        self.assertEqual(lookup.call_count, 1)
        state = json.loads((self.queue_dir / STATE_NAME).read_text())
        self.assertEqual(state["records"][item.id]["status"], "pending")
        self.assertEqual(state["records"][item.id]["lookup"]["outcome"], "not_found")
        self.assertNotIn(CONFIG.api_key, json.dumps(state))
        # Someone appeared in Action Builder after the standalone check.
        status, submit, _, _ = self.run_sender("--submit", lookup_effect=[EXISTING])
        self.assertEqual(status, 1)
        self.assertEqual(self.last_lookup.call_count, 1)
        submit.assert_not_called()
        self.assertTrue((self.queue_dir / "review" / item.path.name).is_file())

    def test_existing_and_ambiguous_people_are_held_without_post(self):
        for result in (EXISTING, AMBIGUOUS):
            with self.subTest(outcome=result.outcome):
                self.queue_dir = self.folder / result.outcome
                item = self.seed_queue([RECORD])[0]
                status, submit, output, _ = self.run_sender("--submit", lookup_effect=[result])
                self.assertEqual(status, 1)
                submit.assert_not_called()
                self.assertIn(IDENTIFIER, output)
                if result is AMBIGUOUS:
                    self.assertIn("Fields to review: phone", output)
                with RecordQueue(self.queue_dir) as queue:
                    self.assertEqual(queue.pending_records(), [])
                    self.assertEqual(queue.status_for(item.family), "review")
                state = json.loads((self.queue_dir / STATE_NAME).read_text())
                receipt = state["records"][item.id]["lookup"]
                self.assertEqual(receipt["outcome"], result.outcome)
                self.assertEqual(receipt["destination"], CONFIG.destination)
                self.assertEqual(receipt["candidates"][0]["identifiers"], [IDENTIFIER])

    def test_get_failure_leaves_record_pending_without_send_claim(self):
        item = self.seed_queue([RECORD])[0]
        with patch.object(RecordQueue, "begin_send", autospec=True) as claim:
            status, submit, _, _ = self.run_sender(
                "--submit", lookup_effect=LookupError("Invented incomplete search.")
            )
        self.assertEqual(status, 1)
        submit.assert_not_called()
        claim.assert_not_called()
        state = json.loads((self.queue_dir / STATE_NAME).read_text())
        self.assertEqual(state["records"][item.id]["status"], "pending")
        self.assertNotIn("lookup", state["records"][item.id])

    def test_each_lookup_precedes_its_claim_and_post(self):
        self.seed_queue([RECORD, sender_fixture.OTHER_RECORD])
        events = []
        real_claim = RecordQueue.begin_send

        def lookup(payload):
            events.append("lookup")
            return CLEAR

        def claim(queue, item):
            self.assertEqual(events[-1], "lookup")
            real_claim(queue, item)
            events.append("claim")

        def post(payload, *, config):
            self.assertEqual(events[-1], "claim")
            self.assertIs(config, CONFIG)
            events.append("post")
            return sender_fixture.SUCCESS

        with patch.object(RecordQueue, "begin_send", autospec=True, side_effect=claim):
            status, submit, _, errors = self.run_sender(
                "--submit", lookup_effect=lookup, submit_effect=post,
            )
        self.assertEqual(status, 0, errors)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(events, ["lookup", "claim", "post", "lookup", "claim", "post"])

    def test_legacy_json_cannot_bypass_existing_person_lookup(self):
        path = self.folder / "legacy.json"
        path.write_text(json.dumps(RECORD))
        status, submit, _, _ = self.run_sender(path, "--submit", lookup_effect=[EXISTING])
        self.assertEqual(status, 1)
        submit.assert_not_called()
        self.last_lookup.assert_called_once()
        self.assertTrue(path.is_file())

    def test_legacy_no_match_uses_checked_configuration_for_post(self):
        path = self.folder / "legacy.json"
        path.write_text(json.dumps(RECORD))
        status, submit, _, errors = self.run_sender(path, "--submit")
        self.assertEqual(status, 0, errors)
        self.assertIs(submit.call_args.kwargs["config"], CONFIG)
        self.last_lookup.assert_called_once()

    def test_hold_survives_reopen_resolution_changes_and_new_filename(self):
        item = self.seed_queue([RECORD])[0]
        self.assertEqual(self.run_check(EXISTING)[0], 1)
        changed = {**RECORD, "phone": "12025550199"}
        with RecordQueue(self.queue_dir) as queue:
            for family, record in ((item.family, RECORD), (item.family, changed),
                                   ("another-name", RECORD)):
                with self.subTest(family=family, phone=record["phone"]):
                    outcome = queue.enqueue(family, record, {"new-source"}, ["new.pdf"], resolve=True)
                    self.assertEqual(outcome, "review")
                    self.assertEqual(queue.pending_records(), [])

    def test_new_history_link_holds_other_family_current_version(self):
        second = {**RECORD, "phone": "12025550198"}
        third = {**RECORD, "phone": "12025550199"}
        with RecordQueue(self.queue_dir) as queue:
            queue.enqueue("held", RECORD, {"one"}, ["one.pdf"])
            queue.record_lookup(queue.pending_records()[0], EXISTING.as_history(CONFIG))
            queue.enqueue("other", second, {"two"}, ["two.pdf"])
            queue.enqueue("other", third, {"three"}, ["three.pdf"], resolve=True)
            self.assertEqual(len(queue.pending_records()), 1)
            # This links the held group to a historical version of 'other'.
            queue.enqueue("held", second, {"four"}, ["four.pdf"], resolve=True)
            self.assertEqual(queue.pending_records(), [])
            self.assertEqual(queue.status_for("other"), "review")
            fourth = {**RECORD, "phone": "12025550197"}
            self.assertEqual(queue.enqueue("other", fourth, {"five"}, ["five.pdf"], resolve=True), "review")
            self.assertEqual(queue.pending_records(), [])

    def test_hold_revokes_later_related_item_in_prepared_batch(self):
        changed = {**RECORD, "phone": "12025550199"}
        with RecordQueue(self.queue_dir) as queue:
            queue.enqueue("first", RECORD, {"one"}, ["one.pdf"])
            queue.enqueue("second", RECORD, {"one"}, ["one.pdf"])
            queue.enqueue("second", changed, {"two"}, ["two.pdf"], resolve=True)
            queue.enqueue("first", RECORD, {"one"}, ["one.pdf"], resolve=True)
            self.assertEqual(len(queue.pending_records()), 2)
        status, submit, _, errors = self.run_sender("--submit", lookup_effect=[EXISTING])
        self.assertEqual(status, 1, errors)
        submit.assert_not_called()
        self.assertEqual(self.last_lookup.call_count, 1)
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(queue.pending_records(), [])

    def test_bad_lookup_receipt_cannot_be_saved_or_release_hold(self):
        item = self.seed_queue([RECORD])[0]
        bad_receipts = [
            {**CLEAR.as_history(CONFIG), "outcome": []},
            {**CLEAR.as_history(CONFIG), "outcome": "existing"},
            {**CLEAR.as_history(CONFIG), "candidates": list(EXISTING.candidates)},
            {**AMBIGUOUS.as_history(CONFIG), "outcome": "existing"},
            {**CLEAR.as_history(CONFIG), "api_key": "should-not-be-stored"},
        ]
        with RecordQueue(self.queue_dir) as queue:
            for receipt in bad_receipts:
                with self.subTest(receipt=receipt):
                    with self.assertRaises(QueueError):
                        queue.record_lookup(item, receipt)
            self.assertEqual(len(queue.pending_records()), 1)
            queue.record_lookup(item, EXISTING.as_history(CONFIG))
            with self.assertRaises(QueueError):
                queue.record_lookup(item, CLEAR.as_history(CONFIG))

    def test_real_lookup_and_sender_contract_with_mocked_http(self):
        for existing, check_only in ((False, False), (True, False), (True, True)):
            with self.subTest(existing=existing, check_only=check_only):
                self.queue_dir = self.folder / f"http-{existing}-{check_only}"
                item = self.seed_queue([RECORD])[0]
                events = []
                remote = {**send_person.build_actionbuilder_payload(RECORD)["person"],
                          "identifiers": [IDENTIFIER]}
                collection = {
                    "page": 1, "total_pages": 1 if existing else 0, "per_page": 25,
                    "_embedded": {"osdi:people": [remote] if existing else []},
                }

                def get(url, **kwargs):
                    self.assertEqual(url, CONFIG.people_url)
                    self.assertEqual(kwargs["headers"], CONFIG.headers)
                    self.assertFalse(kwargs["allow_redirects"])
                    events.append("get")
                    response = Mock(status_code=200)
                    response.json.return_value = collection
                    return response

                def post(url, **kwargs):
                    self.assertEqual(url, CONFIG.people_url)
                    self.assertEqual(kwargs["headers"], CONFIG.headers)
                    # Verify the real claim was durable before HTTP POST.
                    state = json.loads((self.queue_dir / STATE_NAME).read_text())
                    self.assertEqual(state["records"][item.id]["status"], "sending")
                    self.assertEqual(state["records"][item.id]["lookup"]["outcome"], "not_found")
                    events.append("post")
                    response = Mock(status_code=201)
                    response.json.return_value = sender_fixture.SUCCESS
                    return response

                # The real config loader is replaced; no .env is read.
                with patch.object(send_person.ActionBuilderConfig, "from_environment", return_value=CONFIG) as settings, \
                        patch.object(send_person.requests, "get", side_effect=get) as get_mock, \
                        patch.object(send_person.requests, "post", side_effect=post) as post_mock, \
                        patch.object(send_person.time, "sleep"), redirect_stdout(io.StringIO()):
                    status = send_person.send_queue(self.queue_dir, None, not check_only,
                                                    check_only=check_only)
                settings.assert_called_once()
                self.assertEqual(get_mock.call_count, 3)
                state = json.loads((self.queue_dir / STATE_NAME).read_text())
                receipt = state["records"][item.id]["lookup"]
                self.assertEqual(receipt["destination"], CONFIG.destination)
                if existing:
                    self.assertEqual(status, 1)
                    post_mock.assert_not_called()
                    self.assertEqual(receipt["outcome"], "existing")
                    self.assertEqual(receipt["candidates"][0]["identifiers"], [IDENTIFIER])
                    self.assertEqual(state["records"][item.id]["status"], "review")
                else:
                    self.assertEqual(status, 0)
                    self.assertEqual(events, ["get", "get", "get", "post"])
                    self.assertEqual(state["records"][item.id]["status"], "sent")


if __name__ == "__main__":
    unittest.main()
