"""Use invented people and mocked GETs to verify the existing-person gate.

These tests never read real credentials, use the real queue, or contact an API.
"""

from contextlib import redirect_stderr, redirect_stdout
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
# Both supported searches completed without finding a candidate.
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
        with (patch.object(send_person.ActionBuilderConfig, "from_environment", return_value=CONFIG),
                patch.object(send_person.ActionBuilderLookup, "check", return_value=result) as lookup,
                patch.object(send_person.requests, "get", side_effect=AssertionError("No real GET allowed")),
                patch.object(send_person.requests, "post", side_effect=AssertionError("No real POST allowed")),
                patch.object(send_person, "submit_to_actionbuilder") as submit,
                redirect_stdout(io.StringIO())):
            status = send_person.check_queue(self.queue_dir)
        submit.assert_not_called()
        return status, lookup

    def test_preview_never_performs_lookup(self):
        self.seed_queue([RECORD])
        status, submit, _, _ = self.run_sender()
        self.assertEqual(status, 0)
        self.last_lookup.assert_not_called()
        submit.assert_not_called()

    def test_clearance_stays_pending_but_is_checked_again_before_post(self):
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

    def run_real_precheck(self, *, remote=None, check_only=False, standalone=None,
                          allow_submit=False, phone_status=200):
        # Use the real lookup and sender together; replace only their external dependencies.
        collection = {
            "page": 1, "total_pages": 1 if remote is not None else 0, "per_page": 25,
            "_embedded": {"osdi:people": [remote] if remote is not None else []},
        }
        output = io.StringIO()
        errors = io.StringIO()
        events = []
        real_claim = RecordQueue.begin_send

        def get(url, **kwargs):
            self.assertEqual(url, CONFIG.people_url)
            self.assertEqual(kwargs["headers"], CONFIG.headers)
            self.assertFalse(kwargs["allow_redirects"])
            field = kwargs["params"]["filter"].partition(" eq ")[0]
            self.assertIn(field, ("email_address", "phone_number"))
            expected = RECORD["email" if field == "email_address" else "phone"]
            self.assertEqual(kwargs["params"], {"filter": f"{field} eq '{expected}'", "page": 1})
            events.append(f"GET {field}")
            response = Mock(status_code=phone_status if field == "phone_number" else 200)
            response.json.return_value = collection
            return response

        def claim(queue, item):
            self.assertTrue(allow_submit, "No send claim is permitted in this case")
            self.assertEqual(events[-2:], ["GET email_address", "GET phone_number"])
            real_claim(queue, item)
            events.append("claim")

        def post(url, **kwargs):
            self.assertTrue(allow_submit, "POST is forbidden in this case")
            self.assertEqual(url, CONFIG.people_url)
            self.assertEqual(kwargs["headers"], CONFIG.headers)
            self.assertEqual(kwargs["json"], send_person.build_actionbuilder_payload(RECORD))
            self.assertFalse(kwargs["allow_redirects"])
            if standalone is None:
                self.assertEqual(events[-1], "claim")
            events.append("POST")
            response = Mock(status_code=201)
            response.json.return_value = sender_fixture.SUCCESS
            return response

        with (
            patch.object(send_person, "load_dotenv"),
            patch.object(send_person.ActionBuilderConfig, "from_environment", return_value=CONFIG),
            patch.object(send_person.requests, "get", side_effect=get),
            patch.object(send_person.requests, "post", side_effect=post),
            patch.object(RecordQueue, "begin_send", autospec=True, side_effect=claim),
            patch.object(send_person.time, "sleep"),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            if check_only:
                status = send_person.check_queue(self.queue_dir)
            else:
                argv = ["send_person.py", "--queue-dir", str(self.queue_dir), "--submit"]
                if standalone is not None:
                    argv.append(str(standalone))
                with patch("sys.argv", argv):
                    status = send_person.main()
        return status, output.getvalue() + errors.getvalue(), events

    def test_real_lookup_and_sender_contract_with_mocked_http(self):
        for kind in ("existing", "ambiguous"):
            for check_only in (False, True):
                with self.subTest(kind=kind, check_only=check_only):
                    self.queue_dir = self.folder / f"http-{kind}-{check_only}"
                    item = self.seed_queue([RECORD])[0]
                    remote = {**send_person.build_actionbuilder_payload(RECORD)["person"], "identifiers": [IDENTIFIER]}
                    if kind == "ambiguous":
                        remote["additional_name"] = "Z"
                    status, output, events = self.run_real_precheck(remote=remote, check_only=check_only)
                    self.assertEqual(status, 1, output)
                    self.assertEqual(events, ["GET email_address", "GET phone_number"])
                    state = json.loads((self.queue_dir / STATE_NAME).read_text())
                    receipt = state["records"][item.id]["lookup"]
                    self.assertEqual(receipt["destination"], CONFIG.destination)
                    self.assertEqual(state["records"][item.id]["status"], "review")
                    self.assertTrue((self.queue_dir / "review" / item.path.name).is_file())
                    self.assertNotIn(CONFIG.api_key, json.dumps(state))
                    self.assertEqual(receipt["outcome"], "existing" if kind == "existing" else "needs_review")
                    self.assertEqual(receipt["candidates"][0]["identifiers"], [IDENTIFIER])
                    if kind == "ambiguous":
                        self.assertIn("additional_name", receipt["candidates"][0]["differing_fields"])
                    # Later submissions skip review records completely.
                    status, output, events = self.run_real_precheck()
                    self.assertEqual(status, 0, output)
                    self.assertEqual(events, [])

    def test_historical_empty_search_hold_survives_reopen_resolution_and_alias(self):
        item = self.seed_queue([RECORD])[0]
        historical_hold = LookupResult("needs_review", "Earlier policy required manual absence review.")
        self.assertEqual(self.run_check(historical_hold)[0], 1)
        changed = {**RECORD, "phone": "12025550199"}
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(queue.pending_records(), [])
            with self.assertRaises(QueueError):
                queue.begin_send(item)
            for family, record in ((item.family, RECORD), (item.family, changed), ("renamed", RECORD)):
                with self.subTest(family=family, phone=record["phone"]):
                    outcome = queue.enqueue(family, record, {"later-source"}, ["later.pdf"], resolve=True)
                    self.assertEqual(outcome, "review")
                    self.assertEqual(queue.pending_records(), [])
            with self.assertRaises(QueueError):
                queue.record_lookup(item, CLEAR.as_history(CONFIG))
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(queue.pending_records(), [])
            self.assertEqual(queue.status_for("renamed"), "review")

    def test_real_empty_search_clears_then_submits_once_after_fresh_gets(self):
        item = self.seed_queue([RECORD])[0]
        status, submit, output, _ = self.run_sender()
        self.assertEqual(status, 0, output)
        self.last_lookup.assert_not_called()
        submit.assert_not_called()

        status, output, events = self.run_real_precheck(check_only=True)
        self.assertEqual(status, 0, output)
        self.assertEqual(events, ["GET email_address", "GET phone_number"])
        state = json.loads((self.queue_dir / STATE_NAME).read_text())
        entry = state["records"][item.id]
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["lookup"]["outcome"], "not_found")
        self.assertEqual(entry["lookup"]["candidates"], [])
        self.assertEqual(entry["lookup"]["destination"], CONFIG.destination)
        self.assertNotIn(CONFIG.api_key, json.dumps(state))
        self.assertTrue(item.path.is_file())

        status, output, events = self.run_real_precheck(allow_submit=True)
        self.assertEqual(status, 0, output)
        self.assertEqual(events, ["GET email_address", "GET phone_number", "claim", "POST"])
        state = json.loads((self.queue_dir / STATE_NAME).read_text())
        self.assertEqual(state["records"][item.id]["status"], "sent")
        self.assertTrue((self.queue_dir / "sent" / item.path.name).is_file())
        self.assertFalse(item.path.exists())

        status, output, events = self.run_real_precheck()
        self.assertEqual(status, 0, output)
        self.assertEqual(events, [])

    def test_real_submit_holds_new_match_after_successful_clearance(self):
        item = self.seed_queue([RECORD])[0]
        status, output, _ = self.run_real_precheck(check_only=True)
        self.assertEqual(status, 0, output)
        remote = {**send_person.build_actionbuilder_payload(RECORD)["person"], "identifiers": [IDENTIFIER]}
        status, output, events = self.run_real_precheck(remote=remote)
        self.assertEqual(status, 1, output)
        self.assertEqual(events, ["GET email_address", "GET phone_number"])
        state = json.loads((self.queue_dir / STATE_NAME).read_text())
        self.assertEqual(state["records"][item.id]["status"], "review")
        self.assertEqual(state["records"][item.id]["lookup"]["outcome"], "existing")

    def test_real_second_search_failure_blocks_even_after_earlier_clearance(self):
        for previously_checked in (False, True):
            with self.subTest(previously_checked=previously_checked):
                self.queue_dir = self.folder / f"failed-phone-{previously_checked}"
                item = self.seed_queue([RECORD])[0]
                if previously_checked:
                    self.assertEqual(self.run_real_precheck(check_only=True)[0], 0)
                status, output, events = self.run_real_precheck(phone_status=400)
                self.assertEqual(status, 1, output)
                self.assertEqual(events, ["GET email_address", "GET phone_number"])
                state = json.loads((self.queue_dir / STATE_NAME).read_text())
                entry = state["records"][item.id]
                self.assertEqual(entry["status"], "pending")
                if previously_checked:
                    self.assertEqual(entry["lookup"]["outcome"], "not_found")
                else:
                    self.assertNotIn("lookup", entry)
                self.assertIn("HTTP 400", output)

    def test_real_empty_search_allows_standalone_submit(self):
        path = self.folder / "standalone.json"
        path.write_text(json.dumps(RECORD), encoding="utf-8")
        status, output, events = self.run_real_precheck(standalone=path, allow_submit=True)
        self.assertEqual(status, 0, output)
        self.assertEqual(events, ["GET email_address", "GET phone_number", "POST"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), RECORD)
        self.assertFalse(self.queue_dir.exists())



if __name__ == "__main__":
    unittest.main()
