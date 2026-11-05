"""Device identity and encrypted provisioning. Never log keys or payloads."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CONTEXT = b'econtrol-controller-provisioning-v1'
DEFAULT_API = 'https://elevator.econtrol.co.il/api/licensing/controllers'

class ProvisioningError(Exception):
    """Only fixed error codes, without request data or credentials."""

def atomic_write(path: pathlib.Path, data: bytes, uid: int | None = None, gid: int | None = None):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ProvisioningError('unsafe_file_path')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        if uid is not None:
            os.fchown(fd, uid, gid if gid is not None else -1)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

class Identity:
    def __init__(self, key):
        self.key = key
        public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = public.hex()
        self.device_id = 'lift-' + hashlib.sha256(public).hexdigest()[:24]

    @classmethod
    def load_or_create(cls, directory: pathlib.Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / 'identity.pem'
        if not path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            atomic_write(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != 'secp256r1':
            raise ProvisioningError('invalid_device_identity')
        return cls(key)

    def sign(self, action, body, timestamp):
        signed = {'v':1, 'action':action, 'deviceId':self.device_id, 'publicKey':self.public_key,
                  'nonce':secrets.token_hex(16), 'timestamp':int(timestamp), 'body':body}
        canonical = json.dumps(signed, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        signature = self.key.sign(canonical, ec.ECDSA(hashes.SHA256()))
        return {k:v for k,v in signed.items() if k != 'action'} | {'signature':base64.b64encode(signature).decode()}

    def decrypt(self, envelope):
        try:
            revision = envelope['revision']
            if envelope.get('v') != 1 or type(revision) is not int or revision < 1:
                raise ValueError()
            if len(envelope['data']) > 4 * 1024 * 1024:
                raise ValueError()
            ephemeral = bytes.fromhex(envelope['ephemeral'])
            remote = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ephemeral)
            shared = self.key.exchange(ec.ECDH(), remote)
            key = hashlib.sha256(shared + CONTEXT + ephemeral + bytes.fromhex(self.public_key)).digest()
            data = AESGCM(key).decrypt(bytes.fromhex(envelope['iv']),
                bytes.fromhex(envelope['data']) + bytes.fromhex(envelope['tag']),
                (self.device_id + ':' + str(revision)).encode())
            decoded = json.loads(data)
            validate_assignment(decoded, self.device_id, revision)
            return decoded
        except Exception:
            raise ProvisioningError('invalid_encrypted_assignment') from None

def validate_assignment(value, device_id, revision):
    a, cfg = value['assignment'], value['config']
    s = cfg['settings']
    parsed = urllib.parse.urlsplit(s['FIREBASE_URL'])
    if (a['revision'] != revision or s.get('CONTROLLER_DEVICE_ID') != device_id or
        s.get('ELEVATOR_ID') != a['elevatorId'] or not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', a['elevatorId']) or
        parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port or
        not re.fullmatch(r'[a-z0-9-]+\.(?:[a-z0-9-]+\.)?(?:firebaseio\.com|firebasedatabase\.app)', parsed.hostname or '') or
        parsed.path != '/elevators.json' or parsed.query or parsed.fragment or
        not isinstance(s.get('SECRET_KEY'), str) or not s['SECRET_KEY'] or
        s['SECRET_KEY'] in ('***REDACTED***', 'PUT-YOUR-SECRET-KEY-HERE') or
        not re.fullmatch(r'/dev/(?:ttyUSB\d+|ttyACM\d+|serial\d+)', s.get('SERIAL_PORT', '')) or
        s.get('BAUDRATE') not in (9600,19200,38400,57600,115200)):
        raise ProvisioningError('invalid_assignment')
    allowed = {'FIREBASE_URL','ELEVATOR_ID','SECRET_KEY','SERIAL_PORT','BAUDRATE',
               'CONTROLLER_DEVICE_ID','LOG_BACKUP_ENABLED'}
    if set(s) - allowed or not isinstance(cfg.get('tags'), dict) or len(cfg['tags']) > 2000:
        raise ProvisioningError('invalid_assignment')
    if any(not re.fullmatch(r'[A-F0-9]{8,64}', tag) or not isinstance(floor, str) or len(floor) > 40 for tag,floor in cfg['tags'].items()):
        raise ProvisioningError('invalid_assignment')

class Client:
    def __init__(self, identity, api=DEFAULT_API, allow_local_test=False):
        parsed = urllib.parse.urlsplit(api)
        local = allow_local_test and parsed.hostname in ('127.0.0.1','localhost')
        if (not local and (parsed.scheme != 'https' or parsed.hostname != 'elevator.econtrol.co.il')) or parsed.username or parsed.password:
            raise ProvisioningError('invalid_provisioning_server')
        self.identity, self.api, self.offset = identity, api.rstrip('/'), 0

    def request(self, action, body=None):
        url = self.api + '/' + action
        data = None if action == 'time' else json.dumps(self.identity.sign(action, body or {}, time.time() + self.offset)).encode()
        request = urllib.request.Request(url, data=data, headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if urllib.parse.urlsplit(response.url).netloc != urllib.parse.urlsplit(self.api).netloc:
                    raise ProvisioningError('unexpected_server_redirect')
                result = json.loads(response.read(5 * 1024 * 1024))
        except urllib.error.HTTPError as error:
            raise ProvisioningError('server_http_' + str(error.code)) from None
        except Exception:
            raise ProvisioningError('server_unavailable') from None
        if action == 'time':
            self.offset = int(result['timestamp']) - time.time()
        return result
