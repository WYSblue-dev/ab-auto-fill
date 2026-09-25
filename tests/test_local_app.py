"""Exercise the local app using temporary workspaces and mocked Action Builder."""
import io
import json
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

import local_app
from action_builder_lookup import LookupError
from record_queue import RecordQueue
from extract_person import ImportSummary
from test_send_queue import RECORD, CLEAR, SUCCESS
from test_lookup_queue import EXISTING, AMBIGUOUS
from test_action_builder_lookup import collection, response


REAL_LOOKUP_CHECK = local_app.ActionBuilderLookup.check


class LocalAppTests(unittest.TestCase):
    def enter_patch(self, context):
        value = context.start()
        self.addCleanup(context.stop)
        return value

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.app = local_app.Application(self.root, self.root / 'queue')
        self.data = {'api_key': 'test-secret', 'subdomain': 'example', 'campaign_id': 'campaign', 'downloads': str(self.root)}
        self.app.settings.save(self.data)
        self.config = self.app.settings.config()
        self.lookup = self.enter_patch(patch.object(local_app.ActionBuilderLookup, 'check', return_value=CLEAR))
        self.post = self.enter_patch(patch.object(local_app, 'submit_to_actionbuilder', return_value=SUCCESS))
        self.enter_patch(patch.object(local_app.time, 'sleep'))
        self.enter_patch(patch.object(requests, 'get', side_effect=AssertionError('Real GET forbidden')))
        self.enter_patch(patch.object(requests, 'post', side_effect=AssertionError('Real POST forbidden')))

    def seed(self, *, held=False, person=None, family='sample'):
        with RecordQueue(self.app.queue_dir) as queue:
            queue.enqueue(family, person or RECORD, {family + '-source'}, [family + '.pdf'])
            if held:
                queue.hold(family, {family + '-source'}, [family + '.pdf'], 'Check this contact.')
            items = queue.review_records() if held else queue.pending_records()
            return next(item for item in items if item.family == family)

    def batch_data(self):
        return {'ids': [row['id'] for row in self.app.state()['records'] if row['status']=='pending'],
                'destination': self.config.destination, 'confirmed': True}

    def review_data(self, item):
        return {'id': item.id, 'confirmed': True, 'checked': True, 'reason': 'Verified this is a separate person.'}

    def uncertain_member(self, *, address_line_1=None, family='sample', saved_receipt=False):
        self.app.settings.save({**self.data, 'residence_local': '1105'})
        self.config = self.app.settings.config()
        person = {**RECORD, 'classification': 'CE/CW'}
        if address_line_1 is not None:
            person['address_line_1'] = address_line_1
        item = self.seed(person=person, family=family)
        with RecordQueue(self.app.queue_dir) as queue:
            queue.record_lookup(item, CLEAR.as_history(self.config))
            queue.begin_send(item)
            if saved_receipt:
                queue.record_person_receipt(item, SUCCESS, destination=self.config.destination)
            queue.mark_uncertain(item, 'Earlier sending attempt was not confirmed.')
        self.lookup.return_value = EXISTING
        data = {**self.review_data(item), 'destination': self.config.destination, 'residence_local': '1105'}
        return item, data

    @staticmethod
    def member_verification_responses(*, assessment=1, omit_field=None):
        person = {'identifiers': list(EXISTING.candidates[0]['identifiers']),
                  'action_builder:latest_assessment': assessment}
        tags = [{'action_builder:section': 'Fourth District Workers',
                 'action_builder:field': field, 'name': value}
                for field, value in (('Classification - 4D', 'CE/CW'),
                                     ('Local Jurisdiction by Zip (Residence) - 4D', '1105'))
                if field != omit_field]
        collection = {'page': 1, 'per_page': 25, 'total_pages': 1,
                      '_embedded': {'osdi:taggings': tags}}
        responses = []
        for document in (person, collection):
            response = Mock(status_code=200)
            response.json.return_value = document
            responses.append(response)
        return responses

    def test_first_run_creates_private_empty_env_and_preserves_existing_file(self):
        folder = self.root / 'first'
        folder.mkdir()
        settings = local_app.Settings(folder / '.env')
        self.assertFalse(settings.public()['configured'])
        if os.name == 'posix':
            self.assertEqual(settings.path.stat().st_mode & 0o777, 0o600)
        settings.path.write_text('# preserve me\nOTHER_SETTING=abc\n')
        local_app.Settings(settings.path)
        settings.save(self.data)
        self.assertIn('OTHER_SETTING=abc', settings.path.read_text())
        self.assertIn('# preserve me', settings.path.read_text())

    def test_settings_edit_uses_file_not_stale_environment_and_hides_key(self):
        with patch.dict(os.environ, {'ACTION_BUILDER_API_KEY': 'stale-token'}):
            self.assertEqual(self.app.settings.config().api_key, 'test-secret')
            self.app.settings.save({**self.data, 'api_key': 'replacement'})
            self.assertEqual(self.app.settings.config().api_key, 'replacement')
            self.app.settings.save({**self.data, 'api_key': ''})
            self.assertEqual(self.app.settings.config().api_key, 'replacement')
        self.assertNotIn('replacement', json.dumps(self.app.state()))
        self.assertNotIn('test-secret', json.dumps(self.app.state()))

    def test_invalid_settings_preserve_previous_file(self):
        before = self.app.settings.path.read_bytes()
        with self.assertRaises(LookupError):
            self.app.settings.save({**self.data, 'subdomain': 'https://outside.example'})
        self.assertEqual(before, self.app.settings.path.read_bytes())
        with self.assertRaises(ValueError):
            self.app.settings.save({**self.data, 'downloads': str(self.root / 'missing')})
        self.assertEqual(before, self.app.settings.path.read_bytes())

    def test_residence_local_is_saved_and_preserved_during_credential_edits(self):
        self.app.settings.save({**self.data,'residence_local':'999'})
        self.assertEqual(self.app.settings.config().residence_local,'999')
        self.assertEqual(self.app.settings.public()['residence_local'],'999')
        self.app.settings.save({**self.data,'api_key':'replacement'})
        self.assertEqual(self.app.settings.config().residence_local,'999')

    def test_test_connection_is_get_only_and_does_not_save_proposed_key(self):
        with patch.object(local_app.ActionBuilderLookup, '_search', return_value=[]) as search:
            self.app.perform('test', {**self.data, 'api_key': 'unsaved'})
        search.assert_called_once_with('email_address', 'lookup-diagnostic@example.invalid')
        self.assertEqual(self.app.settings.config().api_key, 'test-secret')
        self.post.assert_not_called()

    def test_county_entry_is_local_and_never_changes_person_payload(self):
        item = self.seed()
        with patch.object(local_app, 'county_from_address') as geocode:
            self.app.perform('county-save', {'id': item.id, 'county': 'Franklin'})
            row = self.app.state()['records'][0]
            self.assertEqual(row['residence']['county'], 'Franklin')
            self.assertEqual(row['person'], RECORD)
            self.assertEqual(row['status'], 'pending')
            geocode.assert_not_called()
        self.post.assert_not_called()
        self.app.perform('send', self.batch_data())
        self.assertNotIn('county', json.dumps(self.post.call_args.args[0]))
        self.assertEqual(self.app.state()['records'][0]['residence']['county'], 'Franklin')

    def test_county_lookup_requires_opt_in_and_errors_do_not_change_submission_state(self):
        item = self.seed()
        with patch.object(local_app, 'county_from_address', side_effect=local_app.CountyLookupError('County not found.')) as geocode:
            with self.assertRaisesRegex(ValueError, 'Enable Census'):
                self.app.perform('county-lookup', {'id': item.id})
            geocode.assert_not_called()
            self.app.settings.save({**self.data, 'county_lookup': 'census'})
            with self.assertRaises(local_app.CountyLookupError):
                self.app.perform('county-lookup', {'id': item.id})
        row = self.app.state()['records'][0]
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['residence'], {})
        self.post.assert_not_called()

    def test_county_reference_recomputes_when_local_changes(self):
        item = self.seed(person={**RECORD, 'region': 'OH'})
        self.app.perform('county-save', {'id': item.id, 'county': 'Muskingum'})
        self.app.settings.save({**self.data, 'residence_local': '1105'})
        self.assertEqual(self.app.state()['records'][0]['jurisdiction']['status'], 'inside')
        self.app.settings.save({**self.data, 'residence_local': '999'})
        result = self.app.state()['records'][0]['jurisdiction']
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['note'], '')

    def test_import_county_lookup_is_optional_cached_and_failure_is_nonblocking(self):
        first = self.seed(family='first')
        self.app.perform('county-save', {'id': first.id, 'county': 'Manual county'})
        self.seed(family='second', person={**RECORD, 'given_name': 'Second'})
        self.seed(family='third', person={**RECORD, 'given_name': 'Third'})
        found = {'county': 'Licking County', 'region': RECORD['region'].upper(), 'source': 'census', 'matched_address': 'Public test address'}
        with patch.object(local_app, 'import_downloads', side_effect=lambda *args: ImportSummary()), patch.object(local_app, 'county_from_address', side_effect=[found, local_app.CountyLookupError('Unmatched')]) as geocode:
            self.app.perform('import', {})
            geocode.assert_not_called()
            self.app.settings.save({**self.data, 'county_lookup': 'census'})
            message = self.app.perform('import', {})
            self.assertEqual(geocode.call_count, 2)
        self.assertIn('1 counties found', message)
        rows = self.app.state()['records']
        self.assertTrue(all(row['status'] == 'pending' for row in rows))
        self.assertEqual(next(row for row in rows if row['id'] == first.id)['residence']['source'], 'manual')
        self.assertEqual(sum(bool(row['residence']) for row in rows), 2)
        self.post.assert_not_called()

    def test_address_correction_invalidates_county_and_old_record_cannot_be_annotated(self):
        item = self.seed(held=True)
        self.app.perform('county-save', {'id': item.id, 'county': 'Franklin'})
        self.app.perform('review-edit', {'id': item.id, 'person': {**RECORD, 'address_line_1': '20 New Street'}})
        self.assertEqual(self.app.state()['records'][0]['residence'], {})
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.app.perform('county-save', {'id': item.id, 'county': 'Franklin'})
        self.post.assert_not_called()

    def test_preview_and_check_only_do_not_submit(self):
        self.seed()
        snapshot = self.app.state()
        self.lookup.assert_not_called()
        self.app.perform('check', self.batch_data())
        self.post.assert_not_called()
        self.assertEqual(self.app.state()['records'][0]['status'], 'pending')
        snapshot['records'][0]['person']['email'] = 'changed@example.test'
        self.assertEqual(self.app.state()['records'][0]['person']['email'], RECORD['email'])

    def test_send_checks_again_and_stores_receipt(self):
        self.seed()
        self.app.perform('check', self.batch_data())
        self.app.perform('send', self.batch_data())
        self.assertEqual(self.lookup.call_count, 2)
        self.post.assert_called_once()
        row = self.app.state()['records'][0]
        self.assertEqual(row['status'], 'sent')
        self.assertIn('identifiers', row['result'])
        self.app.perform('send', self.batch_data())
        self.post.assert_called_once()

    def test_changed_batch_destination_and_missing_confirmation_block_posts(self):
        self.seed()
        data = self.batch_data()
        for invalid in ({**data, 'ids': []}, {**data, 'destination': {}}, {**data, 'confirmed': False}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.app.perform('send', invalid)
        self.post.assert_not_called()
        self.lookup.assert_not_called()

    def test_matched_person_is_held_and_other_person_can_send(self):
        self.seed()
        self.seed(person={**RECORD, 'given_name': 'Other'}, family='second')
        self.lookup.side_effect = [EXISTING, CLEAR]
        self.app.perform('send', self.batch_data())
        self.assertEqual(sorted(row['status'] for row in self.app.state()['records']), ['review','sent'])
        self.post.assert_called_once()

    def test_lookup_failure_never_posts_and_post_timeout_is_held(self):
        self.seed()
        self.lookup.side_effect = LookupError('Lookup unavailable.')
        with self.assertRaises(LookupError):
            self.app.perform('send', self.batch_data())
        self.post.assert_not_called()
        self.lookup.side_effect = None
        self.post.side_effect = requests.Timeout('private URL')
        with self.assertRaises(requests.Timeout):
            self.app.perform('send', self.batch_data())
        row = self.app.state()['records'][0]
        self.assertEqual(row['status'], 'uncertain')
        self.assertTrue(row['related_uncertain'])
        self.app.perform('send', self.batch_data())
        self.post.assert_called_once()
        reopened = local_app.Application(self.root, self.app.queue_dir)
        reason = reopened.state()['records'][0]['reason']
        self.assertIn('Check your connection', reason)
        self.assertNotIn('private URL', reason)

    def test_member_submission_failure_reason_survives_reopening(self):
        self.seed()
        message = 'Classification - 4D was not confirmed. The API returned no response for this field.'
        self.post.side_effect = local_app.MemberAutomationError(message)
        with self.assertRaises(local_app.MemberAutomationError):
            self.app.perform('send', self.batch_data())
        reopened = local_app.Application(self.root, self.app.queue_dir)
        row = reopened.state()['records'][0]
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['reason'], 'Sending stopped: ' + message)
        self.post.assert_called_once()

    def test_person_receipt_is_durable_before_member_verification_failure(self):
        self.app.settings.save({**self.data, 'residence_local': '1105'})
        self.config = self.app.settings.config()
        item = self.seed(person={**RECORD, 'classification': 'CE/CW'})

        def created_but_unverified(payload, *, config, on_person_receipt, **options):
            on_person_receipt(SUCCESS)
            raise local_app.MemberAutomationError('Member information could not be verified: HTTP 503.')

        self.post.side_effect = created_but_unverified
        with patch.object(local_app.MemberAutomationClient, 'preflight'):
            with self.assertRaises(local_app.MemberAutomationError):
                self.app.perform('send', {**self.batch_data(), 'residence_local': '1105'})
        reopened = local_app.Application(self.root, self.app.queue_dir)
        row = reopened.state()['records'][0]
        self.assertEqual(row['id'], item.id)
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['pending_receipt'], {'identifiers': SUCCESS['person']['identifiers'],
                                                 'destination': self.config.destination})
        self.assertTrue(row['related_created'])
        self.assertIn('Person created; verification could not complete', row['reason'])
        self.assertNotIn('tag failed', row['reason'])
        self.app.perform('send', {**self.batch_data(), 'residence_local': '1105'})
        self.post.assert_called_once()

    def test_review_requires_displayed_fresh_check_and_explicit_acknowledgment(self):
        item = self.seed(held=True)
        data = self.review_data(item)
        with self.assertRaises(ValueError):
            self.app.perform('review-send', data)
        self.app.perform('review-check', {'id':item.id})
        with self.assertRaises(ValueError):
            self.app.perform('review-send', {**data, 'checked':False})
        self.post.assert_not_called()
        self.app.perform('review-check', {'id':item.id})
        self.app.perform('review-send', data)
        self.post.assert_called_once()
        self.assertEqual(self.app.state()['records'][0]['status'], 'sent')

    def test_changed_review_result_requires_another_human_review(self):
        item = self.seed(held=True)
        self.app.perform('review-check', {'id':item.id})
        self.lookup.return_value = AMBIGUOUS
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.app.perform('review-send', self.review_data(item))
        self.post.assert_not_called()
        self.assertEqual(self.app.state()['records'][0]['lookup']['outcome'], 'needs_review')

    def test_review_credential_change_revokes_approval(self):
        item = self.seed(held=True)
        self.app.perform('review-check', {'id':item.id})
        self.app.perform('settings', {**self.data, 'api_key':'new-key'})
        with self.assertRaises(ValueError):
            self.app.perform('review-send', self.review_data(item))
        self.post.assert_not_called()

    def test_review_approval_expires_and_related_receipt_blocks_creation(self):
        item = self.seed(held=True)
        self.app.perform('review-check', {'id':item.id})
        self.app.approvals[item.id]['at'] -= 601
        with self.assertRaises(ValueError):
            self.app.perform('review-send', self.review_data(item))
        self.app.perform('review-check', {'id':item.id})
        with patch.object(RecordQueue, 'review_details', return_value={'related_sent':True, 'related_uncertain':False}):
            with self.assertRaisesRegex(ValueError, 'already sent'):
                self.app.perform('review-send', self.review_data(item))
        self.post.assert_not_called()

    def test_stop_cannot_interrupt_a_job_or_allow_new_jobs_after_closing(self):
        self.app.job['running'] = True
        with self.assertRaises(ValueError):
            self.app.stop()
        self.assertFalse(self.app.stopping)
        self.app.job['running'] = False
        self.app.stop()
        with self.assertRaisesRegex(ValueError, 'closing'):
            self.app.start('import', {})

    def test_reconcile_existing_person_updates_local_queue_without_post(self):
        item = self.seed()
        with RecordQueue(self.app.queue_dir) as queue:
            queue.record_lookup(item, CLEAR.as_history(self.config))
            queue.begin_send(item)
            queue.mark_uncertain(item, 'Unconfirmed receipt.')
        self.lookup.return_value = EXISTING
        data = {**self.review_data(item), 'destination':self.config.destination}
        with self.assertRaises(ValueError):
            self.app.perform('review-reconcile', {**data, 'checked':False})
        self.app.perform('review-reconcile', data)
        self.post.assert_not_called()
        self.assertEqual(self.app.state()['records'][0]['status'], 'sent')

    def test_configured_reconcile_verifies_both_tags_and_assessment_without_post(self):
        item, data = self.uncertain_member()
        with patch.object(requests, 'get', side_effect=self.member_verification_responses()) as get:
            self.app.perform('review-reconcile', data)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(get.call_args_list[0].args[0].endswith('/people/' + EXISTING.candidates[0]['identifiers'][0].partition(':')[2]))
        self.assertTrue(get.call_args_list[1].args[0].endswith('/taggings'))
        self.post.assert_not_called()
        reopened = local_app.Application(self.root, self.app.queue_dir)
        row = reopened.state()['records'][0]
        self.assertEqual(row['id'], item.id)
        self.assertEqual(row['status'], 'sent')
        self.assertEqual(row['lookup']['outcome'], 'existing')
        self.assertEqual(row['result']['identifiers'], EXISTING.candidates[0]['identifiers'])

    def test_saved_person_receipt_verifies_by_id_without_contact_search_or_post(self):
        item, data = self.uncertain_member(address_line_1='123 Unstandardized Street', saved_receipt=True)
        original_lookup = self.app.state()['records'][0]['lookup']
        self.lookup.side_effect = AssertionError('A saved person receipt must not trigger contact searches.')
        with patch.object(requests, 'get', side_effect=self.member_verification_responses()) as get:
            self.app.perform('review-reconcile', data)
        self.assertEqual(get.call_count, 2)
        person_id = SUCCESS['person']['identifiers'][0].partition(':')[2]
        self.assertTrue(get.call_args_list[0].args[0].endswith('/people/' + person_id))
        self.assertTrue(get.call_args_list[1].args[0].endswith('/people/' + person_id + '/taggings'))
        self.lookup.assert_not_called()
        self.post.assert_not_called()
        reopened = local_app.Application(self.root, self.app.queue_dir)
        row = reopened.state()['records'][0]
        self.assertEqual(row['id'], item.id)
        self.assertEqual(row['status'], 'sent')
        self.assertEqual(row['result']['identifiers'], SUCCESS['person']['identifiers'])
        self.assertFalse(row.get('pending_receipt'))
        self.assertEqual(row['lookup'], original_lookup)
        with self.assertRaisesRegex(ValueError, 'no longer available'):
            self.app.perform('review-reconcile', data)

    def test_saved_receipt_contact_only_verification_still_checks_person_id(self):
        item = self.seed()
        with RecordQueue(self.app.queue_dir) as queue:
            queue.record_lookup(item, CLEAR.as_history(self.config))
            queue.begin_send(item)
            queue.record_person_receipt(item, SUCCESS, destination=self.config.destination)
            queue.mark_uncertain(item, 'Interrupted before verification finished.')
        data = {**self.review_data(item), 'destination': self.config.destination}
        with patch.object(requests, 'get', return_value=self.member_verification_responses()[0]) as get:
            self.app.perform('review-reconcile', data)
        get.assert_called_once()
        self.lookup.assert_not_called()
        self.post.assert_not_called()
        self.assertEqual(self.app.state()['records'][0]['status'], 'sent')

    def test_saved_person_receipt_survives_read_error_without_private_details(self):
        _, data = self.uncertain_member(saved_receipt=True)
        original = self.app.state()['records'][0]['pending_receipt']
        with patch.object(requests, 'get', side_effect=requests.Timeout('private-token and private-address')):
            with self.assertRaises(local_app.MemberAutomationError):
                self.app.perform('review-reconcile', data)
        reopened = local_app.Application(self.root, self.app.queue_dir)
        row = reopened.state()['records'][0]
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['pending_receipt'], original)
        self.assertTrue(row['related_created'])
        self.assertIn('Verification stopped', row['reason'])
        self.assertNotIn('private-', row['reason'])
        self.lookup.assert_not_called()
        self.post.assert_not_called()

    def test_saved_person_receipt_rejects_wrong_returned_person_id(self):
        _, data = self.uncertain_member(saved_receipt=True)
        document = {'identifiers': ['action_builder:22222222-2222-4222-8222-222222222222'],
                    'action_builder:latest_assessment': 1}
        with patch.object(requests, 'get', return_value=response(document)) as get:
            with self.assertRaises(local_app.MemberAutomationError):
                self.app.perform('review-reconcile', data)
        get.assert_called_once()
        row = self.app.state()['records'][0]
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['pending_receipt']['identifiers'], SUCCESS['person']['identifiers'])
        self.lookup.assert_not_called()
        self.post.assert_not_called()

    def test_saved_person_receipt_requires_confirmed_original_campaign_and_settings(self):
        _, data = self.uncertain_member(saved_receipt=True)
        for changed in ({'checked': False}, {'confirmed': False}, {'residence_local': '999'},
                        {'destination': {**self.config.destination, 'campaign_id': 'another'}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.app.perform('review-reconcile', {**data, **changed})
        self.app.settings.save({**self.data, 'campaign_id': 'another', 'residence_local': '1105'})
        with self.assertRaisesRegex(ValueError, 'saved person receipt'):
            self.app.perform('review-reconcile', {**data, 'destination': self.app.settings.config().destination})
        row = self.app.state()['records'][0]
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['pending_receipt']['destination'], self.config.destination)
        self.lookup.assert_not_called()
        self.post.assert_not_called()

    def test_saved_person_receipt_blocks_another_new_person_submission(self):
        item, _ = self.uncertain_member(saved_receipt=True)
        self.app.perform('review-check', {'id': item.id})
        self.lookup.reset_mock()
        with self.assertRaisesRegex(ValueError, 'already created'):
            self.app.perform('review-send', self.review_data(item))
        self.lookup.assert_not_called()
        self.post.assert_not_called()

    def test_saved_person_receipt_blocks_edit_without_losing_verification_record(self):
        item, _ = self.uncertain_member(saved_receipt=True)
        original = self.app.state()['records'][0]
        with self.assertRaisesRegex(ValueError, 'Verify the created person before editing'):
            self.app.perform('review-edit', {'id': item.id,
                'person': {**original['person'], 'address_line_1': '456 Corrected Street'}})
        reopened = local_app.Application(self.root, self.app.queue_dir)
        self.assertEqual(reopened.state()['records'], [original])
        self.lookup.assert_not_called()
        self.post.assert_not_called()

    def test_real_lookup_reconciles_suffix_difference_and_verifies_member_details(self):
        item, data = self.uncertain_member(address_line_1='123 Sample Street')
        record = self.app.state()['records'][0]['person']
        remote = deepcopy(local_app.build_actionbuilder_payload(record)['person'])
        remote['identifiers'] = list(EXISTING.candidates[0]['identifiers'])
        remote['postal_addresses'][0]['address_lines'] = ['123 Sample St.']
        documents = [response(collection([remote])), response(collection([remote])),
                     *self.member_verification_responses()]
        with (patch.object(local_app.ActionBuilderLookup, 'check', REAL_LOOKUP_CHECK),
              patch.object(requests, 'get', side_effect=documents) as get):
            self.app.perform('review-reconcile', data)
        self.assertEqual(get.call_count, 4)
        self.assertTrue(get.call_args_list[0].kwargs['params']['filter'].startswith('email_address eq '))
        self.assertTrue(get.call_args_list[1].kwargs['params']['filter'].startswith('phone_number eq '))
        self.assertTrue(get.call_args_list[-1].args[0].endswith('/taggings'))
        self.post.assert_not_called()
        reopened = local_app.Application(self.root, self.app.queue_dir)
        row = reopened.state()['records'][0]
        self.assertEqual(row['id'], item.id)
        self.assertEqual(row['status'], 'sent')
        self.assertEqual(row['lookup']['candidates'][0]['differing_fields'], [])
        self.assertEqual(row['person']['address_line_1'], '123 Sample Street')

    def test_real_lookup_reconciliation_keeps_different_house_or_unit_held(self):
        cases = (('123 Sample Street', '124 Sample St.'),
                 ('123 Sample Street Apt 2', '123 Sample St. Apt 3'))
        for index, (local_street, remote_street) in enumerate(cases):
            with self.subTest(local_street=local_street, remote_street=remote_street):
                item, data = self.uncertain_member(address_line_1=local_street, family=f'address-case-{index}')
                record = next(row['person'] for row in self.app.state()['records'] if row['id'] == item.id)
                remote = deepcopy(local_app.build_actionbuilder_payload(record)['person'])
                remote['identifiers'] = list(EXISTING.candidates[0]['identifiers'])
                remote['postal_addresses'][0]['address_lines'] = [remote_street]
                with (patch.object(local_app.ActionBuilderLookup, 'check', REAL_LOOKUP_CHECK),
                      patch.object(requests, 'get', side_effect=[response(collection([remote])), response(collection([remote]))]) as get):
                    with self.assertRaisesRegex(ValueError, 'exact matching person'):
                        self.app.perform('review-reconcile', data)
                self.assertEqual(get.call_count, 2)
                row = next(row for row in self.app.state()['records'] if row['id'] == item.id)
                self.assertEqual(row['status'], 'uncertain')
                self.assertEqual(row['lookup']['outcome'], 'needs_review')
                self.assertEqual(row['lookup']['candidates'][0]['differing_fields'], ['address_line_1'])
                self.assertEqual(row['person']['address_line_1'], local_street)
        self.post.assert_not_called()

    def test_reconcile_assessment_failure_preserves_latest_match_and_reason(self):
        _, data = self.uncertain_member()
        with patch.object(requests, 'get', side_effect=self.member_verification_responses(assessment=None)):
            with self.assertRaisesRegex(local_app.MemberAutomationError, 'Assessment 1'):
                self.app.perform('review-reconcile', data)
        reopened = local_app.Application(self.root, self.app.queue_dir)
        row = reopened.state()['records'][0]
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['lookup']['outcome'], 'existing')
        self.assertIn('Assessment 1 was not confirmed', row['reason'])
        self.assertIn('did not return an assessment', row['reason'])
        self.post.assert_not_called()

    def test_reconcile_missing_tag_stays_uncertain_and_names_the_field(self):
        _, data = self.uncertain_member()
        with patch.object(requests, 'get', side_effect=self.member_verification_responses(omit_field='Classification - 4D')):
            with self.assertRaisesRegex(local_app.MemberAutomationError, 'Classification - 4D'):
                self.app.perform('review-reconcile', data)
        row = self.app.state()['records'][0]
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['lookup']['outcome'], 'existing')
        self.assertIn('Classification - 4D', row['reason'])
        self.post.assert_not_called()

    def test_reconcile_nonexact_match_saves_current_differing_fields(self):
        _, data = self.uncertain_member()
        self.lookup.return_value = AMBIGUOUS
        with patch.object(local_app.MemberAutomationClient, 'verify') as verify:
            with self.assertRaisesRegex(ValueError, 'exact matching person'):
                self.app.perform('review-reconcile', data)
            verify.assert_not_called()
        row = self.app.state()['records'][0]
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['lookup']['outcome'], 'needs_review')
        self.assertEqual(row['lookup']['candidates'][0]['differing_fields'], ['phone'])
        self.assertIn('latest matching and differing fields', row['reason'])
        self.post.assert_not_called()

    def test_reconcile_request_error_is_persisted_without_raw_request_details(self):
        _, data = self.uncertain_member()
        self.lookup.side_effect = requests.Timeout('https://private.invalid/?token=test-secret&email=private@example.test')
        self.app._work('review-reconcile', data)
        self.assertTrue(self.app.status()['error'])
        reopened = local_app.Application(self.root, self.app.queue_dir)
        row = reopened.state()['records'][0]
        self.assertEqual(row['status'], 'uncertain')
        self.assertEqual(row['lookup']['outcome'], 'not_found')
        self.assertIn('Check your connection', row['reason'])
        for text in (row['reason'], self.app.status()['message']):
            self.assertNotIn('private.invalid', text)
            self.assertNotIn('test-secret', text)
            self.assertNotIn('private@example.test', text)
        self.post.assert_not_called()

    def test_reconcile_checks_saved_destination_before_replacing_lookup(self):
        _, data = self.uncertain_member()
        original = self.app.state()['records'][0]['lookup']
        self.app.settings.save({**self.data, 'campaign_id': 'other-campaign', 'residence_local': '1105'})
        current = self.app.settings.config().destination
        with self.assertRaisesRegex(ValueError, 'saved submission lookup'):
            self.app.perform('review-reconcile', {**data, 'destination': current})
        self.lookup.assert_not_called()
        row = self.app.state()['records'][0]
        self.assertEqual(row['lookup'], original)
        self.assertEqual(row['status'], 'uncertain')
        self.post.assert_not_called()

    def test_reconcile_rejects_stale_confirmation_destination_or_residence_local(self):
        _, data = self.uncertain_member()
        for changed in ({'destination': {'subdomain': 'example', 'campaign_id': 'other-campaign'}},
                        {'residence_local': '999'}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'changed'):
                self.app.perform('review-reconcile', {**data, **changed})
        self.lookup.assert_not_called()
        self.post.assert_not_called()

    def test_review_check_cannot_replace_uncertain_submission_destination(self):
        item, data = self.uncertain_member()
        original = self.app.state()['records'][0]['lookup']
        self.app.settings.save({**self.data, 'campaign_id': 'other-campaign', 'residence_local': '1105'})
        with self.assertRaises(local_app.QueueError):
            self.app.perform('review-check', {'id': item.id})
        self.lookup.assert_called_once()
        current = self.app.settings.config().destination
        with self.assertRaisesRegex(ValueError, 'saved submission lookup'):
            self.app.perform('review-reconcile', {**data, 'destination': current})
        self.lookup.assert_called_once()  # Reconciliation stops before a second lookup.
        row = self.app.state()['records'][0]
        self.assertEqual(row['lookup'], original)
        self.assertEqual(row['status'], 'uncertain')
        self.post.assert_not_called()

    def test_review_corrections_and_discard_preserve_history(self):
        item = self.seed(held=True)
        self.app.perform('review-edit', {'id':item.id, 'person':{**RECORD,'email':'corrected@example.test'}})
        rows = self.app.state()['records']
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]['id'], item.id)
        self.assertEqual(rows[0]['person']['email'], 'corrected@example.test')
        self.app.perform('review-discard', {'id':rows[0]['id'],'reason':'Duplicate import.'})
        self.assertEqual(self.app.state()['records'], [])
        self.post.assert_not_called()

    def test_background_job_prevents_overlap_and_reports_sanitized_errors(self):
        entered, release = threading.Event(), threading.Event()
        def perform(*args):
            entered.set()
            release.wait(3)
            raise requests.ConnectionError('secret query token')
        with patch.object(self.app, 'perform', side_effect=perform):
            self.app.start('test', {})
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(ValueError, 'already running'):
                self.app.start('test', {})
            release.set()
            # Acquire the operation lock to wait until the mocked call returns.
            with self.app.lock:
                pass
        # Complete the worker synchronously for deterministic error assertions.
        with patch.object(self.app, 'perform', side_effect=requests.ConnectionError('secret query token')):
            self.app._work('test', {})
        self.assertTrue(self.app.status()['error'])
        self.assertNotIn('secret', self.app.status()['message'])


