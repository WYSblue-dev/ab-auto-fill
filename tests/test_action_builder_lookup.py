"""Exercise duplicate lookup with invented people and mocked GET requests only.

No test reads the user's .env, touches their queue, or contacts Action Builder.
The POST guard makes an accidental mutation fail immediately.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import os
import unittest
from unittest.mock import Mock, patch

import requests

import action_builder_lookup as lookup


PERSON_ID = "action_builder:11111111-1111-4111-8111-111111111111"
OTHER_ID = "action_builder:22222222-2222-4222-8222-222222222222"


def payload():
    """Use the actual sender's nested API shape with entirely invented data."""
    return {"person": {
        "given_name": "Morgan", "family_name": "Example", "additional_name": "A",
        "action_builder:entity_type": "Person",
        "email_addresses": [{"address": "morgan@example.test"}],
        "phone_numbers": [{"number": "12025550123"}],
        "postal_addresses": [{
            "address_lines": ["123 Sample Street"], "locality": "Example City",
            "region": "DC", "postal_code": "20001", "country": "US",
        }],
    }}


def remote(identifier=PERSON_ID, **changes):
    person = deepcopy(payload()["person"])
    person["identifiers"] = [identifier]
    person.update(changes)
    return person


def collection(people=(), *, page=1, total_pages=1, per_page=25, **extra):
    return {
        "page": page, "total_pages": total_pages, "per_page": per_page,
        "_embedded": {"osdi:people": deepcopy(list(people))}, **extra,
    }


def response(document, status=200):
    return Mock(status_code=status, json=Mock(return_value=deepcopy(document)))


