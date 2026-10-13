"""Project authorization. Bundled into server.py so historical tool restores cannot remove it."""
import base64
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa, ed25519

OWNER_EMAIL = 'lamont.leras@gmail.com'
ACCESS_ISSUER = 'https://frosty-leaf-772d.cloudflareaccess.com'
ACCESS_AUDIENCE = '3bd94b86309c134085f50ad9c385eb9337eda339abfde012463bdc8c59a2677a'
PROJECTS = {
    'yas-karting': 'Karting',
    'qr-transfer': 'QR Transfer',
    'floor-plan': 'Floor Plan Editor', 'garmin': 'Garmin Watch Studio',
    'lampshade': 'Lampshade Studio', 'mechanisms': 'Mechanism Workbench',
    'nx-shortcuts': 'NX Shortcut Workbench', 'print-farm': '3D Print Farm Tracker',
    'power-screw': 'Power Screw Design Tool', 'stl-converter': 'STL to Solid Body Converter',
    'qr-studio': 'QR Print Studio',
    'kua-pairing': 'Kua Pairing',
}

def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def email_address(value):
    if not isinstance(value, str): raise ValueError('Enter a valid email address.')
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", value):
        raise ValueError('Enter a valid email address.')
    return value

def mailbox_key(value):
    """Only consumer Gmail ignores dots; Workspace and other domains do not."""
    value = email_address(value)
    local, domain = value.rsplit('@', 1)
    if domain == 'gmail.com' and re.fullmatch(r'[a-z0-9]+(?:\.[a-z0-9]+)*', local):
        return local.replace('.', '') + '@gmail.com'
    return value

def display_email(value):
    key = mailbox_key(value)
    return OWNER_EMAIL if key == mailbox_key(OWNER_EMAIL) else key

def b64url(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('Invalid encoding')
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))