class HandlerTests(unittest.TestCase):
    """Exercise real handler routing without binding a socket in unit tests."""
    def handler(self, *, path='/api/state', token='session-secret', host='127.0.0.1:8765', origin=None, data=None):
        handler = object.__new__(local_app.Handler)
        handler.path = path
        handler.headers = {'Host':host, 'X-App-Token':token, 'Content-Type':'application/json'}
        if origin is not None:
            handler.headers['Origin'] = origin
        body = json.dumps(data or {}).encode()
        handler.headers['Content-Length'] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.server = Mock(origin='http://127.0.0.1:8765', token='session-secret')
        handler.server.app.status.return_value = {'running':False}
        handler.server.app.state.return_value = {'records':[]}
        handler.reply = Mock()
        return handler

    def test_api_rejects_other_origins_hosts_and_missing_tokens(self):
        for args in ({'token':''}, {'origin':'https://evil.example'}, {'host':'evil.example:8765'}):
            handler = self.handler(**args)
            handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[0], 403)
            handler.server.app.state.assert_not_called()

    def test_authorized_api_and_static_allowlist(self):
        handler = self.handler()
        handler.do_GET()
        self.assertEqual(handler.reply.call_args.args[0],200)
        for path in ('/.env','/../.env','/composed_info/.queue-state.json'):
            handler = self.handler(path=path)
            handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[0],404)

    def test_post_requires_authorization_and_limits_request_body(self):
        handler = self.handler(path='/api/send', token='')
        handler.do_POST()
        handler.server.app.start.assert_not_called()
        handler = self.handler(path='/api/settings')
        handler.headers['Content-Length']='70000'
        handler.do_POST()
        handler.server.app.start.assert_not_called()
        self.assertEqual(handler.reply.call_args.args[0],400)

    def test_running_job_blocks_state_and_quit(self):
        handler = self.handler()
        handler.server.app.status.return_value={'running':True}
        handler.do_GET()
        self.assertEqual(handler.reply.call_args.args[0],409)
        handler.path='/api/quit'
        handler.server.app.stop.side_effect=ValueError('Wait for the current operation.')
        handler.do_POST()
        handler.server.shutdown.assert_not_called()
        self.assertEqual(handler.reply.call_args.args[0],400)
