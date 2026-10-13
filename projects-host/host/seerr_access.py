"""Owner-only adapter for one existing Seerr Cloudflare policy; never a generic proxy."""
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from access import OWNER_EMAIL, canonical_json, email_address, mailbox_key, display_email

CF_ACCOUNT = 'd41f3e6bde0f8696db58508e5210183d'
CF_APP = '43465cea-b33b-4114-b719-c756afbd114a'
CF_POLICY = 'afbabfa2-7659-4e7d-8f4d-8f903b354ed7'
CF_BASE = '/accounts/' + CF_ACCOUNT + '/access'
CF_POLICY_PATH = CF_BASE + '/policies/' + CF_POLICY
CF_APP_PATH = CF_BASE + '/apps/' + CF_APP
CF_FIELDS = {'name', 'decision', 'include', 'exclude', 'require', 'approval_groups',
    'approval_required', 'connection_rules', 'isolation_required', 'mfa_config',
    'purpose_justification_prompt', 'purpose_justification_required', 'session_duration'}
CF_READ_FIELDS = {'id', 'uid', 'account_id', 'app_count', 'created_at', 'updated_at', 'reusable'}

class SeerrAccessError(Exception):
    """Only fixed, safe messages may cross the HTTP boundary."""

class NoCloudflareRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def cloudflare_request(token, method, path, value=None):
    if (method, path) not in {('GET', CF_POLICY_PATH), ('PUT', CF_POLICY_PATH),
            ('GET', CF_APP_PATH), ('GET', CF_APP_PATH + '/policies')}:
        raise SeerrAccessError('Unsupported Cloudflare operation.')
    req = urllib.request.Request('https://api.cloudflare.com/client/v4' + path,
        data=canonical_json(value) if value is not None else None, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(NoCloudflareRedirect()).open(req, timeout=12) as response:
            raw = response.read(1000001)
            if len(raw) > 1000000: raise ValueError()
            doc = json.loads(raw)
            if not isinstance(doc, dict) or doc.get('success') is not True: raise ValueError()
            # Never silently accept a truncated application policy list.
            if doc.get('result_info', {}).get('total_pages', 1) > 1: raise ValueError()
            return doc['result']
    except urllib.error.HTTPError as error:
        codes = []
        try:
            errors = json.loads(error.read(10000)).get('errors', [])
            codes = [str(e['code']) for e in errors if isinstance(e, dict) and type(e.get('code')) is int][:4]
        except Exception: pass
        detail = ' (HTTP ' + str(error.code) + (', codes ' + ', '.join(codes) if codes else '') + ').'
        if error.code in (401, 403):
            raise SeerrAccessError('Cloudflare rejected the token. It needs Access: Apps and Policies Edit for your account' + detail) from None
        if error.code == 429:
            raise SeerrAccessError('Cloudflare is rate limiting requests. Wait a moment, then refresh.') from None
        raise SeerrAccessError('Cloudflare could not complete the request' + detail + ' Check that you pasted the Cloudflare token, not a generated password.') from None
    except Exception:
        raise SeerrAccessError('Cloudflare could not be reached or returned an unexpected response. Refresh before trying again.') from None

class SeerrAccess:
    def __init__(self, data, transport=None):
        self.private = Path(data) / 'private'
        self.credential = self.private / 'seerr-cloudflare.json'
        self.audit_file = Path(data) / 'seerr-access-audit.json'
        self.request = transport or cloudflare_request
        self.lock = threading.RLock()

    @staticmethod
    def revision(policy):
        return '"' + hashlib.sha256(canonical_json(policy)).hexdigest() + '"'

    def token(self):
        if not self.credential.exists(): return None
        try:
            value = json.loads(self.credential.read_text())['token']
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,256}', value): raise ValueError()
            return value
        except Exception:
            raise SeerrAccessError('The saved connection could not be read. Reconnect Cloudflare.') from None

    def read_policy(self, token):
        app = self.request(token, 'GET', CF_APP_PATH)
        linked = self.request(token, 'GET', CF_APP_PATH + '/policies')
        policy = self.request(token, 'GET', CF_POLICY_PATH)
        if not isinstance(app, dict) or app.get('domain') != 'seerr.mungoon.net' or app.get('id') != CF_APP:
            raise SeerrAccessError('The configured application is no longer Seerr. No changes were made.')
        if not isinstance(linked, list) or len(linked) != 1 or linked[0].get('id') != CF_POLICY:
            raise SeerrAccessError('Seerr has a different policy setup. Review it before editing emails here.')
        if not isinstance(policy, dict) or policy.get('id') != CF_POLICY or policy.get('app_count') != 1 or policy.get('decision') != 'allow':
            raise SeerrAccessError('This policy must be an Allow policy used only by Seerr. No changes were made.')
        if policy.get('account_id', CF_ACCOUNT) != CF_ACCOUNT:
            raise SeerrAccessError('The policy belongs to a different account. No changes were made.')
        unknown = set(policy) - CF_FIELDS - CF_READ_FIELDS
        if unknown:
            names = ', '.join(sorted(k for k in unknown if re.fullmatch(r'[a-z_]{1,50}', k)))[:200]
            raise SeerrAccessError('Cloudflare returned unsupported policy fields' + (': ' + names if names else '') + '. No changes were made.')
        rules = policy.get('include')
        if not isinstance(rules, list) or not rules:
            raise SeerrAccessError('Seerr does not have a supported email allowlist.')
        try:
            for rule in rules:
                if set(rule) != {'email'} or set(rule['email']) != {'email'}: raise ValueError()
                email_address(rule['email']['email'])
        except (ValueError, TypeError, KeyError):
            raise SeerrAccessError('The policy includes rules other than individual emails. No changes were made.') from None
        return policy

    def view(self, policy):
        return {'connected': True, 'owner': OWNER_EMAIL, 'policyName': policy['name'],
            'emails': sorted(set(display_email(r['email']['email']) for r in policy['include'])),
            'checkedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'note': 'Removing an email blocks future sign-ins; existing Cloudflare sessions may remain active until they expire.'}, self.revision(policy)

    def status(self):
        with self.lock:
            token = self.token()
            if not token: return {'connected': False, 'emails': [], 'owner': OWNER_EMAIL}, None
            return self.view(self.read_policy(token))

    def connect(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,256}', token.strip()):
            raise ValueError('Paste a valid Cloudflare API token into this secure form.')
        token = token.strip()
        with self.lock:
            policy = self.read_policy(token)  # Read-only validation; never rewrite the list on connect.
            self.private.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.private, 0o700)
            fd, name = tempfile.mkstemp(prefix='.seerr-', dir=self.private)
            try:
                with os.fdopen(fd, 'wb') as out:
                    out.write(canonical_json({'token': token})); out.flush(); os.fsync(out.fileno())
                os.chmod(name, 0o600)
                os.replace(name, self.credential)
            finally:
                Path(name).unlink(missing_ok=True)
            return self.view(policy)

    def update(self, value, revision, actor):
        if set(value) != {'email', 'action'} or value['action'] not in ('add', 'remove'):
            raise ValueError('Choose add or remove and enter one email address.')
        email = email_address(value['email'])
        if value['action'] == 'remove' and mailbox_key(email) == mailbox_key(OWNER_EMAIL):
            raise ValueError('Your owner email cannot be removed here.')
        with self.lock:
            token = self.token()
            if not token: raise SeerrAccessError('Connect Cloudflare before editing emails.')
            policy = self.read_policy(token)
            if revision != self.revision(policy):
                raise FileExistsError('The Cloudflare list changed. Refresh the list, then try again.')
            addresses = {email_address(r['email']['email']) for r in policy['include']}
            aliases = {address for address in addresses if mailbox_key(address) == mailbox_key(email)}
            additions = {email, mailbox_key(email)} - addresses
            if value['action'] == 'add' and not additions or value['action'] == 'remove' and not aliases:
                return self.view(policy)
            if value['action'] == 'add' and len(addresses) >= 200: raise ValueError('Email limit reached.')
            body = copy.deepcopy({k: v for k, v in policy.items() if k in CF_FIELDS})
            if value['action'] == 'add': body['include'].extend({'email': {'email': address}} for address in sorted(additions))
            else: body['include'] = [r for r in body['include'] if mailbox_key(r['email']['email']) != mailbox_key(email)]
            if not body['include']: raise ValueError('The last email cannot be removed here.')
            try:
                self.request(token, 'PUT', CF_POLICY_PATH, body)
                saved = self.read_policy(token)
                expected = sorted(email_address(r['email']['email']) for r in body['include'])
                actual = sorted(email_address(r['email']['email']) for r in saved['include'])
                if actual != expected: raise SeerrAccessError('Verification failed.')
                for key, before in body.items():
                    if key != 'include' and saved.get(key) != before: raise SeerrAccessError('Verification failed.')
            except Exception:
                raise SeerrAccessError('The save could not be confirmed. Cloudflare may have applied it. Refresh the list before retrying; nothing was automatically retried.') from None
            result, etag = self.view(saved)
            try:
                audit = json.loads(self.audit_file.read_text()) if self.audit_file.exists() else []
                audit = (audit + [{'at': result['checkedAt'], 'actor': actor, 'email': email, 'action': value['action']}])[-500:]
                temp = self.audit_file.with_suffix('.tmp'); temp.write_bytes(canonical_json(audit)); os.replace(temp, self.audit_file)
            except Exception:
                result['warning'] = 'Cloudflare saved the change, but the local change log could not be updated.'
            return result, etag