class AccessControl:
    def __init__(self, data, public_key_path='/srv/deploy-public.hex'):
        self.file = Path(data) / 'access-control.json'
        self.public_key_path = Path(public_key_path)
        self.lock = threading.RLock()
        self.keys = {}
        self.keys_until = 0
        self.last_fetch = 0
        self.nonces = {}

    def document(self):
        # Missing on first install means owner only. Corruption fails closed.
        if not self.file.exists(): return {'users': {}, 'audit': []}
        doc = json.loads(self.file.read_text())
        if not isinstance(doc.get('users'), dict): raise ValueError('Access settings unavailable')
        return doc

    def revision(self, doc):
        return '"' + hashlib.sha256(canonical_json(doc)).hexdigest() + '"'

    def users(self):
        with self.lock:
            doc = self.document()
            return {'owner': OWNER_EMAIL, 'projects': PROJECTS, 'users': [
                {'email': email, **row} for email, row in sorted(doc['users'].items())
            ], 'audit': list(reversed(doc.get('audit', [])[-30:]))}, self.revision(doc)

    def update(self, value, revision, actor):
        email = mailbox_key(value.get('email'))
        if email == mailbox_key(OWNER_EMAIL): raise ValueError('The owner cannot be changed or revoked here.')
        grants = value.get('projects')
        enabled = value.get('enabled')
        if not isinstance(grants, list) or any(not isinstance(p, str) or p not in PROJECTS for p in grants):
            raise ValueError('Choose valid projects.')
        if not isinstance(enabled, bool): raise ValueError('Choose an account status.')
        if enabled and not grants: raise ValueError('Choose at least one project, or revoke access.')
        with self.lock:
            doc = self.document()
            if revision != self.revision(doc): raise FileExistsError('Access settings changed. Reload before saving.')
            if len(doc['users']) >= 200 and email not in doc['users']: raise ValueError('User limit reached.')
            aliases = [address for address in doc['users'] if mailbox_key(address) == email]
            before = doc['users'].get(aliases[0]) if aliases else None
            now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            row = {'projects': sorted(set(grants)), 'enabled': enabled, 'updated': now}
            for address in aliases: del doc['users'][address]
            doc['users'][email] = row
            doc['audit'] = (doc.get('audit', []) + [{'at': now, 'actor': actor, 'email': email,
                'action': 'revoked' if not enabled else 'added' if before is None else 'updated',
                'projects': row['projects']}])[-500:]
            temp = self.file.with_suffix('.tmp')
            temp.write_bytes(canonical_json(doc))
            os.replace(temp, self.file)
            return self.users()

    def identity(self, email):
        email = mailbox_key(email)
        if email == mailbox_key(OWNER_EMAIL): return {'email': OWNER_EMAIL, 'owner': True, 'projects': list(PROJECTS)}
        with self.lock:
            matches = [row for address, row in self.document()['users'].items() if mailbox_key(address) == email]
        # Legacy duplicates must never combine grants or override a revocation.
        if len(matches) > 1: raise PermissionError('Duplicate email spellings. Ask the owner to save this user again.')
        row = matches[0] if matches else None
        if not row or not row.get('enabled'): raise PermissionError('No project access. Ask the owner to add your email.')
        return {'email': email, 'owner': False, 'projects': [p for p in row.get('projects', []) if p in PROJECTS]}

    def signing_key(self, kid):
        with self.lock:
            now = time.time()
            if now < self.keys_until and kid in self.keys: return self.keys[kid]
            # Unknown key IDs must not cause an outbound request flood.
            if now - self.last_fetch < 30: raise ValueError('Unknown signing key')
            self.last_fetch = now
            with urllib.request.urlopen(ACCESS_ISSUER + '/cdn-cgi/access/certs', timeout=8) as response:
                data = json.loads(response.read(100000))
            keys = {}
            for jwk in data['keys']:
                if jwk.get('kty') == 'RSA' and jwk.get('alg') == 'RS256' and jwk.get('use') == 'sig':
                    keys[jwk['kid']] = rsa.RSAPublicNumbers(int.from_bytes(b64url(jwk['e']), 'big'),
                        int.from_bytes(b64url(jwk['n']), 'big')).public_key()
            self.keys, self.keys_until = keys, now + 3600
            if kid not in keys: raise ValueError('Unknown signing key')
            return keys[kid]

    def verify_jwt(self, token):
        if not isinstance(token, str) or len(token) > 24000: raise ValueError('Invalid login')
        head, payload, signature = token.split('.')
        header = json.loads(b64url(head))
        if header.get('alg') != 'RS256' or header.get('crit'): raise ValueError('Invalid algorithm')
        key = self.signing_key(header['kid'])
        key.verify(b64url(signature), (head + '.' + payload).encode(), padding.PKCS1v15(), hashes.SHA256())
        claims = json.loads(b64url(payload))
        if claims.get('iss') != ACCESS_ISSUER: raise ValueError('Invalid issuer')
        audience = claims.get('aud')
        if not (audience == ACCESS_AUDIENCE or isinstance(audience, list) and ACCESS_AUDIENCE in audience):
            raise ValueError('Invalid audience')
        now = time.time()
        for name in ('exp', 'iat'):
            if type(claims.get(name)) not in (int, float) or not math.isfinite(claims[name]): raise ValueError('Invalid dates')
        if claims['exp'] <= now or claims['iat'] > now + 30: raise ValueError('Expired or future login')
        if 'nbf' in claims and (type(claims['nbf']) not in (int, float) or not math.isfinite(claims['nbf']) or claims['nbf'] > now + 30):
            raise ValueError('Login not active')
        if claims.get('type') != 'app' or not claims.get('sub'): raise ValueError('User login required')
        return email_address(claims.get('email'))

    def verify_local(self, method, path, headers, body):
        stamp, nonce = headers.get('X-Project-Time', ''), headers.get('X-Project-Nonce', '')
        if not re.fullmatch(r'[0-9]{10,12}', stamp) or abs(time.time() - int(stamp)) > 60: raise ValueError('Expired request')
        if not re.fullmatch(r'[a-f0-9]{32}', nonce): raise ValueError('Invalid nonce')
        message = canonical_json(['mungoon-owner-request-v1', method, path, stamp, nonce,
            headers.get('Origin', ''), headers.get('If-Match', ''), hashlib.sha256(body).hexdigest()])
        key = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.public_key_path.read_text().strip()))
        key.verify(base64.b64decode(headers.get('X-Project-Signature', ''), validate=True), message)
        with self.lock:
            self.nonces = {n: t for n, t in self.nonces.items() if t > time.time() - 120}
            if nonce in self.nonces: raise ValueError('Replayed request')
            self.nonces[nonce] = time.time()
        return OWNER_EMAIL
