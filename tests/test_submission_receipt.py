"""A successful HTTP status must include a usable person receipt."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from action_builder_lookup import ActionBuilderConfig, LookupError, LookupResult
from record_queue import RecordQueue, STATE_NAME
import send_person
from send_person import submit_to_actionbuilder
from test_send_queue import RECORD
from test_lookup_queue import EXISTING, AMBIGUOUS


CONFIG = ActionBuilderConfig("invented-token", "invented", "invented-campaign")
PERSON_ID = "action_builder:11111111-1111-4111-8111-111111111111"


class SubmissionReceiptTests(unittest.TestCase):
    def submit(self, document):
        response = Mock(status_code=201)
        response.json.return_value = document
        with patch("send_person.requests.post", return_value=response) as post, patch("send_person.time.sleep"):
            result = submit_to_actionbuilder({"person": {"given_name": "Example"}}, config=CONFIG)
        self.assertEqual(post.call_count, 1)
        return result

    def test_native_person_receipt_is_accepted(self):
        receipt = {"person": {"identifiers": [PERSON_ID, "custom_id:invented"]}}
        self.assertEqual(self.submit(receipt), receipt)

    def test_empty_error_or_missing_person_is_not_a_success(self):
        for receipt in ({}, {"error": "Rejected"}, {"person": None},
                        {"person": []}, {"person": {}},
                        {"error": "Rejected", "person": {"identifiers": [PERSON_ID]}}):
            with self.subTest(receipt=receipt):
                with self.assertRaises(RuntimeError):
                    self.submit(receipt)

    def test_missing_malformed_or_ambiguous_ids_are_not_a_success(self):
        for identifiers in (None, PERSON_ID, [], [None], [""], ["custom_id:invented"],
                            ["action_builder:bad-id"], [PERSON_ID, PERSON_ID]):
            with self.subTest(identifiers=identifiers):
                with self.assertRaises(RuntimeError):
                    self.submit({"person": {"identifiers": identifiers}})

    def test_unexpected_receipt_diagnostic_shows_structure_without_private_values(self):
        receipt = {"identifiers": [PERSON_ID], "given_name": "Private Name",
                   "error": "private-token and private@example.test", "private-key-name": "secret"}
        with self.assertRaises(send_person.SubmissionReceiptError) as caught:
            self.submit(receipt)
        message = str(caught.exception)
        self.assertIn("HTTP 201", message)
        self.assertIn("person=missing", message)
        self.assertIn("top-level identifiers=list(1)", message)
        self.assertIn("error=str", message)
        for private in (PERSON_ID, "Private Name", "private-token", "private@example.test", "private-key-name", "secret"):
            self.assertNotIn(private, message)

    def test_diagnostic_distinguishes_empty_error_field_from_missing_person(self):
        with self.assertRaises(send_person.SubmissionReceiptError) as caught:
            self.submit({"person": {"identifiers": [PERSON_ID]}, "errors": []})
        self.assertIn("person.identifiers=list(1)", str(caught.exception))
        self.assertIn("errors=list(0)", str(caught.exception))

    def test_bad_success_receipt_holds_normal_submission_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            queue_dir = Path(directory) / "queue"
            with RecordQueue(queue_dir) as queue:
                queue.enqueue("example", RECORD, {"invented-source"}, ["invented.pdf"])
                item = queue.pending_records()[0]
            response = Mock(status_code=200)
            response.json.return_value = {}
            with (
                patch.object(ActionBuilderConfig, "from_environment", return_value=CONFIG),
                patch.object(send_person.ActionBuilderLookup, "check", return_value=LookupResult("not_found", "Invented complete searches.")),
                patch.object(send_person.requests, "post", return_value=response) as post,
                patch.object(send_person.time, "sleep"),
                patch("sys.argv", ["send_person.py", "--queue-dir", str(queue_dir), "--submit"]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(send_person.main(), 1)
                state = json.loads((queue_dir / STATE_NAME).read_text())
                self.assertEqual(state["records"][item.id]["status"], "uncertain")
                self.assertTrue((queue_dir / "review" / item.path.name).exists())
                self.assertEqual(send_person.main(), 0)
                self.assertEqual(post.call_count, 1)

    def test_unfamiliar_receipt_is_confirmed_by_read_only_exact_match(self):
        response = Mock(status_code=200)
        response.json.return_value = {"unexpected": "Private remote value"}
        payload = send_person.build_actionbuilder_payload(RECORD)
        with (patch("send_person.requests.post", return_value=response) as post,
              patch.object(send_person.ActionBuilderLookup, "check", return_value=EXISTING) as lookup,
              patch("send_person.time.sleep")):
            receipt = submit_to_actionbuilder(payload, config=CONFIG)
        post.assert_called_once()
        lookup.assert_called_once_with(payload)
        self.assertTrue(receipt["confirmed_by_lookup"])
        self.assertEqual(receipt["person"]["identifiers"], EXISTING.candidates[0]["identifiers"])
        self.assertNotIn("Private remote value", str(receipt))

    def test_fallback_never_reposts_or_accepts_missing_differing_or_failed_lookup(self):
        response = Mock(status_code=200)
        response.json.return_value = {}
        for outcome in (LookupResult("not_found", "No match"), AMBIGUOUS, LookupError("Incomplete search")):
            with self.subTest(outcome=outcome):
                with (patch("send_person.requests.post", return_value=response) as post,
                      patch.object(send_person.ActionBuilderLookup, "check",
                                   side_effect=outcome if isinstance(outcome, Exception) else None,
                                   return_value=outcome),
                      patch("send_person.time.sleep")):
                    with self.assertRaises(send_person.SubmissionReceiptError):
                        submit_to_actionbuilder(send_person.build_actionbuilder_payload(RECORD), config=CONFIG)
                    post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
