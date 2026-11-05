"""Enrollment security, durable installation and reconnect recovery."""
import base64
import json
import os
from pathlib import Path
import subprocess

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from provisioning.client import Client, Identity, ProvisioningError
from provisioning.agent import install_assignment

def assignment(identity, revision=1):
    return {'assignment':{'projectId':'hotel','projectName':'Hotel','customer':'Customer',
                         'elevatorId':'C','revision':revision,'assignedAt':1800000000},
            'config':{'settings':{'FIREBASE_URL':'https://test.firebaseio.com/elevators.json',
                                 'SECRET_KEY':'synthetic-fixture-only', 'ELEVATOR_ID':'C',
                                 'CONTROLLER_DEVICE_ID':identity.device_id,'SERIAL_PORT':'/dev/ttyUSB0',
                                 'BAUDRATE':115200,'LOG_BACKUP_ENABLED':False}, 'tags':{}}}

def test_signature_covers_action_identity_nonce_clock_and_body():
    identity = Identity(ec.generate_private_key(ec.SECP256R1()))
    request = identity.sign('register', {'model':'Pi 4'}, 1800000000)
    signed = {k:v for k,v in request.items() if k != 'signature'} | {'action':'register'}
    canonical = json.dumps(signed, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    identity.key.public_key().verify(base64.b64decode(request['signature']), canonical, ec.ECDSA(hashes.SHA256()))
    assert identity.sign('register', {}, 1800000000)['nonce'] != request['nonce']

def test_provisioning_server_cannot_be_replaced_with_an_arbitrary_host():
    identity = Identity(ec.generate_private_key(ec.SECP256R1()))
    for url in ('http://elevator.econtrol.co.il','https://evil.example/api','https://user:pass@elevator.econtrol.co.il/api'):
        with pytest.raises(ProvisioningError): Client(identity, url)

def test_actual_node_envelope_decrypts_in_python_and_tampering_fails():
    # This is a documented public test scalar, never a device credential.
    identity = Identity(ec.derive_private_key(1, ec.SECP256R1()))
    vectors = json.loads((Path(__file__).parent / 'fixtures/controller-envelope-v1.json').read_text())
    decoded = identity.decrypt(vectors['envelope'])
    assert decoded == vectors['plaintext']
    assert decoded['config']['tags'] == {}
    other = Identity(ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(ProvisioningError): other.decrypt(vectors['envelope'])
    for change in ({'revision':2}, {'tag':'00'*16}, {'data':'00'+vectors['envelope']['data'][2:]}):
        with pytest.raises(ProvisioningError): identity.decrypt(vectors['envelope'] | change)

unix_only = pytest.mark.skipif(os.name == 'nt', reason='Image filesystem permissions require Linux')

@unix_only
def test_identity_is_durable_and_two_cards_receive_different_identities(tmp_path):
    first = Identity.load_or_create(tmp_path / 'first')
    assert Identity.load_or_create(tmp_path / 'first').device_id == first.device_id
    assert Identity.load_or_create(tmp_path / 'second').device_id != first.device_id
    assert (tmp_path / 'first/identity.pem').stat().st_mode & 0o777 == 0o600

@unix_only
def test_failed_start_and_power_loss_can_retry_without_losing_the_assignment(tmp_path):
    root, state = tmp_path / 'app', tmp_path / 'state'
    root.mkdir();state.mkdir()
    identity = Identity.load_or_create(state)
    decoded = assignment(identity)
    def failed_start(): raise ProvisioningError('service_start_failed')
    with pytest.raises(ProvisioningError): install_assignment(root,state,identity,decoded,restart=failed_start)
    assert (root/'rfid_config.json').exists()
    assert not (state/'state.json').exists()
    starts=[]
    installed=install_assignment(root,state,identity,decoded,restart=lambda:starts.append(True))
    assert installed['revision'] == 1 and starts == [True]
    assert json.loads((root/'rfid_config.json').read_text()) == decoded['config']

@unix_only
def test_reconnect_preserves_new_tag_mappings_and_rejects_rollback(tmp_path):
    root, state = tmp_path/'app', tmp_path/'state';root.mkdir();state.mkdir()
    identity=Identity.load_or_create(state);decoded=assignment(identity,2)
    install_assignment(root,state,identity,decoded,restart=lambda:None)
    current=json.loads((root/'rfid_config.json').read_text());current['tags']['AABBCCDDEEFF']='3'
    (root/'rfid_config.json').write_text(json.dumps(current))
    install_assignment(root,state,identity,decoded,restart=lambda:pytest.fail('reconnected service restarted'))
    assert json.loads((root/'rfid_config.json').read_text())['tags'] == {'AABBCCDDEEFF':'3'}
    with pytest.raises(ProvisioningError): install_assignment(root,state,identity,assignment(identity,1),restart=lambda:None)

@unix_only
def test_enrollment_refuses_to_overwrite_a_legacy_controller(tmp_path):
    root, state = tmp_path/'app',tmp_path/'state';root.mkdir();state.mkdir()
    identity=Identity.load_or_create(state)
    original=json.dumps({'settings':{'SECRET_KEY':'synthetic-legacy'},'tags':{}})
    (root/'rfid_config.json').write_text(original)
    with pytest.raises(ProvisioningError): install_assignment(root,state,identity,assignment(identity),restart=lambda:None)
    assert (root/'rfid_config.json').read_text() == original
