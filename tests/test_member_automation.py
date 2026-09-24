"""Member tagging tests use invented contacts and mocked requests exclusively."""
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from action_builder_lookup import ActionBuilderConfig, LookupError
from member_automation import (CLASSIFICATION_FIELD, JURISDICTION_FIELD, SECTION, MemberAutomationClient,
                               MemberAutomationError, classification_tag, prepare_member_payload)
from member_classification import CLASSIFICATIONS, normalize_classification, cwce_classification
from extract_person import TextFragment
from record_queue import RecordQueue
import send_person
from test_send_queue import RECORD, CLEAR, SUCCESS
from test_lookup_queue import EXISTING
import review_person

CONFIG = ActionBuilderConfig('invented-token','example','campaign','1105')
IDENTIFIER = SUCCESS['person']['identifiers'][0]


def member_payload():
    return prepare_member_payload(send_person.build_actionbuilder_payload({**RECORD,'classification':'CE/CW'}),CONFIG)


def collection(key, rows):
    return {'page':1,'per_page':25,'total_pages':1,'_embedded':{key:rows}}


def response(value, status=200):
    result=Mock(status_code=status)
    result.json.return_value=deepcopy(value)
    return result


class ClassificationTests(unittest.TestCase):
    def test_allowlist_and_explicit_level_mapping(self):
        self.assertEqual(len(set(CLASSIFICATIONS)), len(CLASSIFICATIONS))
        for value in CLASSIFICATIONS:
            self.assertEqual(normalize_classification(value), value)
        for value in ('CW','CE','CW I','CW II','CW III','CW IV','CW V','CE I','CE II','CE III','CE 3'):
            self.assertEqual(normalize_classification(value), 'CE/CW')
        self.assertEqual(normalize_classification('JOURNEYMAN'),'Journeyman')
        for value in ('',None,'Apprentice','CW unknown','Journeyman Inside','Not available'):
            with self.assertRaises(ValueError):
                normalize_classification(value)

    def sheet(self, marks=(6,), *, missing_box=False, moved_label=False, scale=1, rotation=0, offset=0):
        # Coordinates copied from the independently inspected source layout.
        rows=[677.90,660.19,642.49,625.15,608.20,591.24,574.28,557.32]
        fragments=[TextFragment('CW',101.25,625.14),TextFragment('IV',114.85,625.14),
                   TextFragment('CE',101.25,574.27),TextFragment('III',112.91,574.27),
                   TextFragment('JOURNEYMAN',101.25,557.31), TextFragment('12000+',162.75,557.31),
                   TextFragment('classification.',435.66,710.62 if not moved_label else 650)]
        page=Mock(mediabox=(0,0,612,792),cropbox=(0,0,612,792),rotation=rotation)
        page.get.return_value=scale
        def draw(visitor_operand_before):
            visit=visitor_operand_before
            for y in rows[1:] if missing_box else rows:
                visit(b'Do',['/BlankBox'],[9,0,0,8.25,81,y],[])
            for row in marks:
                y=rows[row]+4.22+offset
                visit(b'm',[85.17,y-4.5],[1,0,0,1,0,0],[])
                # The source's filled ring and center use 12 bezier segments.
                for _ in range(12):
                    visit(b'c',[80.67,y-4.5,89.67,y+4.5,85.17,y],[1,0,0,1,0,0],[])
                visit(b'f',[],[1,0,0,1,0,0],[])
        page.extract_text.side_effect=draw
        return page,fragments

    def test_each_cw_ce_checkbox_maps_to_ce_cw_and_journeyman_is_distinct(self):
        for row in range(8):
            page,fragments=self.sheet((row,))
            self.assertEqual(cwce_classification(page,fragments),'Journeyman' if row==7 else 'CE/CW')
        page,fragments=self.sheet((0,),offset=-1.2)
        self.assertEqual(cwce_classification(page,fragments),'CE/CW')

    def test_missing_multiple_or_unrecognized_layout_needs_review(self):
        for settings in ({'marks':()}, {'marks':(0,6)}, {'missing_box':True}, {'moved_label':True}, {'scale':2}, {'rotation':90}):
            with self.subTest(settings=settings):
                page,fragments=self.sheet(**settings)
                self.assertIsNone(cwce_classification(page,fragments))