class ConfigTests(unittest.TestCase):
    def test_config_validates_destination_and_does_not_expose_credentials(self):
        config = lookup.ActionBuilderConfig("invented-token", "Example-Org", "campaign_123")
        self.assertEqual(config.people_url,
                         "https://example-org.actionbuilder.org/api/rest/v1/campaigns/campaign_123/people")
        self.assertEqual(config.destination, {"subdomain": "example-org", "campaign_id": "campaign_123"})
        self.assertNotIn("invented-token", repr(config))
        self.assertNotIn("invented-token", str(config.destination))
        self.assertEqual(config.headers["OSDI-API-Token"], "invented-token")

    def test_invalid_config_cannot_redirect_credentials(self):
        configurations = [
            ("", "example", "campaign"), ("token with space", "example", "campaign"),
            ("token\n", "example", "campaign"), (None, "example", "campaign"),
            ("token", "outside.invalid/", "campaign"), ("token", "https://example", "campaign"),
            ("token", "example.actionbuilder.org", "campaign"), ("token", "-example", "campaign"),
            ("token", "example-", "campaign"), ("token", "a" * 64, "campaign"),
            ("token", "example", "../other"), ("token", "example", "campaign?x=1"),
            ("token", "example", "campaign#fragment"), ("token", "example", ""),
            ("your_actual_api_key", "example", "campaign"),
        ]
        for values in configurations:
            with self.subTest(values=values), self.assertRaises(lookup.LookupError):
                lookup.ActionBuilderConfig(*values)

    def test_environment_loader_is_explicit_and_mocked(self):
        fake_environment = {
            "ACTION_BUILDER_API_KEY": " invented-token ",
            "ACTION_BUILDER_SUBDOMAIN": " example ",
            "ACTION_BUILDER_CAMPAIGN_ID": " campaign ",
        }
        with patch.dict(os.environ, fake_environment, clear=True), patch.object(lookup, "load_dotenv") as load:
            config = lookup.ActionBuilderConfig.from_environment()
        load.assert_called_once_with()
        self.assertEqual(config.api_key, "invented-token")
        self.assertEqual(config.destination, {"subdomain": "example", "campaign_id": "campaign"})

    def test_missing_environment_values_fail_without_reading_real_dotenv(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(lookup, "load_dotenv"):
            with self.assertRaises(lookup.LookupError):
                lookup.ActionBuilderConfig.from_environment()


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.config = lookup.ActionBuilderConfig("invented-token", "example", "campaign")
        self.client = lookup.ActionBuilderLookup(self.config)
        self.person = payload()
        get_patch = patch.object(lookup.requests, "get", side_effect=AssertionError("Configure the mock GET result."))
        self.get = get_patch.start()
        self.addCleanup(get_patch.stop)
        post_patch = patch.object(lookup.requests, "post", side_effect=AssertionError("Lookup must never POST."))
        self.post = post_patch.start()
        self.addCleanup(post_patch.stop)
        self.addCleanup(self.post.assert_not_called)
        sleep_patch = patch.object(lookup.time, "sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def results_by_filter(self, **people_by_field):
        def respond(url, **kwargs):
            field = kwargs["params"]["filter"].split(" eq ", 1)[0]
            # Reproduce the observed API rejection of name filters.
            if field not in {"email_address", "phone_number"}:
                return response({"error": "Invalid filter"}, status=400)
            return response(collection(people_by_field.get(field, [])))
        self.get.side_effect = respond

    def test_both_empty_contact_searches_clear_submission_without_querying_names(self):
        self.results_by_filter()
        result = self.client.check(self.person)
        self.assertEqual(result.outcome, "not_found")
        self.assertEqual(result.candidates, ())
        for word in ("email", "phone", "no matches"):
            self.assertIn(word, result.reason.lower())
        self.assertEqual([call.kwargs["params"] for call in self.get.call_args_list], [
            {"filter": "email_address eq 'morgan@example.test'", "page": 1},
            {"filter": "phone_number eq '12025550123'", "page": 1},
        ])
        for call in self.get.call_args_list:
            self.assertEqual(call.args, (self.config.people_url,))
            self.assertEqual(call.kwargs["timeout"], (5, 30))
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["headers"]["OSDI-API-Token"], "invented-token")

    def test_exact_person_is_deduplicated_across_searches(self):
        person = remote()
        self.results_by_filter(email_address=[person], phone_number=[person])
        result = self.client.check(self.person)
        self.assertEqual(result.outcome, "existing")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0]["identifiers"], [PERSON_ID])
        self.assertEqual(result.candidates[0]["differing_fields"], [])

    def test_harmless_formatting_variations_still_match(self):
        person = remote(given_name="  MORGAN  ", family_name="example", additional_name="a.",
                        email_addresses=[{"address": "MORGAN@EXAMPLE.TEST"}],
                        phone_numbers=[{"number": "(202) 555-0123"}])
        person["postal_addresses"][0]["address_lines"] = ["  123   SAMPLE STREET "]
        self.results_by_filter(email_address=[person], phone_number=[person])
        self.assertEqual(self.client.check(self.person).outcome, "existing")

    def test_changed_address_or_middle_initial_needs_review(self):
        for changed_field in ("address_line_1", "additional_name"):
            with self.subTest(changed_field=changed_field):
                person = remote()
                if changed_field == "address_line_1":
                    person["postal_addresses"][0]["address_lines"] = ["999 Previous Street"]
                else:
                    person["additional_name"] = "B"
                self.results_by_filter(email_address=[person], phone_number=[person])
                result = self.client.check(self.person)
                self.assertEqual(result.outcome, "needs_review")
                self.assertIn(changed_field, result.candidates[0]["differing_fields"])

    def test_single_contact_match_with_changed_other_contact_needs_review(self):
        cases = [
            ("email_address", remote(phone_numbers=[{"number": "12025550199"}]), "phone"),
            ("phone_number", remote(email_addresses=[{"address": "previous@example.test"}]), "email"),
        ]
        for field, person, mismatch in cases:
            with self.subTest(field=field):
                self.results_by_filter(**{field: [person]})
                result = self.client.check(self.person)
                self.assertEqual(result.outcome, "needs_review")
                self.assertIn(mismatch, result.candidates[0]["differing_fields"])

    def test_email_and_phone_pointing_to_different_people_needs_review(self):
        email_person = remote(phone_numbers=[{"number": "12025550199"}])
        phone_person = remote(OTHER_ID, email_addresses=[{"address": "other@example.test"}])
        self.results_by_filter(email_address=[email_person], phone_number=[phone_person])
        result = self.client.check(self.person)
        self.assertEqual(result.outcome, "needs_review")
        self.assertEqual(len(result.candidates), 2)

    def test_multiple_exact_people_need_review(self):
        people = [remote(), remote(OTHER_ID)]
        self.results_by_filter(email_address=people, phone_number=people)
        self.assertEqual(self.client.check(self.person).outcome, "needs_review")

    def test_exact_match_succeeds_when_server_rejects_every_name_filter(self):
        person = remote()
        self.results_by_filter(email_address=[person], phone_number=[person])
        result = self.client.check(self.person)
        self.assertEqual(result.outcome, "existing")
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(result.candidates[0]["identifiers"], [PERSON_ID])

    def test_no_match_history_records_clearance_for_the_checked_destination(self):
        self.results_by_filter()
        result = self.client.check(self.person)
        self.assertEqual(result.outcome, "not_found")
        self.assertEqual(result.candidates, ())
        history = result.as_history(self.config)
        self.assertEqual(history["outcome"], "not_found")
        self.assertEqual(history["candidates"], [])
        self.assertEqual(history["destination"], self.config.destination)
        self.assertTrue(history["checked_at"])
        self.assertEqual(history["reason"], result.reason)

    def test_name_differences_are_compared_locally_for_contact_candidates(self):
        for field, changed_name in (("given_name", "Taylor"), ("family_name", "Different")):
            with self.subTest(field=field):
                person = remote(**{field: changed_name})
                self.results_by_filter(email_address=[person], phone_number=[person])
                result = self.client.check(self.person)
                self.assertEqual(result.outcome, "needs_review")
                self.assertIn(field, result.candidates[0]["differing_fields"])

    def test_entity_type_missing_or_not_person_needs_review(self):
        for entity_type in (None, "Employer"):
            with self.subTest(entity_type=entity_type):
                person = remote()
                if entity_type is None:
                    del person["action_builder:entity_type"]
                else:
                    person["action_builder:entity_type"] = entity_type
                self.results_by_filter(email_address=[person], phone_number=[person])
                result = self.client.check(self.person)
                self.assertEqual(result.outcome, "needs_review")
                self.assertIn("entity_type", result.candidates[0]["differing_fields"])

    def test_address_components_from_different_addresses_cannot_form_a_match(self):
        person = remote()
        other_address = deepcopy(person["postal_addresses"][0])
        person["postal_addresses"][0]["locality"] = "Other City"
        other_address["address_lines"] = ["999 Other Street"]
        person["postal_addresses"].append(other_address)
        self.results_by_filter(email_address=[person], phone_number=[person])
        self.assertEqual(self.client.check(self.person).outcome, "needs_review")

    def test_result_history_contains_matching_fields_but_not_contact_values_or_token(self):
        person = remote()
        self.results_by_filter(email_address=[person], phone_number=[person])
        history = self.client.check(self.person).as_history(self.config)
        self.assertEqual(history["destination"], self.config.destination)
        self.assertTrue(history["checked_at"])
        for sensitive_value in ("invented-token", "morgan@example.test", "12025550123", "123 Sample Street"):
            self.assertNotIn(sensitive_value, str(history))


    def test_failed_phone_search_after_email_match_does_not_return_existing(self):
        self.get.side_effect = [
            response(collection([remote()])),
            response({"error": "private-server-detail morgan@example.test 12025550123 invented-token"}, status=400),
        ]
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(lookup.LookupError) as raised:
            self.client.check(self.person)
        self.assertEqual(self.get.call_count, 2)
        message = str(raised.exception)
        self.assertIn("phone_number", message)
        self.assertIn("page 1", message)
        self.assertIn("HTTP 400", message)
        self.assertEqual(output.getvalue(), "")
        for value in ("morgan@example.test", "12025550123", "invented-token", "private-server-detail"):
            self.assertNotIn(value, message)

    def test_empty_email_search_cannot_clear_an_incomplete_phone_search(self):
        failures = [
            response({"error": "Invalid filter"}, status=400),
            requests.Timeout("simulated timeout"),
            response({"_embedded": {"osdi:people": []}}),
            response(collection(total_pages=2)),
        ]
        for failure in failures:
            with self.subTest(failure=repr(failure)):
                self.get.reset_mock()
                self.get.side_effect = [response(collection()), failure]
                with self.assertRaises(lookup.LookupError):
                    self.client.check(self.person)
                self.assertEqual([call.kwargs["params"]["filter"] for call in self.get.call_args_list], [
                    "email_address eq 'morgan@example.test'",
                    "phone_number eq '12025550123'",
                ])

    def test_candidate_changing_during_independent_searches_fails(self):
        changed = remote(additional_name="B")
        self.results_by_filter(email_address=[remote()], phone_number=[changed])
        with self.assertRaises(lookup.LookupError):
            self.client.check(self.person)

    def test_a_server_ignoring_the_filter_cannot_confirm_absence(self):
        self.results_by_filter(email_address=[remote(email_addresses=[{"address": "unrelated@example.test"}])])
        with self.assertRaises(lookup.LookupError):
            self.client.check(self.person)

    def test_all_pages_are_read_without_following_off_origin_next_links(self):
        self.get.side_effect = [
            response(collection([remote()], total_pages=2, per_page=1,
                                _links={"next": {"href": "https://outside.invalid/collect-token"}})),
            response(collection([remote(OTHER_ID)], page=2, total_pages=2, per_page=1)),
        ]
        people = self.client._search("email_address", "morgan@example.test")
        self.assertEqual([person["identifiers"][0] for person in people], [PERSON_ID, OTHER_ID])
        self.assertEqual([call.kwargs["params"]["page"] for call in self.get.call_args_list], [1, 2])
        self.assertTrue(all(call.args == (self.config.people_url,) for call in self.get.call_args_list))

    def test_zero_total_pages_is_valid_only_for_an_empty_first_page(self):
        self.get.side_effect = None
        self.get.return_value = response(collection(total_pages=0))
        self.assertEqual(self.client._search("email_address", "morgan@example.test"), [])
        self.get.return_value = response(collection([remote()], total_pages=0))
        with self.assertRaises(lookup.LookupError):
            self.client._search("email_address", "morgan@example.test")

    def test_missing_or_invalid_pagination_cannot_confirm_absence(self):
        invalid_documents = []
        for name in ("page", "total_pages", "per_page"):
            document = collection()
            del document[name]
            invalid_documents.append(document)
        for name, value in (("page", 2), ("page", True), ("total_pages", True),
                            ("total_pages", -1), ("total_pages", 101), ("per_page", 0),
                            ("per_page", 26), ("per_page", True)):
            invalid_documents.append({**collection(), name: value})
        self.get.side_effect = None
        for document in invalid_documents:
            with self.subTest(document=document):
                self.get.return_value = response(document)
                with self.assertRaises(lookup.LookupError):
                    self.client._search("email_address", "morgan@example.test")

    def test_changing_page_count_fails(self):
        self.get.side_effect = [response(collection([remote()], total_pages=2, per_page=1)),
                                response(collection([remote(OTHER_ID)], page=2, total_pages=3, per_page=1))]
        with self.assertRaisesRegex(lookup.LookupError, "changed during pagination"):
            self.client._search("email_address", "morgan@example.test")
        self.assertEqual(self.get.call_count, 2)

    def test_empty_nonfinal_page_fails(self):
        self.get.side_effect = [response(collection(total_pages=2))]
        with self.assertRaises(lookup.LookupError):
            self.client._search("email_address", "morgan@example.test")

    def test_short_nonfinal_page_fails(self):
        self.get.side_effect = [response(collection([remote()], total_pages=2, per_page=25))]
        with self.assertRaises(lookup.LookupError):
            self.client._search("email_address", "morgan@example.test")

    def test_empty_final_page_after_nonempty_first_page_fails(self):
        self.get.side_effect = [response(collection([remote()], total_pages=2, per_page=1)),
                                response(collection(page=2, total_pages=2, per_page=1))]
        with self.assertRaisesRegex(lookup.LookupError, "incomplete pages"):
            self.client._search("email_address", "morgan@example.test")
        self.assertEqual(self.get.call_count, 2)

    def test_changing_page_size_fails(self):
        self.get.side_effect = [response(collection([remote()], total_pages=2, per_page=1)),
                                response(collection([remote(OTHER_ID)], page=2, total_pages=2, per_page=2))]
        with self.assertRaisesRegex(lookup.LookupError, "changed its page size"):
            self.client._search("email_address", "morgan@example.test")
        self.assertEqual(self.get.call_count, 2)

    def test_repeated_page_or_person_fails(self):
        cases = [
            collection([remote(OTHER_ID)], page=1, total_pages=2, per_page=1),
            collection([remote()], page=2, total_pages=2, per_page=1),
        ]
        for second in cases:
            with self.subTest(second=second):
                self.get.reset_mock()
                self.get.side_effect = [response(collection([remote()], total_pages=2, per_page=1)), response(second)]
                with self.assertRaises(lookup.LookupError):
                    self.client._search("email_address", "morgan@example.test")
                self.assertEqual(self.get.call_count, 2)

    def test_next_link_after_declared_final_page_fails(self):
        self.get.side_effect = [response(collection(_links={"next": {"href": "https://example.invalid/page2"}}))]
        with self.assertRaises(lookup.LookupError):
            self.client._search("email_address", "morgan@example.test")

    def test_malformed_collections_fail(self):
        documents = [[], None, {}, {**collection(), "_embedded": []},
                     {**collection(), "_embedded": {"osdi:people": None}},
                     collection([remote(), remote(OTHER_ID)], per_page=1),
                     collection(_links=[])]
        self.get.side_effect = None
        for document in documents:
            with self.subTest(document=document):
                self.get.return_value = response(document)
                with self.assertRaises(lookup.LookupError):
                    self.client._search("email_address", "morgan@example.test")

    def test_malformed_people_fail_before_any_absence_decision(self):
        records = [None, [], {}, remote(identifiers=[]), remote(identifiers=["custom_id:123"]),
                   remote(identifiers=[PERSON_ID, OTHER_ID]), remote(identifiers=[None]),
                   remote(identifiers=["action_builder:not-a-uuid"]), remote(given_name=None),
                   remote(email_addresses={}), remote(email_addresses=[None]),
                   remote(email_addresses=[{"address": None}]),
                   remote(phone_numbers=[{"number": 12025550123}]), remote(postal_addresses="address"),
                   remote(postal_addresses=[{"address_lines": "street"}]),
                   remote(postal_addresses=[{"address_lines": [None]}]),
                   remote(postal_addresses=[{"region": None}])]
        self.get.side_effect = None
        for person in records:
            with self.subTest(person=person):
                self.get.return_value = response(collection([person]))
                with self.assertRaises(lookup.LookupError):
                    self.client._search("email_address", "morgan@example.test")

    def test_http_errors_and_redirects_never_become_not_found(self):
        self.get.side_effect = None
        for status in (201, 204, 301, 302, 307, 400, 401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                self.get.return_value = response(collection(), status=status)
                with self.assertRaises(lookup.LookupError):
                    self.client.check(self.person)

    def test_connection_errors_are_safe_and_do_not_expose_request_values(self):
        for error in (requests.Timeout("morgan@example.test invented-token"),
                      requests.ConnectionError("morgan@example.test invented-token")):
            with self.subTest(error=type(error).__name__):
                self.get.side_effect = error
                with self.assertRaises(lookup.LookupError) as raised:
                    self.client.check(self.person)
                self.assertNotIn("morgan@example.test", str(raised.exception))
                self.assertNotIn("invented-token", str(raised.exception))

    def test_invalid_json_does_not_become_not_found(self):
        invalid = response(None)
        invalid.json.side_effect = ValueError("Invalid body")
        self.get.side_effect = [invalid]
        with self.assertRaises(lookup.LookupError):
            self.client.check(self.person)

    def test_apostrophes_are_escaped_in_email_and_names_are_not_queried(self):
        self.person["person"]["family_name"] = "O'Example"
        self.person["person"]["email_addresses"][0]["address"] = "morgan.o'example@example.test"
        self.results_by_filter()
        self.assertEqual(self.client.check(self.person).outcome, "not_found")
        filters = [call.kwargs["params"]["filter"] for call in self.get.call_args_list]
        self.assertEqual(filters, [
            "email_address eq 'morgan.o''example@example.test'",
            "phone_number eq '12025550123'",
        ])

    def test_incomplete_local_person_is_rejected_before_any_get(self):
        string_street_lines = payload()
        string_street_lines["person"]["postal_addresses"][0]["address_lines"] = "123 Sample Street"
        inputs = [None, {}, {"person": None}, {"person": {}},
                  {"person": {**self.person["person"], "email_addresses": []}},
                  {"person": {**self.person["person"], "email_addresses": [{"address": "invalid"}]}},
                  {"person": {**self.person["person"], "phone_numbers": [{"number": "1800LETTERS"}]}},
                  {"person": {**self.person["person"], "phone_numbers": [{"number": ""}]}},
                  {"person": {**self.person["person"], "given_name": " "}},
                  {"person": {**self.person["person"], "postal_addresses": []}},
                  string_street_lines]
        for input_person in inputs:
            with self.subTest(input_person=input_person), self.assertRaises(lookup.LookupError):
                self.client.check(input_person)
        self.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
