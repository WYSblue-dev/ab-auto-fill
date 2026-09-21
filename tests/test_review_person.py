"""Exercise manual review using invented records, temporary queues, and mocked HTTP."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from action_builder_lookup import LookupError
from record_queue import RecordQueue, STATE_NAME
import review_person
import test_lookup_queue as fixture


RECORD = fixture.RECORD
CONFIG = fixture.CONFIG
CLEAR = fixture.CLEAR
EXISTING = fixture.EXISTING
SUCCESS = fixture.sender_fixture.SUCCESS


class ReviewPersonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.queue_dir = Path(self.temporary.name) / "composed_info"

    def seed_review(self, records=(RECORD,)):
        with RecordQueue(self.queue_dir) as queue:
            for number, record in enumerate(records):
                family = f"person-{number}-organized"
                queue.enqueue(family, record, {f"source-{number}"}, [f"person-{number}.pdf"])
                item = next(item for item in queue.pending_records() if item.family == family)
                queue.record_lookup(item, EXISTING.as_history(CONFIG))
            return queue.review_records()

    def state(self):
        return json.loads((self.queue_dir / STATE_NAME).read_text(encoding="utf-8"))

    def seed_uncertain(self):
        with RecordQueue(self.queue_dir) as queue:
            queue.enqueue("uncertain-person", RECORD, {"one"}, ["one.pdf"])
            item = queue.pending_records()[0]
            queue.begin_send(item)
            queue.mark_uncertain(item, "An invented earlier send timed out.")
        return item

    def snapshot(self):
        return {str(path.relative_to(self.queue_dir)): path.read_bytes()
                for path in self.queue_dir.rglob("*") if path.is_file()}

    def run_cli(self, answers=(), *, lookup_result=CLEAR, lookup_effect=None,
                submit_effect=None, observe_input=None, post_response=None):
        output, errors = io.StringIO(), io.StringIO()
        answer_iterator = iter(answers)
        self.prompts = []

        def answer(prompt):
            self.prompts.append(prompt)
            print(prompt)
            try:
                value = next(answer_iterator)
            except StopIteration:
                self.fail(f"Unexpected input prompt: {prompt}")
            if observe_input is not None:
                observe_input(prompt, value)
            if isinstance(value, BaseException):
                raise value
            return value

        if post_response is not None:
            submit_effect = review_person.submit_to_actionbuilder
        # Patch credentials too: these tests must never read a real .env file.
        with (patch("sys.argv", ["review_person.py", "--queue-dir", str(self.queue_dir)]),
                patch("builtins.input", side_effect=answer),
                patch.object(review_person.ActionBuilderConfig, "from_environment", return_value=CONFIG) as config,
                patch.object(review_person.ActionBuilderLookup, "check", return_value=lookup_result,
                             side_effect=lookup_effect) as lookup,
                patch("requests.get", side_effect=AssertionError("Real GET forbidden")),
                patch("requests.post", return_value=post_response,
                      side_effect=None if post_response is not None else AssertionError("Real POST forbidden")),
                patch.object(review_person, "submit_to_actionbuilder", return_value=SUCCESS,
                             side_effect=submit_effect) as submit,
                patch("time.sleep"),
                redirect_stdout(output), redirect_stderr(errors)):
            status = review_person.main()
        self.last_config, self.last_lookup, self.last_submit = config, lookup, submit
        return status, output.getvalue(), errors.getvalue()

    def assert_no_network(self):
        self.last_config.assert_not_called()
        self.last_lookup.assert_not_called()
        self.last_submit.assert_not_called()

    def test_empty_queue_finishes_without_credentials_or_questions(self):
        status, _, errors = self.run_cli()
        self.assertEqual(status, 0, errors)
        self.assertEqual(self.prompts, [])
        self.assert_no_network()

    def test_keep_displays_person_sources_reason_and_candidates_without_changes(self):
        self.seed_review()
        before = self.snapshot()
        status, output, errors = self.run_cli(["n", "k"])
        self.assertEqual(status, 0, errors)
        for value in RECORD.values():
            self.assertIn(value, output)
        self.assertIn("person-0.pdf", output)
        self.assertIn(EXISTING.reason, output)
        self.assertIn(fixture.IDENTIFIER, output)
        self.assertEqual(self.snapshot(), before)
        self.assert_no_network()

    def test_records_appear_one_by_one_and_blank_action_keeps_them(self):
        self.seed_review((RECORD, fixture.sender_fixture.OTHER_RECORD))
        before = self.snapshot()
        status, output, errors = self.run_cli(["", "", "", ""])
        self.assertEqual(status, 0, errors)
        self.assertLess(output.index("Morgan"), output.index("Taylor"))
        self.assertEqual(self.snapshot(), before)
        self.assert_no_network()

    def test_eof_or_keyboard_interrupt_never_deletes_or_sends(self):
        self.seed_review()
        before = self.snapshot()
        for interruption in (EOFError(), KeyboardInterrupt()):
            with self.subTest(interruption=type(interruption).__name__):
                status, _, _ = self.run_cli([interruption])
                self.assertEqual(status, 130)
                self.assertEqual(self.snapshot(), before)
                self.assert_no_network()

    def test_unchecked_person_cannot_be_sent(self):
        self.seed_review()
        before = self.snapshot()
        status, _, _ = self.run_cli(["n", "s"])
        self.assertEqual(status, 0)
        self.assertEqual(self.snapshot(), before)
        self.assert_no_network()

    def test_unchecked_person_cannot_be_discarded(self):
        self.seed_review()
        before = self.snapshot()
        self.run_cli(["n", "d"])
        self.assertEqual(self.snapshot(), before)
        self.assert_no_network()

    def test_discard_requires_confirmation_then_suppresses_repeat_import(self):
        item = self.seed_review()[0]
        before = self.snapshot()
        self.run_cli(["y", "d", "n"])
        self.assertEqual(self.snapshot(), before)
        self.assert_no_network()
        status, _, errors = self.run_cli(["y", "d", "y"])
        self.assertEqual(status, 0, errors)
        self.assertFalse(item.path.exists())
        entry = self.state()["records"][item.id]
        self.assertEqual(entry["status"], "discarded")
        self.assertEqual(entry["decisions"][-1]["action"], "discarded")
        with RecordQueue(self.queue_dir) as queue:
            self.assertEqual(queue.review_records(), [])
            queue.enqueue(item.family, RECORD, {"source-0"}, ["person-0.pdf"], resolve=True)
            queue.enqueue("different-filename", RECORD, {"source-new"}, ["new.pdf"], resolve=True)
            self.assertEqual(queue.pending_records(), [])
            self.assertEqual(queue.review_records(), [])
        self.assert_no_network()

    def test_edit_shows_old_values_normalizes_saves_and_redisplays_without_sending(self):
        original = self.seed_review()[0]
        answers = ["y", "c", "4", "UPDATED@EXAMPLE.TEST", "5", "(202) 555-0199",
                   "8", "ma", "done", "n"]
        status, output, errors = self.run_cli(answers)
        self.assertEqual(status, 0, errors)
        self.assertIn(RECORD["email"], output)
        self.assertIn(RECORD["phone"], output)
        expected = {**RECORD, "email": "updated@example.test", "phone": "12025550199"}
        with RecordQueue(self.queue_dir) as queue:
            updated = queue.review_records()[0]
            self.assertEqual(json.loads(updated.path.read_text()), expected)
            self.assertEqual(queue.pending_records(), [])
        canonical = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(updated.id, hashlib.sha256(canonical).hexdigest())
        self.assertNotEqual(updated.id, original.id)
        self.assertIn("updated@example.test", output)
        self.assertIn("12025550199", output)
        self.assertEqual(self.state()["records"][updated.id]["status"], "review")
        self.assert_no_network()

    def test_invalid_edit_reprompts_and_never_saves_bad_phone(self):
        self.seed_review()
        status, output, errors = self.run_cli(["y", "c", "5", "bad-phone", "(202) 555-0199", "done", "n"])
        self.assertEqual(status, 0, errors)
        with RecordQueue(self.queue_dir) as queue:
            record = json.loads(queue.review_records()[0].path.read_text())
        self.assertEqual(record["phone"], "12025550199")
        self.assertGreaterEqual(len(self.prompts), 7)
        self.assertIn("phone", output.lower())
        self.assert_no_network()

    def test_edit_then_send_uses_revised_payload_for_lookup_and_post(self):
        self.seed_review()
        status, output, errors = self.run_cli(["y", "c", "5", "(202) 555-0199", "done", "y", "y", "y"])
        self.assertEqual(status, 0, errors)
        self.assertIn("12025550199", output)
        checked = self.last_lookup.call_args.args[0]
        posted = self.last_submit.call_args.args[0]
        self.assertEqual(checked, posted)
        self.assertEqual(posted["person"]["phone_numbers"][0]["number"], "12025550199")
        self.assertEqual(len(list((self.queue_dir / "sent").glob("*.json"))), 1)

    def test_clear_send_checks_then_confirms_then_claims_before_post(self):
        item = self.seed_review()[0]
        events = []
        real_claim = RecordQueue.begin_review_send

        def lookup(payload):
            self.assertEqual(payload["person"]["given_name"], RECORD["given_name"])
            events.append("lookup")
            return CLEAR

        def answer(prompt, value):
            if "create" in prompt.lower() and "?" in prompt:
                self.assertEqual(self.state()["records"][item.id]["status"], "review")
                events.append("confirm")

        def claim(queue, selected, **kwargs):
            self.assertEqual(events[-1], "confirm")
            real_claim(queue, selected, **kwargs)
            events.append("claim")

        def submit(payload, *, config):
            self.assertEqual(events[-1], "claim")
            self.assertIs(config, CONFIG)
            saved = self.state()["records"][item.id]
            self.assertEqual(saved["status"], "sending")
            self.assertEqual(saved["decisions"][-1]["action"], "approved_send")
            events.append("post")
            return SUCCESS

        with patch.object(RecordQueue, "begin_review_send", autospec=True, side_effect=claim):
            status, _, errors = self.run_cli(["y", "s", "y"],
                                             lookup_effect=lookup, submit_effect=submit, observe_input=answer)
        self.assertEqual(status, 0, errors)
        self.assertEqual(events, ["lookup", "confirm", "claim", "post"])
        self.assertEqual(self.state()["records"][item.id]["status"], "sent")
        self.assertTrue((self.queue_dir / "sent" / item.path.name).is_file())
        self.last_submit.assert_called_once()

    def test_declining_final_send_keeps_record_in_review(self):
        item = self.seed_review()[0]
        with patch.object(RecordQueue, "begin_review_send", autospec=True) as claim:
            status, _, errors = self.run_cli(["y", "s", "n"])
        self.assertEqual(status, 0, errors)
        self.last_lookup.assert_called_once()
        self.last_submit.assert_not_called()
        claim.assert_not_called()
        self.assertEqual(self.state()["records"][item.id]["status"], "review")

    def test_eof_at_final_confirmation_does_not_claim_or_submit(self):
        item = self.seed_review()[0]
        with patch.object(RecordQueue, "begin_review_send", autospec=True) as claim:
            status, _, _ = self.run_cli(["y", "s", EOFError()])
        self.assertEqual(status, 130)
        claim.assert_not_called()
        self.last_submit.assert_not_called()
        self.assertEqual(self.state()["records"][item.id]["status"], "review")

    def test_candidates_require_explicit_override_and_reason(self):
        item = self.seed_review()[0]
        for answers in (["y", "s", "n"], ["y", "s", "y", ""]):
            with self.subTest(answers=answers):
                with patch.object(RecordQueue, "begin_review_send", autospec=True) as claim:
                    self.run_cli(answers, lookup_result=EXISTING)
                claim.assert_not_called()
                self.last_submit.assert_not_called()
                self.assertEqual(self.state()["records"][item.id]["status"], "review")
        status, _, errors = self.run_cli(["y", "s", "y", "Confirmed different people sharing a contact.", "y"],
                                         lookup_result=EXISTING)
        self.assertEqual(status, 0, errors)
        self.last_submit.assert_called_once()
        entry = self.state()["records"][item.id]
        self.assertEqual(entry["status"], "sent")
        self.assertIn("Confirmed different people", json.dumps(entry["decisions"]))

    def test_lookup_failure_never_claims_or_submits(self):
        item = self.seed_review()[0]
        with patch.object(RecordQueue, "begin_review_send", autospec=True) as claim:
            status, _, _ = self.run_cli(["y", "s"], lookup_effect=LookupError("Invented GET failed."))
        self.assertNotEqual(status, 0)
        claim.assert_not_called()
        self.last_submit.assert_not_called()
        self.assertEqual(self.state()["records"][item.id]["status"], "review")

    def test_ambiguous_post_stops_batch_and_never_retries_automatically(self):
        items = self.seed_review((RECORD, fixture.sender_fixture.OTHER_RECORD))
        status, _, _ = self.run_cli(["y", "s", "y"],
                                    submit_effect=TimeoutError("Invented response timeout."))
        self.assertNotEqual(status, 0)
        self.last_submit.assert_called_once()
        state = self.state()["records"]
        self.assertEqual(state[items[0].id]["status"], "uncertain")
        self.assertEqual(state[items[1].id]["status"], "review")
        self.run_cli(["n", "k", "n", "k"])
        self.assert_no_network()
        self.assertEqual(self.state()["records"][items[0].id]["status"], "uncertain")

    def test_http_success_without_person_receipt_becomes_uncertain(self):
        item = self.seed_review()[0]
        response = Mock(status_code=200)
        response.json.return_value = {}
        status, _, _ = self.run_cli(["y", "s", "y"], post_response=response)
        self.assertNotEqual(status, 0)
        self.last_submit.assert_called_once()
        self.assertEqual(self.state()["records"][item.id]["status"], "uncertain")
        self.assertEqual(list((self.queue_dir / "sent").glob("*.json")), [])

    def test_uncertain_retry_requires_prior_attempt_check_and_reason(self):
        item = self.seed_uncertain()
        for answers in (["y", "s", "n"], ["y", "s", "y", ""]):
            with self.subTest(answers=answers):
                with patch.object(RecordQueue, "begin_review_send", autospec=True) as claim:
                    self.run_cli(answers)
                self.last_lookup.assert_called_once()
                self.last_submit.assert_not_called()
                claim.assert_not_called()
                self.assertEqual(self.state()["records"][item.id]["status"], "uncertain")
        status, _, errors = self.run_cli(["y", "s", "y", "Verified the earlier attempt created nobody.", "y"])
        self.assertEqual(status, 0, errors)
        self.last_submit.assert_called_once()
        self.assertEqual(self.state()["records"][item.id]["status"], "sent")

    def test_uncertain_candidate_requires_both_independent_confirmations(self):
        item = self.seed_uncertain()
        self.run_cli(["y", "s", "y", "n"], lookup_result=EXISTING)
        self.last_submit.assert_not_called()
        self.assertEqual(self.state()["records"][item.id]["status"], "uncertain")
        status, _, errors = self.run_cli(["y", "s", "y", "y", "Earlier attempt failed; candidates are different people.", "y"],
                                         lookup_result=EXISTING)
        self.assertEqual(status, 0, errors)
        self.last_submit.assert_called_once()
        self.assertEqual(self.state()["records"][item.id]["status"], "sent")

    def test_review_related_to_sent_person_cannot_create_another(self):
        with RecordQueue(self.queue_dir) as queue:
            queue.enqueue("sent-person", RECORD, {"one"}, ["one.pdf"])
            sent = queue.pending_records()[0]
            queue.begin_send(sent)
            queue.finish_send(sent, SUCCESS)
            queue.enqueue("sent-person", {**RECORD, "phone": "12025550199"}, {"two"}, ["two.pdf"])
        status, output, _ = self.run_cli(["y", "s"])
        self.assertEqual(status, 0)
        self.assertIn("sent", output.lower())
        self.assert_no_network()
        self.assertEqual(self.state()["records"][sent.id]["status"], "sent")


if __name__ == "__main__":
    unittest.main()
