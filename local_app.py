"""Local browser interface. Run with this project's virtual-environment Python.

Only loopback connections are accepted. A per-launch secret authorizes API
requests; credentials stay server-side in the project's ignored .env file.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import dotenv_values, set_key
import requests

from action_builder_lookup import ActionBuilderConfig, ActionBuilderLookup, LookupError
from extract_person import import_downloads
from pdf_downloads_finder import DEFAULT_DOWNLOADS
from record_queue import DEFAULT_QUEUE, QueueError, RecordQueue
from review_person import REVIEW_FIELDS, normalize_review_record
from member_classification import CLASSIFICATIONS
from member_automation import MemberAutomationClient, MemberAutomationError, RESIDENCE_ENV, classification_tag, prepare_member_payload
from jurisdiction_reference import jurisdiction_reference
from residence_review import ResidenceReviews
from census_county import CountyLookupError, county_from_address
from send_person import SubmissionReceiptError, build_actionbuilder_payload, load_approved_person, submit_to_actionbuilder

ROOT = Path(__file__).resolve().parent
ENV_KEYS = ('ACTION_BUILDER_API_KEY', 'ACTION_BUILDER_SUBDOMAIN', 'ACTION_BUILDER_CAMPAIGN_ID', RESIDENCE_ENV)
COUNTY_ENV = 'MEMBER_INTAKE_COUNTY_LOOKUP'
SESSION_FILE = '.member-intake-session.json'


def _installation_id(root):
    return hashlib.sha256(str(Path(root).resolve()).encode('utf-8')).hexdigest()


def _session_descriptor(server):
    return {'version': 1, 'origin': server.origin, 'token': server.token,
            'instance_id': server.instance_id, 'installation_id': server.installation_id}


def _write_session(root, server):
    """Publish this instance's private launcher details only after binding."""
    path = Path(root) / SESSION_FILE
    if path.is_symlink():
        raise ValueError('The local session file must not be a symbolic link.')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root,
                                         prefix='.member-intake-session-', delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(_session_descriptor(server), stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_session(root):
    path = Path(root) / SESSION_FILE
    try:
        if path.is_symlink():
            return None
        info = path.stat()
        if info.st_size > 4096 or (os.name != 'nt' and info.st_mode & 0o077):
            return None
        data = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(data, dict)
                or set(data) != {'version', 'origin', 'token', 'instance_id', 'installation_id'}
                or type(data['version']) is not int or data['version'] != 1
                or not isinstance(data['origin'], str)
                or not isinstance(data['token'], str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', data['token'])
                or not isinstance(data['instance_id'], str)
                or not re.fullmatch(r'[0-9a-f]{32}', data['instance_id'])
                or data['installation_id'] != _installation_id(root)):
            return None
        return data
    except (OSError, UnicodeError, ValueError):
        return None


def _running_session_url(root, port):
    """Authenticate a known local instance; never navigate to a file's URL."""
    if not 1 <= port <= 65535:
        return None
    origin = f'http://127.0.0.1:{port}'
    data = _read_session(root)
    if data is None or data['origin'] != origin:
        return None
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(origin + '/api/session',
                                   headers={'X-App-Token': data['token'], 'Accept': 'application/json'},
                                   timeout=(1, 2), allow_redirects=False)
            if response.status_code != 200 or response.json() != {
                    'instance_id': data['instance_id'], 'installation_id': data['installation_id']}:
                return None
    except (requests.RequestException, ValueError):
        return None
    return f"{origin}/#{data['token']}"


def _remove_session(root, server):
    # Main calls this while still bound, before a replacement can take the port.
    # An instance launched on another port may have published a newer descriptor.
    if _read_session(root) == _session_descriptor(server):
        try:
            (Path(root) / SESSION_FILE).unlink()
        except FileNotFoundError:
            pass


def _open_session(url, *, no_browser):
    if no_browser:
        print(url, flush=True)
    elif not webbrowser.open(url):
        print('The browser could not open automatically. Open this local link:', flush=True)
        print(url, flush=True)


class Settings:
    def __init__(self, path: Path):
        self.path = path
        if not path.exists():
            # Exclusive creation never overwrites an existing installation.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write('# Member Intake local settings. Do not share this file.\n')
                for key in ENV_KEYS:
                    stream.write(f'{key}=\n')
                stream.write(f'{COUNTY_ENV}=manual\n')

    def values(self):
        return dotenv_values(self.path, interpolate=False)

    def config(self):
        values = self.values()
        # Read this file explicitly: settings edits must not be shadowed by
        # stale process environment values loaded by a previous CLI run.
        return ActionBuilderConfig(*(str(values.get(key) or '').strip() for key in ENV_KEYS))

    def public(self):
        values = self.values()
        try:
            self.config()
            configured = True
        except LookupError:
            configured = False
        return {'configured': configured, 'has_key': bool(values.get(ENV_KEYS[0])),
                'subdomain': values.get(ENV_KEYS[1]) or '',
                'campaign_id': values.get(ENV_KEYS[2]) or '',
                'residence_local': values.get(RESIDENCE_ENV) or '',
                'county_lookup': 'census' if values.get(COUNTY_ENV) == 'census' else 'manual',
                'downloads': values.get('MEMBER_INTAKE_DOWNLOADS') or str(DEFAULT_DOWNLOADS)}

    def proposed(self, data):
        key = data.get('api_key') or self.values().get(ENV_KEYS[0]) or ''
        values = (key, data.get('subdomain', ''), data.get('campaign_id', ''), data.get('residence_local', self.values().get(RESIDENCE_ENV) or ''))
        if any(not isinstance(value, str) for value in values):
            raise ValueError('Enter text for each credential field.')
        return ActionBuilderConfig(*(value.strip() for value in values))

    def save(self, data):
        config = self.proposed(data)
        county_lookup = data.get('county_lookup', self.public()['county_lookup'])
        if county_lookup not in {'manual', 'census'}:
            raise ValueError('Choose manual county entry or Census lookup.')
        downloads = data.get('downloads', str(DEFAULT_DOWNLOADS))
        if not isinstance(downloads, str) or not Path(downloads).expanduser().is_dir():
            raise ValueError('Enter an existing Downloads folder path.')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='.settings-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(self.path.read_text(encoding='utf-8'))
            for key, value in zip(ENV_KEYS, (config.api_key, config.subdomain, config.campaign_id, config.residence_local)):
                set_key(str(temporary), key, value)
            set_key(str(temporary), 'MEMBER_INTAKE_DOWNLOADS', str(Path(downloads).expanduser().resolve()))
            set_key(str(temporary), COUNTY_ENV, county_lookup)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return self.public()


def safe_error(error):
    # Request exceptions and unexpected errors can contain URLs or secrets.
    if isinstance(error, requests.RequestException):
        return 'Action Builder could not complete the request. Check your connection and credentials in Settings. Any unconfirmed submission is held for review.'
    if isinstance(error, (LookupError, QueueError, ValueError, SubmissionReceiptError, MemberAutomationError)):
        return str(error)
    return 'The operation stopped. Check your local files and settings. Any unconfirmed submission is held for review.'


class Application:
    def __init__(self, root=ROOT, queue_dir=DEFAULT_QUEUE):
        self.settings = Settings(Path(root) / '.env')
        self.queue_dir = Path(queue_dir)
        self.lock = threading.Lock()
        self.job_lock = threading.Lock()
        self.job = {'running': False, 'message': '', 'error': False}
        self.approvals = {}
        self.stopping = False

    def state(self):
        with self.lock:
            with RecordQueue(self.queue_dir) as queue:
                snapshot = queue.snapshot()
                reviews = ResidenceReviews(self.queue_dir)
                settings = self.settings.public()
                for row in snapshot['records']:
                    row['residence'] = reviews.get(row)
                    row['jurisdiction'] = jurisdiction_reference(row['residence'].get('county', ''),
                        row['residence'].get('region', row['person'].get('region', '')), settings['residence_local'])
            return {**snapshot, 'settings': settings, 'fields': REVIEW_FIELDS, 'classifications': CLASSIFICATIONS}

    def config_signature(self):
        config = self.settings.config()
        # Kept in memory only, never returned to the browser.
        return (config.api_key, config.subdomain, config.campaign_id, config.residence_local)

    def status(self):
        with self.job_lock:
            return dict(self.job)

    def start(self, action, data):
        with self.job_lock:
            if self.stopping:
                raise ValueError('The app is closing. Open it again using the desktop shortcut.')
            if self.job['running']:
                raise ValueError('An operation is already running. Wait for it to finish.')
            self.job = {'running': True, 'message': 'Working…', 'error': False}
        thread = threading.Thread(target=self._work, args=(action, data), daemon=False)
        thread.start()
        return {'started': True}

    def stop(self):
        with self.job_lock:
            if self.job['running']:
                raise ValueError('Wait for the current operation before closing the app.')
            self.stopping = True

    def _work(self, action, data):
        try:
            with self.lock:
                message = self.perform(action, data)
            result = {'running': False, 'message': message, 'error': False}
        except Exception as error:
            result = {'running': False, 'message': safe_error(error), 'error': True}
        with self.job_lock:
            self.job = result

    def _post(self, queue, item, payload, config):
        try:
            options = {'member_preflight': True} if payload.get('add_tags') else {}
            receipt = submit_to_actionbuilder(payload, config=config,
                on_person_receipt=lambda receipt: queue.record_person_receipt(item, receipt, destination=config.destination),
                **options)
            queue.finish_send(item, receipt)
        except BaseException as error:
            try:
                prefix = ('Person created; verification could not complete: '
                          if queue.pending_person_receipt(item) else 'Sending stopped: ')
                queue.mark_uncertain(item, prefix + safe_error(error))
            except (OSError, QueueError):
                pass
            raise
        time.sleep(0.3)

    def perform(self, action, data):
        if action == 'settings':
            self.settings.save(data)
            self.approvals.clear()
            return 'Settings saved. Use Test connection to check access to the campaign.'
        if action == 'test':
            config = self.settings.proposed(data)
            ActionBuilderLookup(config)._search('email_address', 'lookup-diagnostic@example.invalid')
            if config.residence_local:
                MemberAutomationClient(config).preflight(prepare_member_payload({'person':{}, 'add_tags':[classification_tag('CE/CW')]}, config))
            return 'Connection successful. The campaign people search is accessible. This does not test permission to create people.'
        if action == 'import':
            summary = import_downloads(self.settings.public()['downloads'], self.queue_dir)
            if self.settings.public()['residence_local']:
                with RecordQueue(self.queue_dir) as queue:
                    for item in queue.pending_records():
                        if not load_approved_person(item.path).get('classification'):
                            queue.hold(item.family, set(), [], 'Classification was not recognized in the PDF. Choose it in individual review before sending.')
                            summary.messages.append('A pending record was held because its classification needs review.')
            if self.settings.public()['county_lookup'] == 'census':
                found = unresolved = 0
                with RecordQueue(self.queue_dir) as queue:
                    reviews = ResidenceReviews(self.queue_dir)
                    for row in queue.snapshot()['records']:
                        if row['status'] not in {'pending', 'review'} or reviews.get(row):
                            continue
                        try:
                            reviews.save(row, county_from_address(row['person']))
                            found += 1
                        except CountyLookupError:
                            unresolved += 1
                summary.messages.append(f'{found} counties found · {unresolved} addresses need manual county entry. Review the matched addresses in County reference.')
            self.approvals.clear()
            values = asdict(summary)
            return '\n'.join([f"{values['queued']} queued · {values['unchanged']} unchanged · {values['review']} need review · {values['deferred']} waiting for download", *values['messages'], *values['errors']])
        if action in {'check', 'send'}:
            return self.batch(data, submit=action == 'send')
        if action in {'review-check', 'review-edit', 'review-discard', 'review-send', 'review-reconcile'}:
            return self.review(action, data)
        if action in {'county-save', 'county-lookup'}:
            return self.county_review(action, data)
        raise ValueError('Unknown operation.')

    def county_review(self, action, data):
        with RecordQueue(self.queue_dir) as queue:
            row = next((row for row in queue.snapshot()['records'] if row['id'] == data.get('id')), None)
            if row is None:
                raise ValueError('This record changed or is no longer available. Refresh the list.')
            if action == 'county-lookup':
                if self.settings.public()['county_lookup'] != 'census':
                    raise ValueError('Enable Census county lookup in Settings before sending an address to Census.')
                values = county_from_address(row['person'])
            else:
                values = {'county': data.get('county'), 'region': row['person']['region'].upper(), 'source': 'manual'}
            ResidenceReviews(self.queue_dir).save(row, values)
        return 'County reference saved locally. Review the county and map; any suggested note must be pasted into Action Builder manually.'

    def batch(self, data, *, submit):
        config = self.settings.config()
        if data.get('destination') != config.destination:
            raise ValueError('The destination changed. Refresh and review the batch again.')
        if data.get('residence_local', '') != config.residence_local:
            raise ValueError('The configured residence local changed. Refresh and review the batch again.')
        with RecordQueue(self.queue_dir) as queue:
            items = queue.pending_records()
            if sorted(data.get('ids', [])) != sorted(item.id for item in items):
                raise ValueError('The pending batch changed. Refresh and review it again.')
            if submit and data.get('confirmed') is not True:
                raise ValueError('Confirm the batch before submitting.')
            prepared = [(item, build_actionbuilder_payload(load_approved_person(item.path))) for item in items]
            client = ActionBuilderLookup(config)
            checked = held = sent = 0
            for item, payload in prepared:
                if item.id not in {current.id for current in queue.pending_records()}:
                    held += 1
                    continue
                try:
                    payload = prepare_member_payload(payload, config)
                except MemberAutomationError as error:
                    queue.hold(item.family, set(), [], str(error))
                    held += 1
                    continue
                result = client.check(payload)
                queue.record_lookup(item, result.as_history(config))
                checked += 1
                if result.outcome != 'not_found':
                    held += 1
                    continue
                if submit:
                    if payload.get('add_tags'):
                        MemberAutomationClient(config).preflight(payload)
                    time.sleep(0.3)
                    queue.begin_send(item)
                    self._post(queue, item, payload, config)
                    sent += 1
            return f'{checked} checked · {held} held for review · {sent} submitted.'

    def review(self, action, data):
        with RecordQueue(self.queue_dir) as queue:
            item = next((item for item in queue.review_records() if item.id == data.get('id')), None)
            if item is None:
                raise ValueError('This record is no longer available for review. Refresh the list.')
            details = queue.review_details(item)
            if action == 'review-edit':
                if details.get('related_created'):
                    raise ValueError('Action Builder already returned a person receipt. Verify the created person before editing this import.')
                queue.save_review_edit(item, normalize_review_record(data.get('person')))
                self.approvals.clear()
                return 'Corrected details saved. Run a fresh check before approving a submission.'
            if action == 'review-discard':
                queue.discard_review(item, data.get('reason', ''))
                self.approvals.pop(item.id, None)
                return 'Import discarded. Its history is retained to prevent reimporting it.'
            if action == 'review-reconcile':
                try:
                    if details['status'] != 'uncertain':
                        raise ValueError('Only an uncertain submission can be marked already created here.')
                    if data.get('confirmed') is not True or data.get('checked') is not True:
                        raise ValueError('Confirm that the person already exists with the correct details in Action Builder.')
                    config = self.settings.config()
                    if data.get('destination') != config.destination:
                        raise ValueError('The destination changed. Review the current campaign before reconciling.')
                    if data.get('residence_local', '') != config.residence_local:
                        raise ValueError('The residence local changed. Review the current member settings before reconciling.')
                    saved_receipt = queue.pending_person_receipt(item)
                    prior = saved_receipt or details.get('lookup')
                    if not prior or prior['destination'] != config.destination:
                        evidence = 'person receipt' if saved_receipt else 'submission lookup'
                        raise ValueError(f'The verification campaign must match the saved {evidence}. Restore that campaign in Settings before reconciling.')
                    payload = prepare_member_payload(build_actionbuilder_payload(load_approved_person(item.path)), config)
                    if saved_receipt:
                        MemberAutomationClient(config).verify(payload,
                            {'person': {'identifiers': saved_receipt['identifiers']}}, require_person=True)
                        queue.finish_person_verification(item, destination=config.destination,
                            identifiers=saved_receipt['identifiers'],
                            reason='Verified the saved Action Builder person ID and configured member details without sending again.')
                    else:
                        result = ActionBuilderLookup(config).check(payload)
                        # Check the prior destination above before replacing lookup evidence.
                        # Failed reconciliation must still show the newly found candidates.
                        queue.record_review_lookup(item, result.as_history(config))
                        if result.outcome != 'existing':
                            raise ValueError('Reconciliation requires one exact matching person. Review the latest matching and differing fields below; keep this record held.')
                        MemberAutomationClient(config).verify(payload, {'person':{'identifiers':result.candidates[0]['identifiers']}})
                        queue.reconcile_sent(item, lookup=result.as_history(config),
                                             reason='Earlier submission verified by the operator and fresh exact email and phone matches; reconciled without sending again.')
                except Exception as error:
                    if details['status'] == 'uncertain':
                        try:
                            queue.mark_uncertain(item, 'Verification stopped: ' + safe_error(error))
                        except (OSError, QueueError):
                            pass  # A failed queue commit must be recovered on reopening.
                    raise
                self.approvals.pop(item.id, None)
                return 'Earlier submission marked complete locally. No person was created or changed in Action Builder.'
            config = self.settings.config()
            payload = prepare_member_payload(build_actionbuilder_payload(load_approved_person(item.path)), config)
            if action == 'review-send':
                approval = self.approvals.pop(item.id, None)
                if (approval is None or approval['config'] != self.config_signature()
                        or time.monotonic() - approval['at'] > 600):
                    raise ValueError('Run Check revised details and review the result before approving this person.')
                if data.get('confirmed') is not True or data.get('checked') is not True:
                    raise ValueError('Confirm that you checked the existing records before creating a person.')
                if details['related_sent']:
                    raise ValueError('A related record was already sent. This person cannot be sent again.')
                if details.get('related_created'):
                    raise ValueError('A person was already created for this record. Verify the existing person instead of creating another one.')
                reason = data.get('reason', '')
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                    raise ValueError('Enter an approval reason of 1–500 characters.')
            result = ActionBuilderLookup(config).check(payload)
            queue.record_review_lookup(item, result.as_history(config))
            proof = {'result': result.as_history(config), 'uncertain': details['related_uncertain']}
            proof['result'].pop('checked_at')
            if action == 'review-check':
                self.approvals[item.id] = {'config': self.config_signature(), 'proof': proof, 'at': time.monotonic()}
                return result.reason
            if approval['proof'] != proof:
                raise ValueError('The lookup or earlier sending history changed. Review the updated results and check again before approving.')
            if payload.get('add_tags'):
                MemberAutomationClient(config).preflight(payload)
            time.sleep(0.3)
            queue.begin_review_send(item, config_destination=config.destination, reason=reason)
            self._post(queue, item, payload, config)
            return 'Submission confirmed. The record is now in Results.'


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, app):
        super().__init__(address, Handler)
        self.app = app
        self.token = secrets.token_urlsafe(32)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.instance_id = secrets.token_hex(16)
        self.installation_id = _installation_id(app.settings.path.parent)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log URLs, contact records, or credential submissions.

    def reply(self, status, data, content_type='application/json; charset=utf-8'):
        raw = json.dumps(data).encode() if not isinstance(data, bytes) else data
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self, *, api=False):
        if self.headers.get('Host') != self.server.origin.removeprefix('http://'):
            self.reply(403, {'error': 'Invalid local host.'})
            return False
        origin = self.headers.get('Origin')
        if origin is not None and origin != self.server.origin:
            self.reply(403, {'error': 'Only this local app can make requests.'})
            return False
        if api and not secrets.compare_digest(self.headers.get('X-App-Token', ''), self.server.token):
            self.reply(403, {'error': 'Open Member Intake using its desktop shortcut to start a session.'})
            return False
        return True

    def do_GET(self):
        if not self.authorized(api=self.path.startswith('/api/')):
            return
        try:
            if self.path == '/api/status':
                self.reply(200, self.server.app.status())
            elif self.path == '/api/session':
                self.reply(200, {'instance_id': self.server.instance_id,
                                 'installation_id': self.server.installation_id})
            elif self.path == '/api/state':
                if self.server.app.status()['running']:
                    self.reply(409, {'error': 'An operation is running. Wait for it to finish.'})
                else:
                    self.reply(200, self.server.app.state())
            else:
                files = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
                if self.path not in files:
                    self.reply(404, {'error': 'Not found.'})
                    return
                filename, mime = files[self.path]
                self.reply(200, (ROOT / 'local_ui' / filename).read_bytes(), mime + '; charset=utf-8')
        except Exception as error:
            self.reply(400, {'error': safe_error(error)})

    def do_POST(self):
        if not self.authorized(api=True):
            return
        try:
            if self.headers.get('Content-Type') != 'application/json':
                raise ValueError('Expected JSON.')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 65536:
                raise ValueError('Invalid request size.')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Expected an object.')
            if self.path == '/api/quit':
                self.server.app.stop()
                self.reply(200, {'closed': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            actions = {'settings', 'test', 'import', 'check', 'send', 'review-check', 'review-edit', 'review-discard', 'review-send', 'review-reconcile', 'county-save', 'county-lookup'}
            action = self.path.removeprefix('/api/')
            if self.path != '/api/' + action or action not in actions:
                self.reply(404, {'error': 'Not found.'})
                return
            self.reply(202, self.server.app.start(action, data))
        except Exception as error:
            self.reply(400, {'error': safe_error(error)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--data-dir', type=Path, default=ROOT,
                        help='Folder for .env; alternate folders also get their own queue (for testing).')
    args = parser.parse_args()
    root = args.data_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = Application(root, root / 'composed_info')
    try:
        server = LocalServer(('127.0.0.1', args.port), app)
    except OSError:
        url = _running_session_url(root, args.port)
        if url:
            print('Member Intake is already running. Opening its existing session.')
            _open_session(url, no_browser=args.no_browser)
            return 0
        print('Member Intake could not start on this port. An existing session for this installation could not be verified. Close the earlier run, or choose another port.')
        return 1
    try:
        _write_session(root, server)
    except (OSError, ValueError):
        server.server_close()
        print('Member Intake could not save its private local session file. Check that this installation folder is writable.')
        return 1
    url = f'{server.origin}/#{server.token}'
    print('Member Intake is running locally. Keep this window open while using it.')
    print('Use Close app in the browser to stop it, or press Ctrl+C here.')
    try:
        _open_session(url, no_browser=args.no_browser)
        server.serve_forever()
    except KeyboardInterrupt:
        print('Stopping the interface. Any active operation will finish before exit.')
    finally:
        try:
            _remove_session(root, server)
        finally:
            server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