class MemberAutomationTests(unittest.TestCase):
    def setUp(self):
        patcher=patch('member_automation.time.sleep')
        patcher.start();self.addCleanup(patcher.stop)

    def test_payload_has_only_two_tags_user_constant_and_assessment_one(self):
        original=send_person.build_actionbuilder_payload({**RECORD,'classification':'CE III'})
        payload=prepare_member_payload(original,CONFIG)
        self.assertEqual(len(payload['add_tags']),2)
        self.assertEqual(payload['add_tags'][0],classification_tag('CE/CW'))
        self.assertEqual(payload['add_tags'][1],{'action_builder:section':SECTION,'action_builder:field':JURISDICTION_FIELD,'name':'1105'})
        self.assertEqual(payload['person']['action_builder:latest_assessment'],1)
        self.assertNotIn('classification',payload['person'])
        self.assertEqual(len(original['add_tags']),1)
        self.assertEqual(prepare_member_payload(payload,CONFIG),payload)
        other=ActionBuilderConfig('token','example','campaign','999')
        self.assertEqual(prepare_member_payload(original,other)['add_tags'][1]['name'],'999')
        self.assertFalse(any('note' in key for tag in payload['add_tags'] for key in tag))

    def test_missing_classification_or_local_never_silently_selects_none(self):
        with self.assertRaises(MemberAutomationError):
            prepare_member_payload(send_person.build_actionbuilder_payload(RECORD),CONFIG)
        blank=ActionBuilderConfig('token','example','campaign')
        with self.assertRaises(MemberAutomationError):
            prepare_member_payload(send_person.build_actionbuilder_payload({**RECORD,'classification':'CE/CW'}),blank)
        old=send_person.build_actionbuilder_payload(RECORD)
        self.assertEqual(prepare_member_payload(old,blank),old)
        self.assertNotIn('action_builder:latest_assessment',old['person'])
        with self.assertRaises(LookupError):
            ActionBuilderConfig('token','example','campaign','not-a-local')

    def test_taxonomy_filters_by_response_then_exact_section_and_field(self):
        payload=member_payload()
        rows=[{**tag,'action_builder:field_type':'standard'} for tag in payload['add_tags']]
        documents=[collection('osdi:tags',[{**rows[0],'action_builder:section':'Another section'},rows[0]]),collection('osdi:tags',[rows[1]])]
        with patch('member_automation.requests.get',side_effect=[response(doc) for doc in documents]) as get:
            MemberAutomationClient(CONFIG).preflight(payload)
        self.assertEqual(get.call_count,2)
        self.assertTrue(all(call.args[0].endswith('/tags') and call.kwargs['allow_redirects'] is False for call in get.call_args_list))
        self.assertEqual(get.call_args_list[0].kwargs['params']['filter'],"name eq 'CE/CW'")

    def test_missing_or_ambiguous_taxonomy_blocks_before_any_post(self):
        for rows in ([],[{'name':'CE/CW','action_builder:section':'Wrong','action_builder:field':CLASSIFICATION_FIELD}],
                     [{**classification_tag('CE/CW'),'action_builder:field_type':'standard'}]*2):
            with self.subTest(rows=rows), patch('member_automation.requests.get',return_value=response(collection('osdi:tags',rows))), patch('send_person.requests.post') as post:
                with self.assertRaises(MemberAutomationError):
                    send_person.submit_to_actionbuilder(send_person.build_actionbuilder_payload({**RECORD,'classification':'CE/CW'}),config=CONFIG)
                post.assert_not_called()

    def test_person_receipt_is_not_enough_until_both_tags_and_assessment_match(self):
        payload=member_payload()
        person={'identifiers':[IDENTIFIER],'action_builder:latest_assessment':1}
        tags=payload['add_tags']
        for observed_person,observed_tags in ((person,tags[:-1]),({**person,'action_builder:latest_assessment':2},tags),
                                              (person,[tags[0],{**tags[1],'name':'999'}]),(person,tags+[tags[1]])):
            with self.subTest(person=observed_person,tags=observed_tags), patch('member_automation.requests.get',side_effect=[response(observed_person),response(collection('osdi:taggings',observed_tags))]):
                with self.assertRaises(MemberAutomationError):
                    MemberAutomationClient(CONFIG).verify(payload,SUCCESS)
        with patch('member_automation.requests.get',side_effect=[response(person),response(collection('osdi:taggings',tags))]) as get:
            MemberAutomationClient(CONFIG).verify(payload,SUCCESS)
        self.assertEqual(get.call_count,2)
        self.assertIn('/people/'+IDENTIFIER.split(':')[1],get.call_args_list[0].args[0])

    def test_post_is_not_repeated_when_member_verification_fails(self):
        with (patch.object(MemberAutomationClient,'preflight'),
              patch.object(MemberAutomationClient,'verify',side_effect=MemberAutomationError('Tag missing')),
              patch('send_person.requests.post',return_value=response(SUCCESS)) as post):
            with self.assertRaises(MemberAutomationError):
                send_person.submit_to_actionbuilder(send_person.build_actionbuilder_payload({**RECORD,'classification':'CE/CW'}),config=CONFIG)
        post.assert_called_once()

    def test_collection_rejects_missing_rows_and_accepts_explicit_zero_pages(self):
        client=MemberAutomationClient(CONFIG)
        with patch.object(client,'get',return_value={'page':1,'per_page':25,'total_pages':0}):
            self.assertEqual(client.collection('/tags','osdi:tags'),[])
        with patch.object(client,'get',return_value={'page':1,'per_page':25,'total_pages':1}):
            with self.assertRaises(MemberAutomationError):
                client.collection('/tags','osdi:tags')

    def test_missing_classification_is_held_before_a_send_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            with RecordQueue(folder) as queue:
                queue.enqueue('sample',RECORD,{'source'},['sample.pdf'])
            with patch.object(ActionBuilderConfig,'from_environment',return_value=CONFIG), patch('requests.post') as post, redirect_stdout(io.StringIO()):
                self.assertEqual(send_person.send_queue(Path(folder),None,True),1)
            post.assert_not_called()
            with RecordQueue(folder) as queue:
                self.assertEqual(queue.pending_records(),[])
                item=queue.review_records()[0]
                self.assertEqual(queue.review_details(item)['status'],'review')

    def test_classification_round_trips_without_changing_legacy_record_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            with RecordQueue(folder) as queue:
                queue.enqueue('old',RECORD,{'old'},['old.pdf'])
                old_id=queue.pending_records()[0].id
                queue.enqueue('member',{**RECORD,'classification':'CE/CW'},{'new'},['new.pdf'])
            with RecordQueue(folder) as queue:
                self.assertIn(old_id,[item.id for item in queue.pending_records()])
                member=next(item for item in queue.pending_records() if item.family=='member')
                self.assertEqual(send_person.load_approved_person(member.path)['classification'],'CE/CW')

    def test_failed_tag_verification_holds_created_person_and_never_reposts(self):
        with tempfile.TemporaryDirectory() as folder:
            with RecordQueue(folder) as queue:
                queue.enqueue('member',{**RECORD,'classification':'CE/CW'},{'source'},['member.pdf'])
            with (patch.object(ActionBuilderConfig,'from_environment',return_value=CONFIG),
                  patch.object(send_person.ActionBuilderLookup,'check',return_value=CLEAR),
                  patch.object(MemberAutomationClient,'preflight'),
                  patch.object(MemberAutomationClient,'verify',side_effect=MemberAutomationError('Tag missing')),
                  patch('send_person.requests.post',return_value=response(SUCCESS)) as post,
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                with self.assertRaises(MemberAutomationError):
                    send_person.send_queue(Path(folder),None,True)
                self.assertEqual(send_person.send_queue(Path(folder),None,True),0)
            post.assert_called_once()
            self.assertEqual(post.call_args.kwargs['json']['person']['action_builder:latest_assessment'],1)
            self.assertEqual(len(post.call_args.kwargs['json']['add_tags']),2)
            with RecordQueue(folder) as queue:
                self.assertEqual(queue.review_details(queue.review_records()[0])['status'],'uncertain')

    def test_reconciliation_cannot_hide_missing_member_tags(self):
        with tempfile.TemporaryDirectory() as folder:
            record={**RECORD,'classification':'CE/CW'}
            with RecordQueue(folder) as queue:
                queue.enqueue('member',record,{'source'},['member.pdf'])
                item=queue.pending_records()[0]
                queue.record_lookup(item,CLEAR.as_history(CONFIG))
                queue.begin_send(item)
                queue.mark_uncertain(item,'Member verification failed.')
                with (patch.object(ActionBuilderConfig,'from_environment',return_value=CONFIG),
                      patch.object(review_person.ActionBuilderLookup,'check',return_value=EXISTING),
                      patch.object(MemberAutomationClient,'verify',side_effect=MemberAutomationError('Tag missing')),
                      patch('requests.post') as post, redirect_stdout(io.StringIO())):
                    with self.assertRaises(MemberAutomationError):
                        review_person.reconcile_person(queue,item,record,checked=True)
                post.assert_not_called()
                self.assertEqual(queue.review_details(item)['status'],'uncertain')
