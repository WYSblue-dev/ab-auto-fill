"""Apply and verify the explicitly configured member tags and assessment.

Jurisdiction is a user-provided constant, not a geographical inference. Notes
about residence are deliberately not generated without a jurisdiction source.
"""
from __future__ import annotations

from copy import deepcopy
import re
import time

import requests

from member_classification import normalize_classification

SECTION = 'Fourth District Workers'
CLASSIFICATION_FIELD = 'Classification - 4D'
JURISDICTION_FIELD = 'Local Jurisdiction by Zip (Residence) - 4D'
RESIDENCE_ENV = 'ACTION_BUILDER_RESIDENCE_LOCAL'


class MemberAutomationError(RuntimeError):
    """Member information cannot be prepared or confirmed safely."""


def prepare_member_payload(payload, config):
    prepared = deepcopy(payload)
    tags = prepared.get('add_tags', [])
    local = config.residence_local
    if not local and not tags:
        return prepared  # Legacy contact-only installation, explicitly unconfigured.
    if not local:
        raise MemberAutomationError(f'Set {RESIDENCE_ENV} before submitting a classified member.')
    if not isinstance(tags, list) or len(tags) not in {1, 2}:
        raise MemberAutomationError('A classification is required. Review the PDF and choose a classification before sending.')
    tag = tags[0]
    if not isinstance(tag, dict) or tag.get('action_builder:section') != SECTION or tag.get('action_builder:field') != CLASSIFICATION_FIELD:
        raise MemberAutomationError('The classification tag does not match the approved member field.')
    try:
        classification = normalize_classification(tag.get('name'))
    except ValueError as error:
        raise MemberAutomationError(str(error)) from None
    prepared['add_tags'] = [classification_tag(classification),
                            {'action_builder:section': SECTION, 'action_builder:field': JURISDICTION_FIELD, 'name': local}]
    if len(tags) == 2 and tags[1] != prepared['add_tags'][1]:
        raise MemberAutomationError('The configured residence local changed. Review the member settings before sending.')
    prepared['person']['action_builder:latest_assessment'] = 1
    return prepared


def classification_tag(value):
    return {'action_builder:section': SECTION, 'action_builder:field': CLASSIFICATION_FIELD,
            'name': normalize_classification(value)}


class MemberAutomationClient:
    """Read only known campaign routes; never follow response URLs with a token."""
    def __init__(self, config):
        self.config = config
        self.next_request = time.monotonic() + .3

    def get(self, path, params=None):
        time.sleep(max(0, self.next_request-time.monotonic()))
        try:
            response = requests.get(self.config.people_url.removesuffix('/people') + path,
                                    headers=self.config.headers, params=params, timeout=(5,30), allow_redirects=False)
            if response.status_code != 200:
                raise MemberAutomationError(f'Member information could not be verified: HTTP {response.status_code}. Keep the record held; do not send it again.')
            result = response.json()
        except (requests.RequestException, ValueError):
            raise MemberAutomationError('Member information could not be verified. Check the connection; do not send the person again.') from None
        finally:
            self.next_request = time.monotonic() + .3
        if not isinstance(result, dict):
            raise MemberAutomationError('Member information returned an unexpected response. Review Action Builder.')
        return result

    def collection(self, path, key, *, name=None):
        output, page, previous_total, previous_size = [], 1, None, None
        while True:
            params = {'page':page}
            if name is not None:
                params['filter'] = "name eq '" + name.replace("'", "''") + "'"
            result = self.get(path, params)
            total, size = result.get('total_pages'), result.get('per_page')
            if (type(result.get('page')) is not int or result['page'] != page or type(total) is not int
                    or not 0 <= total <= 100 or type(size) is not int or not 1 <= size <= 25
                    or (previous_total is not None and (total != previous_total or size != previous_size))):
                raise MemberAutomationError('Member tag pagination is incomplete or changed. Keep the record held.')
            embedded = result.get('_embedded')
            rows = embedded.get(key) if isinstance(embedded, dict) else None
            if '_embedded' not in result and total == 0 and page == 1:
                rows = []
            if (not isinstance(rows,list) or len(rows)>size or any(not isinstance(row,dict) for row in rows)
                    or (total == 0 and (rows or page != 1)) or (page < total and len(rows) != size)
                    or (page > 1 and not rows)):
                raise MemberAutomationError('Member tags did not return a complete collection. Keep the record held.')
            links = result.get('_links',{})
            if not isinstance(links,dict) or (page>=total and links.get('next') is not None):
                raise MemberAutomationError('Member tag pagination links are inconsistent. Keep the record held.')
            output.extend(rows)
            if page>=total:
                return output
            previous_total, previous_size = total,size
            page += 1

    def preflight(self, payload):
        for expected in payload.get('add_tags',[]):
            found = [row for row in self.collection('/tags','osdi:tags',name=expected['name'])
                     if all(row.get(key)==value for key,value in expected.items())]
            if len(found)!=1 or found[0].get('action_builder:field_type')!='standard':
                raise MemberAutomationError('A configured classification or residence-local response is missing or ambiguous in the campaign. No new person should be sent until the tags are configured.')

    def verify(self, payload, receipt):
        if not payload.get('add_tags'):
            return
        identifiers = receipt.get('person',{}).get('identifiers',[])
        native = [value for value in identifiers if isinstance(value,str) and re.fullmatch(r'action_builder:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',value)]
        if len(native)!=1:
            raise MemberAutomationError('Cannot verify member tags without one confirmed person ID.')
        person_path = '/people/' + native[0].partition(':')[2]
        person = self.get(person_path)
        if not isinstance(person.get('identifiers'),list) or person['identifiers'].count(native[0]) != 1:
            raise MemberAutomationError('The person lookup did not return the confirmed person ID. Keep the record held; do not create another person.')
        assessment = person.get('action_builder:latest_assessment')
        if type(assessment) is not int or assessment != 1:
            if assessment is None:
                detail = 'The API did not return an assessment.'
            elif type(assessment) is int:
                detail = f'The API returned assessment {assessment}.'
            else:
                # Describe the shape, never include arbitrary API response text.
                detail = 'The API returned an assessment in an unexpected format.'
            raise MemberAutomationError(f'Assessment 1 was not confirmed. {detail} Review the existing entry, then reconcile; do not create another person.')
        tags = self.collection(person_path+'/taggings','osdi:taggings')
        for expected in payload['add_tags']:
            values = [row for row in tags if row.get('action_builder:section')==expected['action_builder:section'] and row.get('action_builder:field')==expected['action_builder:field']]
            if len(values)!=1 or values[0].get('name')!=expected['name']:
                label = 'Classification - 4D' if expected['action_builder:field'] == CLASSIFICATION_FIELD else 'Local Jurisdiction by Zip (Residence) - 4D'
                if not values:
                    detail = 'The API returned no response for this field.'
                elif len(values) != 1:
                    detail = f'The API returned {len(values)} responses for this field; one is required.'
                else:
                    detail = 'The API response did not match the configured value.'
                raise MemberAutomationError(f'{label} was not confirmed. {detail} Review the existing entry, then reconcile; do not create another person.')
