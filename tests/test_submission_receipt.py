"""A successful HTTP status must include a usable person receipt."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
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
from test_action_builder_lookup import collection, response


CONFIG = ActionBuilderConfig("invented-token", "invented", "invented-campaign")
PERSON_ID = "action_builder:11111111-1111-4111-8111-111111111111"


class SubmissionReceiptTests(unittest.TestCase):
    def member_batch_with_remote_street(self, local_street, remote_street):
        """Run real lookup/submission/member verification against an invented API."""
        config = ActionBuilderConfig('invented-token', 'invented', 'invented-campaign', '1105')
        record = {**RECORD, 'address_line_1': local_street, 'classification': 'CE/CW'}
        remote = deepcopy(send_person.build_actionbuilder_payload(record)['person'])
        remote['postal_addresses'][0]['address_lines'] = [remote_street]
        remote['identifiers'] = [PERSON_ID]
        remote['action_builder:latest_assessment'] = 1
        tags = [{'action_builder:section': 'Fourth District Workers',
                 'action_builder:field': field, 'name': name, 'action_builder:field_type': 'standard'}
                for field, name in (('Classification - 4D', 'CE/CW'),
                                    ('Local Jurisdiction by Zip (Residence) - 4D', '1105'))]
        person_url = config.people_url + '/' + PERSON_ID.partition(':')[2]
        posted = []
        calls = []

        def get(url, **kwargs):
            calls.append(url)
            if url == config.people_url:
                # The two original searches see no entry; the fallback sees the creation.
                return response(collection([remote] if posted else [], total_pages=1 if posted else 0))
            if url == config.people_url.removesuffix('/people') + '/tags':
                expression = kwargs['params']['filter']
                return response({'page': 1, 'per_page': 25, 'total_pages': 1,
                                 '_embedded': {'osdi:tags': [tag for tag in tags if expression == "name eq '" + tag['name'] + "'"]}})
            if url == person_url:
                return response(remote)
            if url == person_url + '/taggings':
                return response({'page': 1, 'per_page': 25, 'total_pages': 1,
                                 '_embedded': {'osdi:taggings': tags}})
            raise AssertionError('Unexpected mock GET route')

        def post(url, **kwargs):
            self.assertEqual(url, config.people_url)
            posted.append(deepcopy(kwargs['json']))
            return response({'unexpected_envelope': True}, status=201)

        with tempfile.TemporaryDirectory() as directory:
            queue_dir = Path(directory) / 'queue'
            with RecordQueue(queue_dir) as queue:
                queue.enqueue('example', record, {'invented-source'}, ['invented.pdf'])
            with (patch.object(ActionBuilderConfig, 'from_environment', return_value=config),
                  patch.object(send_person.requests, 'get', side_effect=get),
                  patch.object(send_person.requests, 'post', side_effect=post) as create,
                  patch.object(send_person.time, 'sleep'),
                  patch('sys.argv', ['send_person.py', '--queue-dir', str(queue_dir), '--submit']),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                exit_code = send_person.main()
                with RecordQueue(queue_dir) as queue:
                    row = queue.snapshot()['records'][0]
                self.assertEqual(send_person.main(), 0)
                self.assertEqual(create.call_count, 1)
        return exit_code, row, posted[0], calls, person_url

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

    def test_member_batch_unfamiliar_receipt_accepts_suffix_only_street_difference(self):
        status, row, payload, calls, person_url = self.member_batch_with_remote_street(
            '123 Sample Street', '123 Sample St.')
        self.assertEqual(status, 0)
        self.assertEqual(row['status'], 'sent')
        self.assertEqual(row['result']['identifiers'], [PERSON_ID])
        self.assertEqual(payload['person']['postal_addresses'][0]['address_lines'], ['123 Sample Street'])
        self.assertEqual(payload['person']['action_builder:latest_assessment'], 1)
        self.assertEqual(len(payload['add_tags']), 2)
        self.assertEqual(calls.count(person_url.rsplit('/', 1)[0]), 4)  # Two prechecks and two receipt-fallback searches.
        self.assertIn(person_url, calls)
        self.assertIn(person_url + '/taggings', calls)

    def test_member_batch_still_holds_different_house_or_unit_after_one_post(self):
        for local_street, remote_street in (('123 Sample Street', '124 Sample St.'),
                                            ('123 Sample Street Apt 2', '123 Sample St. Apt 3')):
            with self.subTest(local_street=local_street, remote_street=remote_street):
                status, row, payload, calls, person_url = self.member_batch_with_remote_street(local_street, remote_street)
                self.assertEqual(status, 1)
                self.assertEqual(row['status'], 'uncertain')
                self.assertNotIn('result', row)
                self.assertEqual(payload['person']['postal_addresses'][0]['address_lines'], [local_street])
                self.assertNotIn(person_url, calls)  # Non-exact contact fallback never reaches member verification.


if __name__ == "__main__":
    unittest.main()
