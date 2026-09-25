"""Exercise bounded GET retries and person verification without remote calls."""

from copy import deepcopy
import unittest
from unittest.mock import Mock, call, patch

import requests

import action_builder_http as http
from action_builder_lookup import ActionBuilderConfig, ActionBuilderLookup, LookupError
from member_automation import (
    CLASSIFICATION_FIELD, JURISDICTION_FIELD, SECTION,
    MemberAutomationClient, MemberAutomationError,
)


PERSON_ID = 'action_builder:11111111-1111-4111-8111-111111111111'
OTHER_ID = 'action_builder:22222222-2222-4222-8222-222222222222'
CONFIG = ActionBuilderConfig('invented-token', 'example', 'campaign', '1105')
RECEIPT = {'person': {'identifiers': [PERSON_ID]}}
TAGS = [
    {'action_builder:section': SECTION, 'action_builder:field': CLASSIFICATION_FIELD, 'name': 'CE/CW'},
    {'action_builder:section': SECTION, 'action_builder:field': JURISDICTION_FIELD, 'name': '1105'},
]


def response(status=200, document=None):
    result = Mock(status_code=status)
    result.json.return_value = deepcopy(document)
    return result


class GetRetryTests(unittest.TestCase):
    def setUp(self):
        get = patch.object(http.requests, 'get', side_effect=AssertionError('Configure mocked GET.'))
        self.get = get.start()
        self.addCleanup(get.stop)
        pause = patch.object(http.time, 'sleep')
        self.sleep = pause.start()
        self.addCleanup(pause.stop)
        post = patch.object(http.requests, 'post', side_effect=AssertionError('GET retry must never POST.'))
        self.post = post.start()
        self.addCleanup(post.stop)
        self.addCleanup(self.post.assert_not_called)

    def fetch(self):
        return http.get_with_retries(
            CONFIG.people_url, headers=CONFIG.headers,
            params={'filter': "email_address eq 'invented@example.test'", 'page': 1},
            timeout=(5, 30), allow_redirects=False,
        )

    def test_success_is_one_unchanged_get_without_retry_wait(self):
        success = response(document={'arbitrary': 'unparsed body'})
        self.get.side_effect = None
        self.get.return_value = success
        self.assertIs(self.fetch(), success)
        self.get.assert_called_once_with(
            CONFIG.people_url, headers=CONFIG.headers,
            params={'filter': "email_address eq 'invented@example.test'", 'page': 1},
            timeout=(5, 30), allow_redirects=False,
        )
        self.sleep.assert_not_called()
        success.json.assert_not_called()
        success.close.assert_not_called()

    def test_each_transient_http_status_retries_same_get_and_returns_success(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.get.reset_mock()
                self.sleep.reset_mock()
                failure, success = response(status), response()
                self.get.side_effect = [failure, success]
                self.assertIs(self.fetch(), success)
                self.assertEqual(self.get.call_count, 2)
                self.assertEqual(self.get.call_args_list[0], self.get.call_args_list[1])
                self.sleep.assert_called_once_with(1)
                failure.close.assert_called_once_with()
                success.close.assert_not_called()

    def test_exhausted_http_retries_stop_at_three_and_return_last_response(self):
        failures = [response(503) for _ in range(3)]
        self.get.side_effect = failures
        self.assertIs(self.fetch(), failures[-1])
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        for failure in failures[:-1]:
            failure.close.assert_called_once_with()
        failures[-1].close.assert_not_called()

    def test_timeout_and_connection_error_share_one_three_attempt_budget(self):
        success = response()
        self.get.side_effect = [requests.Timeout('invented timeout'), requests.ConnectionError('invented connection'), success]
        self.assertIs(self.fetch(), success)
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    def test_exhausted_connection_errors_raise_original_last_error(self):
        errors = [requests.Timeout('first'), requests.ConnectionError('second'), requests.Timeout('last')]
        self.get.side_effect = errors
        with self.assertRaises(requests.Timeout) as raised:
            self.fetch()
        self.assertIs(raised.exception, errors[-1])
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    def test_mixed_http_and_transport_failure_share_same_attempt_budget(self):
        final = response(429)
        self.get.side_effect = [response(500), requests.Timeout('invented timeout'), final]
        self.assertIs(self.fetch(), final)
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    def test_auth_redirect_and_other_permanent_http_responses_are_not_retried(self):
        for status in (201, 301, 302, 307, 308, 400, 401, 403, 404, 409, 422, 501):
            with self.subTest(status=status):
                self.get.reset_mock()
                result = response(status)
                self.get.side_effect = None
                self.get.return_value = result
                self.assertIs(self.fetch(), result)
                self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_ssl_and_other_non_connection_errors_are_not_retried(self):
        for error in (requests.exceptions.SSLError('certificate problem'),
                      requests.exceptions.TooManyRedirects('redirect problem'),
                      requests.exceptions.InvalidURL('invalid URL'),
                      requests.HTTPError('permanent HTTP error')):
            with self.subTest(error=type(error).__name__):
                self.get.reset_mock()
                self.get.side_effect = error
                with self.assertRaises(type(error)) as raised:
                    self.fetch()
                self.assertIs(raised.exception, error)
                self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_lookup_and_member_clients_do_not_retry_invalid_json(self):
        for invoke, expected in (
            (lambda: ActionBuilderLookup(CONFIG)._get_page("email_address eq 'invented@example.test'", 1), LookupError),
            (lambda: MemberAutomationClient(CONFIG).get('/people/invented'), MemberAutomationError),
        ):
            with self.subTest(expected=expected):
                self.get.reset_mock()
                bad = response()
                bad.json.side_effect = ValueError('private untrusted body')
                self.get.side_effect = None
                self.get.return_value = bad
                with self.assertRaises(expected) as raised:
                    invoke()
                self.get.assert_called_once()
                self.assertNotIn('private untrusted body', str(raised.exception))

    def test_both_clients_retry_connection_failures_but_keep_error_messages_private(self):
        for invoke, expected in (
            (lambda: ActionBuilderLookup(CONFIG)._get_page("email_address eq 'invented@example.test'", 1), LookupError),
            (lambda: MemberAutomationClient(CONFIG).get('/people/invented'), MemberAutomationError),
        ):
            with self.subTest(expected=expected):
                self.get.reset_mock()
                self.get.side_effect = requests.ConnectionError('invented@example.test invented-token private URL')
                with self.assertRaises(expected) as raised:
                    invoke()
                self.assertEqual(self.get.call_count, 3)
                for sensitive in ('invented@example.test', 'invented-token', 'private URL'):
                    self.assertNotIn(sensitive, str(raised.exception))


class RequiredPersonVerificationTests(unittest.TestCase):
    def setUp(self):
        get = patch('member_automation.requests.get', side_effect=AssertionError('Configure mocked GET.'))
        self.get = get.start()
        self.addCleanup(get.stop)
        pause = patch('member_automation.time.sleep')
        pause.start()
        self.addCleanup(pause.stop)

    def test_legacy_contact_only_verification_remains_optional(self):
        MemberAutomationClient(CONFIG).verify({'person': {}}, RECEIPT)
        self.get.assert_not_called()

    def test_required_contact_only_verification_reads_expected_person_only(self):
        self.get.side_effect = None
        self.get.return_value = response(document={'identifiers': [PERSON_ID, 'custom_id:invented']})
        MemberAutomationClient(CONFIG).verify({'person': {}}, RECEIPT, require_person=True)
        self.get.assert_called_once()
        self.assertTrue(self.get.call_args.args[0].endswith('/people/' + PERSON_ID.split(':')[1]))
        self.assertFalse(self.get.call_args.kwargs['allow_redirects'])

    def test_invalid_receipt_identifiers_fail_before_any_get(self):
        receipts = [None, {}, {'person': None}, {'person': {'identifiers': PERSON_ID}},
                    {'person': {'identifiers': []}}, {'person': {'identifiers': [PERSON_ID, PERSON_ID]}},
                    {'person': {'identifiers': [PERSON_ID, 'action_builder:invalid']}},
                    {'person': {'identifiers': ['action_builder:invalid']}},
                    {'person': {'identifiers': [PERSON_ID, None]}}]
        for receipt in receipts:
            with self.subTest(receipt=receipt), self.assertRaises(MemberAutomationError):
                MemberAutomationClient(CONFIG).verify({'person': {}}, receipt, require_person=True)
        self.get.assert_not_called()

    def test_person_identity_mismatch_is_not_retried(self):
        for identifiers in (None, [], [OTHER_ID], [PERSON_ID, PERSON_ID],
                            [PERSON_ID, OTHER_ID], [PERSON_ID, None], [PERSON_ID, 'action_builder:invalid']):
            with self.subTest(identifiers=identifiers):
                self.get.reset_mock()
                self.get.side_effect = None
                self.get.return_value = response(document={'identifiers': identifiers})
                with self.assertRaisesRegex(MemberAutomationError, 'confirmed person ID'):
                    MemberAutomationClient(CONFIG).verify({'person': {}}, RECEIPT, require_person=True)
                self.get.assert_called_once()

    def test_assessment_mismatch_is_not_retried(self):
        self.get.side_effect = None
        self.get.return_value = response(document={'identifiers': [PERSON_ID], 'action_builder:latest_assessment': 2})
        with self.assertRaisesRegex(MemberAutomationError, 'Assessment 1'):
            MemberAutomationClient(CONFIG).verify({'person': {}, 'add_tags': TAGS}, RECEIPT, require_person=True)
        self.get.assert_called_once()

    def test_missing_tag_is_not_retried(self):
        self.get.side_effect = [
            response(document={'identifiers': [PERSON_ID], 'action_builder:latest_assessment': 1}),
            response(document={'page': 1, 'per_page': 25, 'total_pages': 1, '_embedded': {'osdi:taggings': TAGS[:1]}}),
        ]
        with self.assertRaisesRegex(MemberAutomationError, 'Local Jurisdiction'):
            MemberAutomationClient(CONFIG).verify({'person': {}, 'add_tags': TAGS}, RECEIPT, require_person=True)
        self.assertEqual(self.get.call_count, 2)

    def test_required_member_verification_keeps_both_tag_and_assessment_checks(self):
        self.get.side_effect = [
            response(document={'identifiers': [PERSON_ID], 'action_builder:latest_assessment': 1}),
            response(document={'page': 1, 'per_page': 25, 'total_pages': 1, '_embedded': {'osdi:taggings': TAGS}}),
        ]
        MemberAutomationClient(CONFIG).verify({'person': {}, 'add_tags': TAGS}, RECEIPT, require_person=True)
        self.assertEqual(self.get.call_count, 2)


if __name__ == '__main__':
    unittest.main()
